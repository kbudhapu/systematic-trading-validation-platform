from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DB_PATH
from src.persistence.ownership_guard import ensure_db_writable

MAX_CYCLE_METRICS_ROWS = 50_000

CYCLE_METRICS_DDL = """
CREATE TABLE IF NOT EXISTS cycle_metrics_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    phase_a_ms REAL NOT NULL,
    phase_b_ms REAL NOT NULL,
    phase_c_ms REAL NOT NULL,
    total_cycle_ms REAL NOT NULL,
    sieve_backlog_qty INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cycle_metrics_recorded_at
    ON cycle_metrics_log(recorded_at);
"""


def ensure_cycle_metrics_schema(db_path: Path = DB_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(CYCLE_METRICS_DDL)


def persist_cycle_metrics_row(
    *,
    phase_a_ms: float,
    phase_b_ms: float,
    phase_c_ms: float,
    total_cycle_ms: float,
    sieve_backlog_qty: int,
    db_path: Path = DB_PATH,
) -> None:
    ensure_cycle_metrics_schema(db_path)
    recorded_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO cycle_metrics_log (
                    recorded_at,
                    phase_a_ms,
                    phase_b_ms,
                    phase_c_ms,
                    total_cycle_ms,
                    sieve_backlog_qty
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    float(phase_a_ms),
                    float(phase_b_ms),
                    float(phase_c_ms),
                    float(total_cycle_ms),
                    int(sieve_backlog_qty),
                ),
            )
            conn.execute(
                """
                DELETE FROM cycle_metrics_log
                WHERE id NOT IN (
                    SELECT id FROM cycle_metrics_log
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (MAX_CYCLE_METRICS_ROWS,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def load_recent_cycle_metrics(
    *,
    limit: int = 100,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    ensure_cycle_metrics_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT recorded_at, phase_a_ms, phase_b_ms, phase_c_ms,
                   total_cycle_ms, sieve_backlog_qty
            FROM cycle_metrics_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(int(limit), 1),),
        ).fetchall()
    return [_row_to_metric(row) for row in rows]


def load_session_cycle_metrics(
    *,
    db_path: Path = DB_PATH,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    ensure_cycle_metrics_schema(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("recorded_at >= ?")
        params.append(since)
    if until:
        clauses.append("recorded_at <= ?")
        params.append(until)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT recorded_at, phase_a_ms, phase_b_ms, phase_c_ms,
                   total_cycle_ms, sieve_backlog_qty
            FROM cycle_metrics_log
            {where_sql}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
    return [_row_to_metric(row) for row in rows]


def _row_to_metric(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "recorded_at": str(row["recorded_at"]),
        "phase_a_ms": float(row["phase_a_ms"]),
        "phase_b_ms": float(row["phase_b_ms"]),
        "phase_c_ms": float(row["phase_c_ms"]),
        "total_cycle_ms": float(row["total_cycle_ms"]),
        "sieve_backlog_qty": int(row["sieve_backlog_qty"]),
    }
