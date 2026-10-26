"""AH3 — a SECOND, INDEPENDENT push path so the dead-man survives a Firebase ban.

ntfy.sh delivers via Google Firebase; a Firebase 429 bans the publishing IP for ~10 minutes with NO
indication to the sender (a 200 does not mean the phone rang). A dead-man with a single point of
failure is not a dead-man. The Telegram Bot API is a completely independent push path (Telegram's
own infrastructure, not Firebase), a single HTTPS POST, free, and it reliably buzzes -- chosen over
Discord (a webhook without an @mention delivers a SILENT badge -- the July-7 shape) and over email
(EmailNotifier exists but email does not buzz).

PRIORITY MESSAGES ONLY (AH3a): the daily liveness ping and CRITICAL/urgent pages -- the dead-man and
real incidents -- go over BOTH ntfy AND Telegram; routine chatter stays on ntfy so the second
channel never burns its own limit and its silence always means something.

Boot-safety (B4/B5): never raises, self-disables without TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, no
connection at construction. The durable operator_alerts row (LogNotifier) is always kept. SECURITY:
reuses build_ntfy_payload's forbidden-financial-key stripping + dollar-amount redaction -- liveness
facts only, never a money figure, exactly as the public-channel fence requires.
"""
from __future__ import annotations

import os
from typing import Callable

import structlog

from src.alerts.ntfy_notifier import build_ntfy_payload
from src.alerts.outbound_cap import _is_priority
from src.alerts.send_interlock import log_blocked_once, running_under_test

log = structlog.get_logger()

TelegramPostFn = Callable[[str, str, str], None]   # (token, chat_id, text) -> None


def _http_post_telegram(token: str, chat_id: str, text: str, *, timeout: float = 8.0) -> None:
    # F1 HARD INTERLOCK: never hit the real network under the test suite.
    if running_under_test():
        log_blocked_once("telegram")
        return
    import httpx  # lazy so import never runs at construction
    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000]},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"telegram non-2xx (HTTP {resp.status_code})")


class TelegramNotifier:
    """Push PRIORITY liveness alerts to Telegram. Never raises; self-disables without token+chat_id."""

    def __init__(self, *, token: str | None = None, chat_id: str | None = None,
                 post_fn: TelegramPostFn | None = None) -> None:
        self._token = token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self._post = post_fn or _http_post_telegram      # injectable; NOT called at construction

    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def notify(self, alert: dict) -> None:
        if not self.enabled():
            return
        # AH3a: the second channel carries the dead-man + real pages ONLY, never routine chatter.
        if not _is_priority(alert.get("kind"), alert.get("severity")):
            return
        if self._post is _http_post_telegram and running_under_test():
            log_blocked_once("telegram")
            return
        try:
            title, body, _priority = build_ntfy_payload(alert)   # reuse the security redaction
            self._post(self._token, self._chat_id, f"{title}\n{body}")
        except Exception as e:                                    # never let a push failure escape
            log.warning("telegram_post_failed", error=str(e))
