"""
Durable governance degradation state and pending-order staging in trading.db.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DB_PATH
from src.persistence.ownership_guard import ensure_db_writable

GOVERNANCE_STATE_KEY = "degradation"

GOVERNANCE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS governance_state (
    state_key TEXT PRIMARY KEY,
    degradation_mode TEXT NOT NULL,
    trigger_reason TEXT NOT NULL DEFAULT '',
    entered_at TEXT,
    flatten_latched INTEGER NOT NULL DEFAULT 0,
    recovery_streak INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

PENDING_ORDER_STAGE_DDL = """
CREATE TABLE IF NOT EXISTS pending_order_stage (
    order_key TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    broker_order_id TEXT,
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_order_stage_symbol
    ON pending_order_stage(symbol);
"""


def ensure_governance_state_schema(db_path: Path = DB_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(GOVERNANCE_STATE_DDL)
        conn.executescript(PENDING_ORDER_STAGE_DDL)


@dataclass(frozen=True)
class PersistedGovernanceState:
    degradation_mode: str
    trigger_reason: str
    entered_at: str | None
    flatten_latched: bool
    recovery_streak: int


def load_governance_state(db_path: Path = DB_PATH) -> PersistedGovernanceState | None:
    ensure_governance_state_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT degradation_mode, trigger_reason, entered_at,
                   flatten_latched, recovery_streak
            FROM governance_state
            WHERE state_key = ?
            """,
            (GOVERNANCE_STATE_KEY,),
        ).fetchone()
    if row is None:
        return None
    return PersistedGovernanceState(
        degradation_mode=str(row["degradation_mode"]),
        trigger_reason=str(row["trigger_reason"] or ""),
        entered_at=row["entered_at"],
        flatten_latched=bool(int(row["flatten_latched"] or 0)),
        recovery_streak=int(row["recovery_streak"] or 0),
    )


def persist_governance_state(
    *,
    degradation_mode: str,
    trigger_reason: str,
    entered_at: str | None,
    flatten_latched: bool,
    recovery_streak: int,
    db_path: Path = DB_PATH,
) -> None:
    ensure_governance_state_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO governance_state (
                state_key, degradation_mode, trigger_reason, entered_at,
                flatten_latched, recovery_streak, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                degradation_mode = excluded.degradation_mode,
                trigger_reason = excluded.trigger_reason,
                entered_at = excluded.entered_at,
                flatten_latched = excluded.flatten_latched,
                recovery_streak = excluded.recovery_streak,
                updated_at = excluded.updated_at
            """,
            (
                GOVERNANCE_STATE_KEY,
                degradation_mode,
                trigger_reason,
                entered_at,
                1 if flatten_latched else 0,
                max(int(recovery_streak), 0),
                now,
            ),
        )


class PendingOrderStore:
    """SQLite-backed pending order registry for crash-safe reconciliation."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        ensure_governance_state_schema(db_path)

    def load_all(self) -> dict[str, str | None]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT order_key, broker_order_id
                FROM pending_order_stage
                """
            ).fetchall()
        return {
            str(row["order_key"]): (
                str(row["broker_order_id"]) if row["broker_order_id"] is not None else None
            )
            for row in rows
        }

    def stage(
        self,
        order_key: str,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        broker_order_id: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ensure_db_writable(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pending_order_stage (
                    order_key, strategy_id, symbol, side, broker_order_id, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_key) DO UPDATE SET
                    broker_order_id = excluded.broker_order_id,
                    submitted_at = excluded.submitted_at
                """,
                (
                    order_key,
                    strategy_id,
                    symbol.upper(),
                    side,
                    broker_order_id,
                    now,
                ),
            )

    def pop(self, order_key: str) -> str | None:
        ensure_db_writable(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT broker_order_id
                FROM pending_order_stage
                WHERE order_key = ?
                """,
                (order_key,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM pending_order_stage WHERE order_key = ?",
                (order_key,),
            )
        broker_id = row["broker_order_id"]
        return str(broker_id) if broker_id is not None else None

    def clear_prefix(self, strategy_id: str, symbol: str) -> int:
        prefix = f"{strategy_id}:{symbol.upper()}:"
        ensure_db_writable(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                DELETE FROM pending_order_stage
                WHERE order_key LIKE ?
                """,
                (f"{prefix}%",),
            )
        return int(cur.rowcount)

    def clear_all(self) -> int:
        ensure_db_writable(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM pending_order_stage")
        return int(cur.rowcount)

    def reconcile_with_broker_open_orders(
        self,
        broker_open_order_ids: set[str],
    ) -> dict[str, Any]:
        staged = self.load_all()
        stale_keys: list[str] = []
        for key, broker_id in staged.items():
            if broker_id is None or broker_id not in broker_open_order_ids:
                stale_keys.append(key)
        removed = 0
        if stale_keys:
            placeholders = ",".join("?" for _ in stale_keys)
            ensure_db_writable(self.db_path)
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    f"DELETE FROM pending_order_stage WHERE order_key IN ({placeholders})",
                    stale_keys,
                )
                removed = int(cur.rowcount)
        return {
            "staged_count": len(staged),
            "broker_open_count": len(broker_open_order_ids),
            "stale_removed": removed,
        }
