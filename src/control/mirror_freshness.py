"""Mirror-freshness marker (P2.3) — makes any report know its own data age.

This week a *stale* Ryzen copy of ``research_vault.db`` (frozen at 2026-07-07T17:05Z
while the droplet kept running) was read as "the collector is dead" — a false P0 that
was repeated for five turns. The root gap was not the data; it was that every report
surfaced the data *without surfacing how old it was*.

Fix: a freshness signal that any diagnostic/reporting tool reads and surfaces as
``as of {ts}, age {N}`` before printing mirror-derived numbers, and that flags
staleness explicitly rather than silently returning old numbers.

Design (two sources, robust by construction):
  1. An explicit ``sync_meta.last_synced_utc`` marker, stamped by :func:`record_sync`
     whenever a real refresh/sync completes. This is the "own small table / single
     row updated on every successful sync" the remediation asked for.
  2. A FALLBACK to the newest collector heartbeat (``MAX(loop_heartbeats.beat_utc)``),
     which the live collector already writes every cycle. This means freshness is
     knowable on *any* copy of the DB **today** — before any sync job is wired and
     without touching the live write path (no redeploy needed for correctness). On a
     stale mirror the newest heartbeat is frozen, so the age is reported truthfully.

If neither source exists the age is UNKNOWN, and unknown is treated as stale — a
report must never silently trust a DB whose age it cannot establish.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.persistence.heartbeat_store import read_latest_heartbeat

# The live collector beats every 5 min. A mirror an hour behind is unambiguously
# stale, while an hour is comfortably above the 5-min cadence and any intraday
# refresh, so a healthy live DB never false-flags. Callers that know their own sync
# cadence should pass ``stale_after_seconds = 2 * their_interval`` (the remediation's
# proposed rule); this default is the safe catch-all for the human review report.
DEFAULT_STALE_AFTER_S = 3600

_SYNC_META_DDL = """
CREATE TABLE IF NOT EXISTS sync_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);
"""
_LAST_SYNCED_KEY = "last_synced_utc"


def ensure_sync_meta_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_SYNC_META_DDL)


def record_sync(db_path: str | Path, *, now: datetime | None = None) -> str:
    """Stamp ``last_synced_utc`` = now. Call at the end of a successful refresh/sync.

    Returns the ISO timestamp written. Idempotent upsert on the single marker row.
    """
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    ensure_sync_meta_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sync_meta (key, value, updated_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_utc=excluded.updated_utc",
            (_LAST_SYNCED_KEY, ts, ts),
        )
    return ts


def read_last_synced(db_path: str | Path) -> datetime | None:
    """The explicit ``last_synced_utc`` marker, or None if never stamped / no table."""
    p = Path(db_path)
    if not p.exists():
        return None
    with sqlite3.connect(p) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_meta'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key=?", (_LAST_SYNCED_KEY,)
        ).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


@dataclass(frozen=True)
class Freshness:
    """Age of the data in a mirror DB. ``as_of`` is None when age is unknowable."""

    as_of: datetime | None
    age_seconds: float | None
    is_stale: bool
    source: str  # "sync_meta" | "heartbeat" | "none"
    threshold_seconds: int

    def banner(self) -> str:
        """One-line prefix a report puts above any mirror-derived numbers."""
        if self.as_of is None:
            return "data freshness: UNKNOWN — no sync marker and no heartbeat; treat as STALE"
        age = _humanize(self.age_seconds or 0.0)
        tag = "  ** STALE **" if self.is_stale else ""
        return f"data as of {self.as_of.isoformat()} (age {age}, via {self.source}){tag}"


def _humanize(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def mirror_freshness(
    db_path: str | Path,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_S,
    heartbeat_component: str = "sip_collector",
    now: datetime | None = None,
) -> Freshness:
    """Establish how old the data in ``db_path`` is.

    Prefers the explicit ``sync_meta`` marker; falls back to the newest collector
    heartbeat; if neither exists, age is UNKNOWN and ``is_stale`` is True.
    """
    ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    as_of = read_last_synced(db_path)
    source = "sync_meta"
    if as_of is None:
        if Path(db_path).exists():
            as_of = read_latest_heartbeat(heartbeat_component, db_path)
        source = "heartbeat" if as_of is not None else "none"

    if as_of is None:
        return Freshness(None, None, True, "none", stale_after_seconds)

    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    age = (ref - as_of).total_seconds()
    return Freshness(
        as_of=as_of,
        age_seconds=age,
        is_stale=age > stale_after_seconds,
        source=source,
        threshold_seconds=stale_after_seconds,
    )
