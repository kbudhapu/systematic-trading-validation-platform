"""AG2/AG3/AG4 — a HARD, SHARED outbound cap for the ntfy push channel.

ntfy.sh rate-limits by IP, and BOTH the soak and the collector publish from the SAME droplet, so a
per-process cap does nothing: the soak's degrade storm ate the collector's dead-man ping budget
(July 7). The counter therefore lives in a SHARED SQLite DB that both processes open -- chosen over
a lock-file because SQLite gives atomic counting under concurrent writers (WAL + busy_timeout) and
because the stats must be QUERYABLE for the daily ping and PRODUCTION_TRUTH.md.

Lanes (AG3): the daily liveness ping and CRITICAL/urgent pages draw from a PRIORITY lane that
routine chatter can never exhaust -- the dead-man must never be starved. On a 429 (AG4) the routine
lane backs off HARD for an hour and it is logged as a CHANNEL INCIDENT, because a Firebase 429 is a
~10-minute IP ban with NO indication to the user: a 200 does not mean the phone rang, so we must
never approach the limit. The durable operator_alerts row is ALWAYS written upstream; suppression
here affects the PHONE, never the RECORD.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import structlog

log = structlog.get_logger()

# A healthy system sends ~1/day (the liveness ping). These are floors well above that but far below
# ntfy's limits, so we never approach the Firebase ban.
MAX_ROUTINE_PER_HOUR = 8
MAX_ROUTINE_PER_DAY = 50
PRIORITY_PER_HOUR = 4                 # reserved lane the routine chatter cannot touch (AG3)
BACKOFF_AFTER_429_SECONDS = 3600.0    # AG4: an HOUR, not five minutes

_PRIORITY_KINDS = frozenset({"daily_liveness"})
_PRIORITY_SEVERITIES = frozenset({"urgent", "critical", "high"})


def _default_cap_db() -> str:
    try:
        from src.persistence.db import RESEARCH_VAULT_PATH
        return str(Path(RESEARCH_VAULT_PATH).parent / "alert_channel.db")
    except Exception:
        return "data/alert_channel.db"


DEFAULT_CAP_DB = _default_cap_db()


def _is_priority(kind: object, severity: object) -> bool:
    return str(kind or "") in _PRIORITY_KINDS or str(severity or "").lower() in _PRIORITY_SEVERITIES


class OutboundCap:
    """Shared, hard outbound cap with a reserved priority lane and a post-429 backoff."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        max_routine_per_hour: int = MAX_ROUTINE_PER_HOUR,
        max_routine_per_day: int = MAX_ROUTINE_PER_DAY,
        priority_per_hour: int = PRIORITY_PER_HOUR,
        backoff_s: float = BACKOFF_AFTER_429_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = str(db_path if db_path is not None else DEFAULT_CAP_DB)
        self._h = max_routine_per_hour
        self._d = max_routine_per_day
        self._p = priority_per_hour
        self._backoff = backoff_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _init_db(self) -> None:
        try:
            with self._conn() as c:
                c.execute("CREATE TABLE IF NOT EXISTS alert_channel_sends "
                          "(sent_utc TEXT NOT NULL, kind TEXT, lane TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS alert_channel_incidents "
                          "(at_utc TEXT NOT NULL, kind TEXT)")
        except Exception as e:
            log.warning("outbound_cap_init_failed", error=str(e))

    @staticmethod
    def _count_since(c: sqlite3.Connection, since_iso: str, lane: str | None = None) -> int:
        if lane is not None:
            return c.execute(
                "SELECT COUNT(*) FROM alert_channel_sends WHERE sent_utc >= ? AND lane = ?",
                (since_iso, lane),
            ).fetchone()[0]
        return c.execute(
            "SELECT COUNT(*) FROM alert_channel_sends WHERE sent_utc >= ?", (since_iso,)
        ).fetchone()[0]

    def allow(self, kind: object, severity: object) -> tuple[bool, str]:
        """Decide whether this alert may leave the process, and RECORD it if so. Fails OPEN on any DB
        error -- a cap bug must never silence the operator's only channel."""
        now = self._clock()
        hour_ago = (now - timedelta(hours=1)).isoformat()
        day_ago = (now - timedelta(days=1)).isoformat()
        priority = _is_priority(kind, severity)
        try:
            with self._conn() as c:
                last_429 = c.execute(
                    "SELECT MAX(at_utc) FROM alert_channel_incidents WHERE kind='429'"
                ).fetchone()[0]
                # AG4: hard backoff after a 429 -- but NEVER starve the priority lane (the dead-man).
                if last_429 and not priority:
                    if (now - datetime.fromisoformat(last_429)).total_seconds() < self._backoff:
                        return False, "channel_backoff_after_429"
                if priority:
                    if self._count_since(c, hour_ago, "priority") >= self._p:
                        return False, "priority_hourly_cap"
                else:
                    if self._count_since(c, hour_ago, "routine") >= self._h:
                        return False, "routine_hourly_cap"
                    if self._count_since(c, day_ago, "routine") >= self._d:
                        return False, "routine_daily_cap"
                c.execute(
                    "INSERT INTO alert_channel_sends (sent_utc, kind, lane) VALUES (?,?,?)",
                    (now.isoformat(), str(kind or ""), "priority" if priority else "routine"),
                )
                return True, "priority" if priority else "routine"
        except Exception as e:
            log.warning("outbound_cap_error_fail_open", error=str(e))
            return True, "cap_error_fail_open"

    def note_429(self, kind: object = None) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO alert_channel_incidents (at_utc, kind) VALUES (?, '429')",
                    (self._clock().isoformat(),),
                )
        except Exception as e:
            log.warning("outbound_cap_incident_write_failed", error=str(e))

    def stats(self) -> dict:
        """AG4b: the alert channel must report its OWN health -- it is the one component whose failure
        is invisible by construction (a 200 does not mean the phone rang)."""
        now = self._clock()
        try:
            with self._conn() as c:
                return {
                    "sends_last_hour": self._count_since(c, (now - timedelta(hours=1)).isoformat()),
                    "sends_today": self._count_since(c, (now - timedelta(days=1)).isoformat()),
                    "cap_routine_per_hour": self._h,
                    "cap_routine_per_day": self._d,
                    "last_429_at": c.execute(
                        "SELECT MAX(at_utc) FROM alert_channel_incidents WHERE kind='429'"
                    ).fetchone()[0],
                }
        except Exception as e:
            return {"error": str(e)}
