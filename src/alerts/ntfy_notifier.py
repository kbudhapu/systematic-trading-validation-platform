"""ntfy push notifier (B5) — LIVENESS ALERTS ONLY, over a PUBLIC channel.

One HTTP POST to https://ntfy.sh/{NTFY_TOPIC}. Satisfies the heartbeat_watchdog.Notifier
protocol; slots into CompositeNotifier next to LogNotifier. One env var (NTFY_TOPIC), NO
credential, nothing to leak.

*** SECURITY FENCE — BINDING ***
ntfy default topics are PUBLIC: anyone who guesses the topic can READ it. This repo is public
and the strategy is published, so a single dollar figure on a guessable topic links a real amount
to a named person — a targeting profile. Therefore this channel carries **LIVENESS FACTS ONLY**
(service up/down, heartbeat age, cadence, candidate COUNTS, feed name, RTH-gate status,
data_integrity flags) and MUST NEVER emit any financial figure — balance, equity, PnL, positions,
fill/entry/exit prices, order details, symbols traded, notional, cash, or any API key. The payload
builder enforces this STRUCTURALLY: forbidden keys are stripped from the detail and dollar-amount
patterns are redacted from the body (see `build_ntfy_payload` + `test_ntfy_notifier`). Money
reporting is OUT OF SCOPE and goes to a private authenticated channel later.

Same boot-safety rules as EmailNotifier (B4): never raises, self-disables when NTFY_TOPIC is unset
(logs once at startup), no connection at construction, and LogNotifier always retains the durable
operator_alerts row.
"""
from __future__ import annotations

import os
import re
from typing import Callable

import structlog

from src.alerts.send_interlock import log_blocked_once, running_under_test

log = structlog.get_logger()

NTFY_BASE = "https://ntfy.sh"


class NtfyRateLimited(Exception):
    """ntfy.sh returned HTTP 429 (rate limited). Surfaced from the transport because
    ``httpx.post`` does NOT raise on 429 -- this is the ONLY signal that the rate limit was
    hit, so the notifier can LOG LOUDLY instead of silently dropping (F5)."""

# Financial fields that must NEVER reach a public topic. Matched case-insensitively as a substring
# of the detail key, so 'filled_price', 'avg_entry_price', 'realized_pnl' are all caught.
_FORBIDDEN_KEY_SUBSTR = (
    "balance", "equity", "pnl", "position", "price", "qty", "quantity", "symbol", "notional",
    "cash", "api_key", "secret", "token", "account", "fill", "order", "notion",
)
# Dollar-amount / money pattern redaction in free text (belt-and-suspenders over the message string).
_MONEY_RE = re.compile(r"[-+]?\$\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*\.\d{2}\b")

PostFn = Callable[[str, str, str, str], None]   # (topic, title, body, priority) -> None


def _forbidden(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _FORBIDDEN_KEY_SUBSTR)


def build_ntfy_payload(alert: dict) -> tuple[str, str, str]:
    """Return (title, body, priority) for a liveness alert. SECURITY: forbidden financial keys are
    stripped from the detail and dollar-amounts are redacted from the body — structurally, so a
    stray money value can never reach the public topic."""
    sev = str(alert.get("severity", "info")).upper()
    title = f"[mbappe {sev}] {alert.get('kind', 'alert')}"[:120]
    safe_detail = {k: v for k, v in (alert.get("detail") or {}).items() if not _forbidden(str(k))}
    body = str(alert.get("message", ""))
    if safe_detail:
        body += " | " + ", ".join(f"{k}={v}" for k, v in safe_detail.items())
    body = _MONEY_RE.sub("[redacted]", body)          # redact any dollar-amount that slipped into text
    priority = "urgent" if sev in {"CRITICAL", "ERROR"} else "default"
    return title, body[:400], priority


def build_liveness_ping(*, cadence_min, candidate_count: int, feed: str, epoch: str,
                        heartbeat_age_min=None, trading_day: bool = True,
                        soak_degradation_mode: str | None = None,
                        soak_degradation_days: float | None = None,
                        channel_stats: dict | None = None) -> dict:
    """The daily POSITIVE liveness ping (~06:30 ET), fired EVERY day (P3) -- trading days AND
    weekends/holidays. COUNTS ONLY (SFD 4.5 — no outcome statistics). A notifier that only fires on
    failure, or only on trading days, is indistinguishable from a broken one; firing every day makes
    silence ALWAYS mean the same thing (no morning ping => go look). On a non-trading day it says so
    rather than reporting a screen that was never expected to run."""
    if trading_day:
        msg = (f"collector alive | {cadence_min}min cadence | {int(candidate_count)} candidates | "
               f"feed={feed} | epoch={epoch}")
    else:
        msg = f"collector alive | market CLOSED (weekend/holiday) | {cadence_min}min cadence"
    detail = {"cadence_min": cadence_min, "candidate_count": int(candidate_count),
              "feed": feed, "epoch": epoch, "trading_day": trading_day}
    if heartbeat_age_min is not None:
        detail["heartbeat_age_min"] = heartbeat_age_min
    # R2.4b: carry the soak's degradation mode in the DAILY ping so a persistent safety latch cannot
    # hide (the six-day HARD_CRITICAL_DEGRADE was found via a DB query, not an alert). A one-shot
    # page on entry is not monitoring; the daily ping is the standing reminder. A degraded mode also
    # bumps the ping's severity so the operator's phone treats it as more than a routine heartbeat.
    severity = "default"
    if soak_degradation_mode and str(soak_degradation_mode).upper() != "NORMAL":
        days = f" ({int(soak_degradation_days)}d)" if soak_degradation_days is not None else ""
        msg += f" | soak: {soak_degradation_mode}{days}"
        detail["soak_degradation_mode"] = soak_degradation_mode
        if soak_degradation_days is not None:
            detail["soak_degradation_days"] = soak_degradation_days
        severity = "high"
    else:
        detail["soak_degradation_mode"] = soak_degradation_mode or "NORMAL"
    # AG4b: the alert channel reports its OWN health -- it is the one component whose failure is
    # invisible (a 200 does not mean the phone rang). Surface send counts + the last 429 in the ping.
    if channel_stats:
        detail["ntfy_sends_today"] = channel_stats.get("sends_today")
        detail["ntfy_sends_last_hour"] = channel_stats.get("sends_last_hour")
        if channel_stats.get("last_429_at"):
            detail["ntfy_last_429_at"] = channel_stats.get("last_429_at")
    return {"kind": "daily_liveness", "severity": severity, "message": msg, "detail": detail}


def _http_post_ntfy(topic: str, title: str, body: str, priority: str, *, timeout: float = 5.0) -> None:
    # F1 HARD INTERLOCK: never hit the real network under the test suite (belt to notify()'s brace).
    if running_under_test():
        log_blocked_once("ntfy")
        return
    import httpx  # already a dependency; lazy so import never runs at construction
    resp = httpx.post(f"{NTFY_BASE}/{topic}", content=body.encode("utf-8"),
                      headers={"Title": title, "Priority": priority}, timeout=timeout)
    # F5: httpx.post does NOT raise on 4xx/5xx, so a 429 would otherwise be a SILENT DROP.
    # Surface it so NtfyNotifier logs loudly (the durable operator_alerts row is unaffected).
    if resp.status_code == 429:
        raise NtfyRateLimited(f"ntfy rate-limited (HTTP 429)")
    if resp.status_code >= 400:
        raise RuntimeError(f"ntfy non-2xx (HTTP {resp.status_code})")


class NtfyNotifier:
    """Push liveness alerts to ntfy.sh/{topic}. Never raises; self-disables without NTFY_TOPIC."""

    def __init__(self, *, topic: str | None = None, post_fn: PostFn | None = None,
                 cap: object | None = None) -> None:
        self._topic = topic if topic is not None else os.getenv("NTFY_TOPIC", "")
        self._post = post_fn or _http_post_ntfy       # injectable; NOT called at construction
        # AG2/AG3/AG4: a SHARED, hard outbound cap (constructed lazily so boot stays connection-free).
        # None -> lazily build the process-shared default (same DB for the soak AND the collector, so
        # one component's storm can never eat another's ntfy budget).
        self._cap = cap
        self._cap_resolved = cap is not None

    def _get_cap(self):
        if self._cap is None and not self._cap_resolved:
            self._cap_resolved = True
            try:
                from src.alerts.outbound_cap import OutboundCap
                self._cap = OutboundCap()
            except Exception as e:
                log.warning("outbound_cap_unavailable", error=str(e))
                self._cap = None
        return self._cap

    def enabled(self) -> bool:
        return bool(self._topic)

    def notify(self, alert: dict) -> None:
        if not self.enabled():
            return
        # F1 HARD INTERLOCK: the notifier itself refuses to send under the test suite, so no test
        # can page a human by accident. Only the REAL transport is blocked -- an injected mock
        # post_fn (the "assert it sends" tests) is left free.
        if self._post is _http_post_ntfy and running_under_test():
            log_blocked_once("ntfy")
            return
        cap = self._get_cap()
        if cap is not None:
            allowed, reason = cap.allow(alert.get("kind"), alert.get("severity"))
            if not allowed:
                st = cap.stats()
                # AG2: log the cap-hit LOUDLY with the suppressed context. The durable
                # operator_alerts row was written UPSTREAM (LogNotifier); only the PHONE is affected.
                log.warning("ntfy_cap_exceeded", kind=alert.get("kind"), reason=reason,
                            sends_last_hour=st.get("sends_last_hour"),
                            sends_today=st.get("sends_today"), last_429_at=st.get("last_429_at"),
                            note="suppressed from the phone; durable operator_alerts row is unaffected")
                return
        try:
            title, body, priority = build_ntfy_payload(alert)
            self._post(self._topic, title, body, priority)
        except NtfyRateLimited as e:                     # AG4: a 429 is a CHANNEL INCIDENT, not transient
            if cap is not None:
                cap.note_429(alert.get("kind"))
            log.error("ntfy_channel_incident_429", error=str(e),
                      note="Firebase ~10min IP ban likely; routine lane backs off 1h; a 200 would not "
                           "have meant the phone rang. Durable operator_alerts row is unaffected")
        except Exception as e:                          # never let a push failure escape
            log.warning("ntfy_post_failed", error=str(e))
