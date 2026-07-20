"""F5 — ThrottledNotifier: dedup + exponential backoff for the PUSH channels.

A persistent fault (e.g. a stale heartbeat every watchdog poll) would otherwise fire the same
alert every 30s, which (a) trains the operator to ignore the channel and (b) burns the ntfy rate
limit so REAL alerts are dropped -- the exact self-DoS that motivated this. This wraps the push
notifiers (ntfy/email) with:

  * DEDUP by (component, kind): the same condition does not re-send every poll.
  * EXPONENTIAL BACKOFF once a condition repeats: send immediately, then no sooner than 5m, 15m,
    60m, then hourly.
  * RESOLVE: when the condition clears, send exactly ONE "recovered" message and reset the state,
    so the next occurrence pages immediately again.

The durable ``LogNotifier`` row is NEVER wrapped by this -- every alert always lands in
``operator_alerts`` regardless of throttling. Throttling governs only what pages a human.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import structlog

log = structlog.get_logger()

# Minimum seconds between re-sends of a REPEATING condition, indexed by how many times it has
# already sent. First send is always immediate; thereafter: 5m, 15m, 60m, then hourly.
_BACKOFF_SCHEDULE_S = (300.0, 900.0, 3600.0)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class ThrottledNotifier:
    """Wrap a push notifier with dedup + exponential backoff, keyed by (component, kind)."""

    def __init__(self, inner, *, clock: Callable[[], datetime] | None = None) -> None:
        self._inner = inner
        self._clock = clock or _default_clock
        # key -> {"last_sent": datetime, "sends": int}
        self._state: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(alert: dict) -> tuple[str, str]:
        detail = alert.get("detail") or {}
        return (str(detail.get("component", "")), str(alert.get("kind", "")))

    def _required_wait(self, sends: int) -> float:
        return _BACKOFF_SCHEDULE_S[min(sends - 1, len(_BACKOFF_SCHEDULE_S) - 1)]

    def notify(self, alert: dict) -> None:
        key = self._key(alert)
        now = self._clock()
        st = self._state.get(key)
        if st is None:
            self._state[key] = {"last_sent": now, "sends": 1}
            self._inner.notify(alert)
            return
        elapsed = (now - st["last_sent"]).total_seconds()
        wait = self._required_wait(st["sends"])
        if elapsed >= wait:
            st["last_sent"] = now
            st["sends"] += 1
            self._inner.notify(alert)
        else:
            log.debug("alert_throttled", component=key[0], kind=key[1],
                      elapsed_s=round(elapsed, 1), required_wait_s=wait, sends=st["sends"])

    def resolve(self, *, component: str, kind: str) -> None:
        """Condition cleared: if it had been firing, send ONE recovered message and reset so the
        next occurrence pages immediately."""
        key = (str(component), str(kind))
        if key not in self._state:
            return
        del self._state[key]
        try:
            self._inner.notify({
                "kind": kind, "severity": "info",
                "message": f"{component} {kind} RECOVERED",
                "detail": {"component": component, "recovered": True},
            })
        except Exception as e:  # a recovered ping must never raise into the watchdog
            log.warning("recovered_notify_failed", component=component, kind=kind, error=str(e))
