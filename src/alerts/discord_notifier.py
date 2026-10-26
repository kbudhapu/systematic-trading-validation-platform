"""AI — a THIRD independent alert path (Discord webhook) so the dead-man is not one Firebase ban
away from silence. ntfy delivers via Google Firebase; a per-IP Firebase ban (proven per-IP in AH2)
silences EVERY ntfy topic from the droplet with no indication to the sender. Discord and Telegram
share none of that relay.

*** THE SILENT-BADGE TRAP (AI1) — get this wrong and the channel is decorative: *** a plain Discord
webhook message produces a SILENT UNREAD BADGE, not a notification -- detection works, delivery
reaches nobody (the July-7 shape). So PRIORITY messages (daily_liveness / critical / urgent) put
`@here` in the `content` field (NOT an embed -- embeds do not trigger mentions) AND set
`allowed_mentions:{"parse":["everyone"]}` (without it Discord silently strips the mention). Routine
carries no mention (a badge is fine), but routine stays on ntfy anyway -- Discord is priority-only.

INDEPENDENCE (AI3): Discord shares NONE of ntfy's OutboundCap budget -- a cap or a ban on ntfy must
never silence Discord (that is the entire point). It shares only the ThrottledNotifier's dedup so a
repeating condition cannot spam it either.

Boot-safety (B4/B5): never raises, self-disables without DISCORD_WEBHOOK_URL, no connection at
construction, LogNotifier keeps the durable row. SECURITY: reuses build_ntfy_payload's forbidden-key
strip + money redaction (SFD 4.5 governs Phase-0 outcome stats regardless of channel privacy -- one
transport, one rule). *** The webhook URL is a CREDENTIAL: it is REDACTED from every log line and
error message (an httpx exception prints the full URL). ***
"""
from __future__ import annotations

import os
import re
from typing import Callable

import structlog

from src.alerts.ntfy_notifier import build_ntfy_payload
from src.alerts.outbound_cap import _is_priority
from src.alerts.send_interlock import log_blocked_once, running_under_test

log = structlog.get_logger()

DiscordPostFn = Callable[[str, str, dict | None], None]   # (webhook_url, content, allowed_mentions)

_WEBHOOK_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+")


def _redact_webhook(text: str, url: str) -> str:
    """A webhook URL anyone holds can post FAKE alerts to the dead-man channel. Never let it into a
    log or an error string -- httpx prints the full request URL on failure."""
    out = text.replace(url, "<DISCORD_WEBHOOK_URL redacted>") if url else text
    return _WEBHOOK_RE.sub("<discord_webhook redacted>", out)


def build_discord_content(alert: dict) -> tuple[str, dict | None]:
    """Return (content, allowed_mentions). PRIORITY -> '@here ...' + allowed_mentions so it actually
    NOTIFIES; routine -> plain (silent badge). Money is redacted via build_ntfy_payload."""
    title, body, _priority = build_ntfy_payload(alert)   # reuse the security redaction
    if _is_priority(alert.get("kind"), alert.get("severity")):
        sev = str(alert.get("severity", "")).upper()
        content = f"@here **[MBAPPE {sev}]** {title}\n{body}"
        allowed_mentions = {"parse": ["everyone"]}
        return content[:1900], allowed_mentions
    return f"{title}\n{body}"[:1900], None


class DiscordRateLimited(Exception):
    """Discord returned HTTP 429."""


def _http_post_discord(webhook_url: str, content: str, allowed_mentions: dict | None,
                       *, timeout: float = 8.0) -> None:
    if running_under_test():
        log_blocked_once("discord")
        return
    import httpx  # lazy so import never runs at construction
    payload: dict = {"content": content}
    if allowed_mentions:
        payload["allowed_mentions"] = allowed_mentions
    resp = httpx.post(webhook_url, json=payload, timeout=timeout)
    if resp.status_code == 429:
        raise DiscordRateLimited("discord rate-limited (HTTP 429)")
    if resp.status_code >= 400:
        # NB: no URL in the message (webhook is a credential).
        raise RuntimeError(f"discord non-2xx (HTTP {resp.status_code})")


class DiscordNotifier:
    """Push PRIORITY liveness alerts to a Discord webhook with @here. Never raises; self-disables
    without DISCORD_WEBHOOK_URL. Independent of ntfy's cap/ban."""

    def __init__(self, *, webhook_url: str | None = None, post_fn: DiscordPostFn | None = None) -> None:
        self._url = webhook_url if webhook_url is not None else os.getenv("DISCORD_WEBHOOK_URL", "")
        self._post = post_fn or _http_post_discord      # injectable; NOT called at construction

    def enabled(self) -> bool:
        return bool(self._url)

    def notify(self, alert: dict) -> None:
        if not self.enabled():
            return
        # AI3 / AH: priority-only. Routine stays on ntfy so this channel never burns its limit and
        # its silence always means something.
        if not _is_priority(alert.get("kind"), alert.get("severity")):
            return
        if self._post is _http_post_discord and running_under_test():
            log_blocked_once("discord")
            return
        try:
            content, allowed_mentions = build_discord_content(alert)
            self._post(self._url, content, allowed_mentions)
        except DiscordRateLimited as e:
            log.warning("discord_rate_limited", error=_redact_webhook(str(e), self._url),
                        note="durable operator_alerts row unaffected")
        except Exception as e:                          # never let a push failure escape
            # REDACT the webhook URL -- an httpx exception happily prints the full URL.
            log.warning("discord_post_failed", error=_redact_webhook(str(e), self._url))
