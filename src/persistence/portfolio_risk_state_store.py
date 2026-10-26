from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DB_PATH
from src.persistence.ownership_guard import ensure_db_writable

PORTFOLIO_RISK_STATE_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_risk_strategy_state (
    strategy_id TEXT PRIMARY KEY,
    peak_equity REAL NOT NULL,
    trailing_drawdown_pct REAL NOT NULL,
    exit_only_mode INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    formula_version TEXT
);
"""

# GV2-2: refused/reset state is ARCHIVED, never deleted — the fossils stay auditable forever.
PORTFOLIO_RISK_STATE_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_risk_strategy_state_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    peak_equity REAL NOT NULL,
    trailing_drawdown_pct REAL NOT NULL,
    exit_only_mode INTEGER NOT NULL,
    updated_at TEXT,
    formula_version TEXT,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


def ensure_portfolio_risk_state_schema(db_path: Path = DB_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(PORTFOLIO_RISK_STATE_DDL)
        conn.executescript(PORTFOLIO_RISK_STATE_ARCHIVE_DDL)
        # GV2-3: additive upgrade for pre-stamp stores (the column simply reads NULL — which the
        # governor treats as a retired formula and refuses; exactly the intended fate of fossils).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(portfolio_risk_strategy_state)")}
        if "formula_version" not in cols:
            conn.execute(
                "ALTER TABLE portfolio_risk_strategy_state ADD COLUMN formula_version TEXT"
            )


def load_portfolio_risk_state(db_path: Path = DB_PATH) -> dict[str, Any] | None:
    ensure_portfolio_risk_state_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT strategy_id, peak_equity, trailing_drawdown_pct, exit_only_mode, formula_version
            FROM portfolio_risk_strategy_state
            ORDER BY strategy_id
            """
        ).fetchall()
    if not rows:
        return None
    strategies: dict[str, dict[str, Any]] = {}
    stamps: set[str | None] = set()
    for row in rows:
        strategies[str(row["strategy_id"])] = {
            "peak_equity": float(row["peak_equity"]),
            "trailing_drawdown_pct": float(row["trailing_drawdown_pct"]),
            "exit_only_mode": bool(int(row["exit_only_mode"] or 0)),
        }
        stamps.add(row["formula_version"])
    # A single coherent stamp or bust: mixed/absent stamps read as UNSTAMPED (None) so the
    # governor's refusal path treats partially-migrated state as retired, never half-trusts it.
    formula_version = stamps.pop() if len(stamps) == 1 else None
    return {"version": 2, "formula_version": formula_version, "strategies": strategies}


def persist_portfolio_risk_state(
    payload: dict[str, Any],
    *,
    db_path: Path = DB_PATH,
) -> None:
    ensure_portfolio_risk_state_schema(db_path)
    strategies = payload.get("strategies", {})
    if not isinstance(strategies, dict):
        raise ValueError("portfolio risk payload requires strategies mapping")
    formula_version = payload.get("formula_version")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM portfolio_risk_strategy_state")
            for strategy_id, row in strategies.items():
                if not isinstance(row, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO portfolio_risk_strategy_state (
                        strategy_id,
                        peak_equity,
                        trailing_drawdown_pct,
                        exit_only_mode,
                        updated_at,
                        formula_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(strategy_id),
                        float(row.get("peak_equity", 0.0)),
                        float(row.get("trailing_drawdown_pct", 0.0)),
                        1 if bool(row.get("exit_only_mode")) else 0,
                        now,
                        formula_version,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def archive_portfolio_risk_state(
    reason: str,
    *,
    db_path: Path = DB_PATH,
) -> int:
    """GV2-2: the auditable reset primitive — copy every current governor state row into the
    archive table (timestamp + reason), then clear the live table. Returns rows archived.
    NEVER a bare DELETE: the fossils stay queryable forever."""
    ensure_portfolio_risk_state_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                INSERT INTO portfolio_risk_strategy_state_archive (
                    strategy_id, peak_equity, trailing_drawdown_pct, exit_only_mode,
                    updated_at, formula_version, archived_at, reason
                )
                SELECT strategy_id, peak_equity, trailing_drawdown_pct, exit_only_mode,
                       updated_at, formula_version, ?, ?
                FROM portfolio_risk_strategy_state
                """,
                (now, reason),
            )
            archived = cur.rowcount
            conn.execute("DELETE FROM portfolio_risk_strategy_state")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return int(archived)


def payload_fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
