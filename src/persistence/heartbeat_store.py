"""SQLite persistence for main-loop liveness heartbeats (garage G1.5).

Append-only: the live loop writes one heartbeat per cycle (via the existing
AsyncDBWriter, WriteKind.HEARTBEAT), and the watchdog reads the latest beat's age
to detect a stalled loop. Rows are only ever INSERTed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from src.persistence.append_only import AppendOnlyStore

_DDL = """
CREATE TABLE IF NOT EXISTS loop_heartbeats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    component   TEXT    NOT NULL,
    beat_utc    TEXT    NOT NULL,
    created_utc TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_heartbeats_component ON loop_heartbeats(component);
"""


def ensure_heartbeat_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_DDL)
        # R3 (INCIDENT-20260722): additive status payload carried alongside the beat -- lets the alert
        # layer distinguish DEAD (no fresh beat) from HALTED-BY-DESIGN (fresh beat, gate blocked) and
        # see the consumption loop's last_cycle_ts even when the (decoupled) heartbeat task is alive.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(loop_heartbeats)")}
        if "status_json" not in cols:
            conn.execute("ALTER TABLE loop_heartbeats ADD COLUMN status_json TEXT")


# E3/R2: composed on the shared AppendOnlyStore base (DDL + rows byte-identical).
_STORE = AppendOnlyStore(
    table="loop_heartbeats", ddl=_DDL,
    insert_columns=["component", "beat_utc", "created_utc"], permanent=True)


def persist_heartbeats(payloads: Sequence[dict], db_path: str | Path) -> None:
    """Append-only INSERT of heartbeat rows. Each payload: {component, beat_utc?}."""
    if not payloads:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"component": str(p.get("component", "main_loop")),
             "beat_utc": str(p.get("beat_utc", now))} for p in payloads]
    _STORE.append(rows, db_path)


def write_heartbeat_sync(component: str, db_path: str | Path,
                         *, beat_utc: datetime | None = None) -> None:
    ts = (beat_utc or datetime.now(timezone.utc)).isoformat()
    persist_heartbeats([{"component": component, "beat_utc": ts}], db_path)


def read_latest_heartbeat(component: str, db_path: str | Path) -> datetime | None:
    """Latest beat timestamp for a component, or None if never written."""
    ensure_heartbeat_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(beat_utc) FROM loop_heartbeats WHERE component = ?",
            (str(component),),
        ).fetchone()
    if not row or row[0] is None:
        return None
    return datetime.fromisoformat(row[0])


def write_heartbeat_status(component: str, db_path: str | Path, *,
                           status: dict | None = None,
                           beat_utc: datetime | None = None) -> None:
    """R3: append one heartbeat row carrying a status payload (gate / escalation_level / last_cycle_ts).
    Append-only; a plain INSERT so the (decoupled) heartbeat emitter needs no AsyncDBWriter."""
    import json
    ensure_heartbeat_schema(db_path)
    now = datetime.now(timezone.utc)
    ts = (beat_utc or now).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO loop_heartbeats (component, beat_utc, created_utc, status_json) VALUES (?,?,?,?)",
            (str(component), ts, now.isoformat(), json.dumps(status) if status is not None else None))


def read_latest_heartbeat_status(component: str, db_path: str | Path) -> tuple[datetime, dict] | None:
    """R3: (latest beat_utc, status dict) for a component. status is {} if the row carried none."""
    import json
    ensure_heartbeat_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT beat_utc, status_json FROM loop_heartbeats WHERE component=? "
            "ORDER BY beat_utc DESC, id DESC LIMIT 1", (str(component),)).fetchone()
    if not row or row[0] is None:
        return None
    status = {}
    if row[1]:
        try:
            status = json.loads(row[1])
        except (ValueError, TypeError):
            status = {}
    return datetime.fromisoformat(row[0]), status
