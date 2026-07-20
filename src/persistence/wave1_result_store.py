"""Append-only Wave-1 experiment-result store in the research vault (research_vault.db).

Mirrors the trial-ledger discipline: the same INSERT the AsyncDBWriter drain would perform, exposed
synchronously for standalone research runners. Every trial writes its LEDGER row (attempt) before its
RESULT row here. Read-only for the verdict report (Task 5).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.persistence.db import RESEARCH_VAULT_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS wave1_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    trial_key      TEXT NOT NULL,
    family         TEXT NOT NULL,
    hypothesis     TEXT,
    instrument     TEXT,
    n_events       INTEGER,
    triage_verdict TEXT NOT NULL,
    p_value        REAL,
    seed           INTEGER,
    statistics_json TEXT NOT NULL,
    created_utc    TEXT NOT NULL
);
"""


def ensure_wave1_schema(db_path: str | Path = RESEARCH_VAULT_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_DDL)


def persist_wave1_result(row: dict, db_path: str | Path = RESEARCH_VAULT_PATH) -> int:
    """Append one trial result. Returns its row id."""
    ensure_wave1_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO wave1_results
               (experiment_id, trial_key, family, hypothesis, instrument, n_events,
                triage_verdict, p_value, seed, statistics_json, created_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (row["experiment_id"], row["trial_key"], row["family"], row.get("hypothesis"),
             row.get("instrument"), row.get("n_events"), row["triage_verdict"],
             row.get("p_value"), row.get("seed"), json.dumps(row["statistics"]),
             datetime.now(timezone.utc).isoformat()),
        )
        return int(cur.lastrowid)


def read_wave1_results(experiment_id: str | None = None,
                       db_path: str | Path = RESEARCH_VAULT_PATH) -> list[dict]:
    ensure_wave1_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if experiment_id:
            rows = conn.execute("SELECT * FROM wave1_results WHERE experiment_id=? ORDER BY id",
                                (experiment_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM wave1_results ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["statistics"] = json.loads(d["statistics_json"])
        out.append(d)
    return out
