"""SQLite persistence for the PSD trial-accounting ledger (doctrine S8).

Append-only: rows are only ever INSERTed (no UPDATE / DELETE path exists here),
so cumulative trial counts can never be silently rewritten. Used by the existing
AsyncDBWriter drain (`WriteKind.TRIAL_LEDGER`) -- no new writer thread.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS psd_trial_ledger (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id                TEXT    NOT NULL,
    queue_id              TEXT    NOT NULL,
    grid_points_evaluated INTEGER NOT NULL,
    timeframes_evaluated  INTEGER NOT NULL,
    objective_variants    INTEGER NOT NULL DEFAULT 1,
    n_trials_this_entry   INTEGER NOT NULL,
    created_at            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_psd_trial_ledger_leg ON psd_trial_ledger(leg_id);
"""


def ensure_trial_ledger_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_DDL)


def _entry_trials(payload: dict) -> int:
    return (int(payload["grid_points_evaluated"])
            * int(payload["timeframes_evaluated"])
            * int(payload.get("objective_variants", 1)))


def persist_trial_rows(payloads: Sequence[dict], db_path: str | Path) -> None:
    """Append-only INSERT of one or more ledger entries. Never updates."""
    if not payloads:
        return
    ensure_trial_ledger_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (str(p["leg_id"]), str(p["queue_id"]),
         int(p["grid_points_evaluated"]), int(p["timeframes_evaluated"]),
         int(p.get("objective_variants", 1)), _entry_trials(p), now)
        for p in payloads
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO psd_trial_ledger
               (leg_id, queue_id, grid_points_evaluated, timeframes_evaluated,
                objective_variants, n_trials_this_entry, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def read_cumulative_n_trials(leg_id: str, db_path: str | Path) -> int:
    """Cumulative N_trials for a leg = SUM(grid x timeframes x objective_variants)
    across all its ledger entries (doctrine S8, for the DSR call)."""
    ensure_trial_ledger_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(n_trials_this_entry), 0) FROM psd_trial_ledger WHERE leg_id = ?",
            (str(leg_id),),
        ).fetchone()
    return int(row[0]) if row else 0


def read_entry_count(leg_id: str, db_path: str | Path) -> int:
    ensure_trial_ledger_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM psd_trial_ledger WHERE leg_id = ?", (str(leg_id),)
        ).fetchone()
    return int(row[0]) if row else 0
