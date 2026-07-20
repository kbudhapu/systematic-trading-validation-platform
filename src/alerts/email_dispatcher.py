"""Email alert dispatcher (P3.1) — reverses the earlier "email dispatcher NOT built".

This channel exists because the dead-man failure mode this whole task closes is *an
alert that fails to send AND fails to log anywhere*. So EmailNotifier is built to
never be that: it is async and non-blocking (no SMTP call in the orchestrator's event
loop), it times out, it retries once, and if delivery still fails it falls back to the
LogNotifier path so the alert always lands in the durable ``operator_alerts`` table.

Reversal of a logged decision is itself logged: see ``docs/OPERATOR_LOG.md``
(2026-07-12), ``docs/RUNBOOK.md``, ``docs/SOAK_CHECKLIST.md``. Safety actions never
depend on any notification channel — that invariant is unchanged.

Credentials come from the environment only (``SMTP_HOST/PORT/USER/PASSWORD``,
``EMAIL_FROM/EMAIL_TO``); no secret is ever committed or logged.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable

import structlog

from src.alerts.send_interlock import log_blocked_once, running_under_test
from src.control.heartbeat_watchdog import LogNotifier, Notifier

log = structlog.get_logger()

DEFAULT_TIMEOUT_S = 10.0

# A send function: (subject, body, cfg) -> awaitable. Injected in tests; the default
# lazily imports aiosmtplib so a missing package degrades to the LogNotifier fallback
# at runtime instead of breaking import.
SendFn = Callable[[str, str, dict], Awaitable[None]]


async def _aiosmtplib_send(subject: str, body: str, cfg: dict) -> None:
    """Real SMTP send over STARTTLS. Lazy import: absence -> caught -> fallback."""
    # F1 HARD INTERLOCK: the suite must never email a human. Absent this, the moment real SMTP
    # config lands in the environment the tests would start sending. Raising here is caught by
    # ``deliver`` like any send failure -> the durable LogNotifier fallback still fires.
    if running_under_test():
        log_blocked_once("email")
        raise RuntimeError("email send blocked under test (send_interlock)")
    import aiosmtplib  # noqa: PLC0415 (lazy on purpose)
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = cfg["email_from"]
    msg["To"] = cfg["email_to"]
    msg["Subject"] = subject
    msg.set_content(body)
    await aiosmtplib.send(
        msg,
        hostname=cfg["smtp_host"],
        port=cfg["smtp_port"],
        username=cfg["smtp_user"] or None,
        password=cfg["smtp_password"] or None,
        start_tls=True,
        timeout=cfg["timeout_s"],
    )


def _smtp_config_from_env(timeout_s: float) -> dict:
    return {
        "smtp_host": os.getenv("SMTP_HOST", ""),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "smtp_user": os.getenv("SMTP_USER", ""),
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        "email_from": os.getenv("EMAIL_FROM", ""),
        "email_to": os.getenv("EMAIL_TO", ""),
        "timeout_s": timeout_s,
    }


class EmailNotifier:
    """Notifier that emails alerts, with a guaranteed LogNotifier fallback.

    ``notify`` is synchronous (the Notifier protocol) but never blocks on SMTP: if an
    event loop is running it schedules delivery as a fire-and-forget task; otherwise
    (sync callers, tests) it drives delivery to completion. Delivery itself times out,
    retries once, then falls back — it never raises into the caller.
    """

    def __init__(
        self,
        fallback: Notifier,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        send_fn: SendFn | None = None,
        config: dict | None = None,
    ) -> None:
        self._fallback = fallback
        self._timeout_s = float(timeout_s)
        self._send_fn = send_fn or _aiosmtplib_send
        self._config = config or _smtp_config_from_env(self._timeout_s)
        self._config.setdefault("timeout_s", self._timeout_s)

    @staticmethod
    def _render(alert: dict) -> tuple[str, str]:
        sev = str(alert.get("severity", "info")).upper()
        subject = f"[mbappe {sev}] {alert.get('kind', 'alert')}: {alert.get('message', '')}"[:200]
        detail = alert.get("detail", {})
        body = f"{alert.get('message', '')}\n\nseverity: {sev}\nkind: {alert.get('kind')}\ndetail: {detail}"
        return subject, body

    def _configured(self) -> bool:
        c = self._config
        return bool(c.get("smtp_host") and c.get("email_from") and c.get("email_to"))

    async def deliver(self, alert: dict) -> bool:
        """Send with timeout + one retry; on failure fall back to LogNotifier.
        Returns True iff the email was sent. Never raises."""
        if not self._configured():
            # Not configured is not an error to swallow — the alert must still land.
            self._fallback.notify(_annotate(alert, "email_not_configured"))
            return False

        subject, body = self._render(alert)
        for attempt in (1, 2):
            try:
                await asyncio.wait_for(
                    self._send_fn(subject, body, self._config), timeout=self._timeout_s
                )
                return True
            except Exception as e:  # timeout, SMTP error, missing aiosmtplib, ...
                log.warning("email_send_failed", attempt=attempt, error=str(e))
        # Both attempts failed -> the alert must not be lost.
        self._fallback.notify(_annotate(alert, "email_delivery_failed"))
        return False

    def notify(self, alert: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self.deliver(alert))  # fire-and-forget; non-blocking
        else:
            asyncio.run(self.deliver(alert))


def _annotate(alert: dict, reason: str) -> dict:
    detail = {**alert.get("detail", {}), reason: True}
    return {**alert, "detail": detail,
            "message": f"{alert.get('message', '')} [{reason}]"}


class _NullNotifier:
    """A no-op fallback. Used inside the composite where a sibling LogNotifier already guarantees
    the durable row, so EmailNotifier's own fallback would otherwise double-write it (F4)."""

    def notify(self, alert: dict) -> None:  # noqa: D401 - intentional no-op
        return None


class CompositeNotifier:
    """Fan an alert out to several notifiers. Keeps the durable operator_alerts row
    (LogNotifier) AND the page (EmailNotifier); one channel failing never blocks the
    others, and no channel can raise into the watchdog."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = notifiers

    def notify(self, alert: dict) -> None:
        for n in self._notifiers:
            try:
                n.notify(alert)
            except Exception as e:
                log.error("composite_notifier_channel_failed",
                          channel=type(n).__name__, error=str(e))

    def resolve(self, *, component: str, kind: str) -> None:
        """Delegate a condition-cleared signal to any child that supports it (the ThrottledNotifier
        push leg emits ONE 'recovered' page + resets its backoff). Never raises into the watchdog."""
        for n in self._notifiers:
            fn = getattr(n, "resolve", None)
            if callable(fn):
                try:
                    fn(component=component, kind=kind)
                except Exception as e:
                    log.error("composite_notifier_resolve_failed",
                              channel=type(n).__name__, error=str(e))


def build_log_and_email(db_path: str | Path, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                        send_fn: SendFn | None = None) -> CompositeNotifier:
    """The shipped notifier: durable log row + throttled email/ntfy page.

    Structure: ``CompositeNotifier(LogNotifier, ThrottledNotifier(email, ntfy, telegram, discord))``.
      * LogNotifier is NEVER throttled -- every alert always lands in ``operator_alerts`` (F5).
      * email + ntfy + telegram + discord are wrapped in one ThrottledNotifier so a repeating
        condition dedups + backs off instead of paging every poll and burning the rate limit (F5).
        Telegram + Discord are independent of ntfy's Firebase relay AND its OutboundCap (AH/AI), so
        a Firebase ban or a routine-cap on ntfy cannot silence the dead-man's priority pages.
      * EmailNotifier is given a NO-OP fallback here (not the LogNotifier): the composite's own
        LogNotifier leg is the durable row, so email's fallback would double-write it (F4)."""
    from src.alerts.discord_notifier import DiscordNotifier
    from src.alerts.ntfy_notifier import NtfyNotifier
    from src.alerts.telegram_notifier import TelegramNotifier
    from src.alerts.throttle import ThrottledNotifier
    log_notifier = LogNotifier(db_path)
    email = EmailNotifier(_NullNotifier(), timeout_s=timeout_s, send_fn=send_fn)
    ntfy = NtfyNotifier()
    telegram = TelegramNotifier()   # AH3: a SECOND path independent of ntfy's Firebase relay
    discord = DiscordNotifier()     # AI: a THIRD path, also off Firebase, with @here so it BUZZES
    # B4/B5: boot-safety visibility. Construction never raises or connects; if a channel is
    # unconfigured we say so ONCE at startup so the operator knows that leg is dark (alerts still
    # land in the durable LogNotifier row and every page degrades cleanly to log-only).
    if not email._configured():
        log.warning("email_alerting_disabled",
                    reason="no SMTP config (SMTP_HOST/EMAIL_FROM/EMAIL_TO)",
                    fallback="LogNotifier durable row only")
    if not ntfy.enabled():
        log.warning("push_alerting_disabled", reason="no NTFY_TOPIC",
                    fallback="LogNotifier durable row only")
    if not telegram.enabled():
        # AH3: the SECOND channel is dark until the operator sets TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
        # Say so at boot -- an unconfigured dead-man backup is the July-7 silence waiting to recur.
        log.warning("telegram_alerting_disabled",
                    reason="no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID",
                    fallback="ntfy + LogNotifier durable row only (NO independent path if ntfy is banned)")
    if not discord.enabled():
        # AI: the THIRD channel is dark until the operator sets DISCORD_WEBHOOK_URL (by hand, in the
        # droplet .env -- it is a credential, never in a file/commit/log here). Say so at boot.
        log.warning("discord_alerting_disabled",
                    reason="no DISCORD_WEBHOOK_URL",
                    fallback="ntfy + Telegram + LogNotifier durable row only")
    # PRIORITY (daily_liveness + critical/urgent) fans out to ntfy AND Telegram AND Discord (both
    # Telegram and Discord self-filter to priority only); routine chatter stays on ntfy. Discord is
    # deliberately NOT bound by ntfy's OutboundCap -- a cap or Firebase ban on ntfy must never silence
    # it (AI3). It shares only this ThrottledNotifier's dedup so a storm cannot spam it either. The
    # durable LogNotifier row (outside the throttle) is always written regardless of any channel.
    throttled_push = ThrottledNotifier(CompositeNotifier(email, ntfy, telegram, discord))
    return CompositeNotifier(log_notifier, throttled_push)
