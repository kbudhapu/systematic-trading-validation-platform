"""
Live trade attribution ledger — schema, enrichment, and persistence hooks.

Writes decorated close events into the research vault for monthly reconciliation
and session slippage calibration.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable
from src.router.risk_manager import (
    AI_POLICY_PASSIVE_SHADOW,
    SESSION_CLOSING_IMBALANCE,
    SESSION_MIDDAY_DOLDRUMS,
    SESSION_OPENING_CROSS,
    THIN_LIQUIDITY_SUB_LABEL,
    ExecutionDriftDiagnostics,
    resolve_trading_session,
)
from src.engine.slippage_calibration import (
    EXEC_DIRECTION_LONG_ENTRY,
    EXEC_DIRECTION_LONG_EXIT,
    EXEC_DIRECTION_SHORT_ENTRY,
    EXEC_DIRECTION_SHORT_EXIT,
    EXECUTION_DIRECTION_TYPES,
)

LIQUIDITY_NORMAL = "NORMAL"
LIQUIDITY_THIN = "THIN"
EXECUTION_PASSIVE = "PASSIVE"
EXECUTION_AGGRESSIVE = "AGGRESSIVE"
EXECUTION_MARKET_FALLBACK = "MARKET_FALLBACK"

LIVE_ATTRIBUTION_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS live_attribution_ledger (
    attribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    pnl REAL NOT NULL,
    regime_id TEXT NOT NULL,
    session_type TEXT NOT NULL,
    liquidity_state TEXT NOT NULL,
    execution_tactic TEXT NOT NULL,
    champion_version_id INTEGER,
    ai_policy_execution_state TEXT NOT NULL,
    promotion_id TEXT,
    expected_price REAL,
    filled_price REAL,
    slippage_pct REAL,
    execution_direction_type TEXT,
    slip_direction_long_entry REAL,
    slip_direction_long_exit REAL,
    slip_direction_short_entry REAL,
    slip_direction_short_exit REAL,
    markout_5bar REAL,
    participation_cap_pct REAL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_attr_timestamp
    ON live_attribution_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_live_attr_symbol
    ON live_attribution_ledger(symbol);
"""

_LEDGER_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("execution_direction_type", "TEXT"),
    ("slip_direction_long_entry", "REAL"),
    ("slip_direction_long_exit", "REAL"),
    ("slip_direction_short_entry", "REAL"),
    ("slip_direction_short_exit", "REAL"),
)

PROMOTION_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS promotion_ledger (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    action_type TEXT NOT NULL,
    old_candidate_id TEXT,
    new_candidate_id TEXT,
    reason_code TEXT NOT NULL,
    composite_score_delta REAL NOT NULL
);
"""


@dataclass(frozen=True)
class TradeAttributionInput:
    """Close-event payload supplied by the execution pipeline."""

    trade_id: str
    timestamp: datetime
    strategy_id: str
    symbol: str
    side: str
    qty: float
    pnl: float
    expected_price: float
    filled_price: float
    slippage_pct: float
    bars_held: int = 0
    position_side_before: str | None = None
    execution_direction_type: str | None = None


@dataclass(frozen=True)
class AttributionEnvironmentState:
    """Runtime context captured at fill time."""

    regime_id: str
    thin_liquidity_active: bool = False
    execution_diagnostics: ExecutionDriftDiagnostics | None = None
    champion_version_id: int | None = None
    ai_policy_execution_state: str = AI_POLICY_PASSIVE_SHADOW
    promotion_id: str | None = None
    participation_cap_pct: float | None = None
    markout_5bar: float | None = None
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveAttributionRecord:
    """Row representation for live_attribution_ledger."""

    trade_id: str
    timestamp: str
    strategy_id: str
    symbol: str
    side: str
    qty: float
    pnl: float
    regime_id: str
    session_type: str
    liquidity_state: str
    execution_tactic: str
    champion_version_id: int | None
    ai_policy_execution_state: str
    promotion_id: str | None
    expected_price: float | None = None
    filled_price: float | None = None
    slippage_pct: float | None = None
    execution_direction_type: str | None = None
    slip_direction_long_entry: float | None = None
    slip_direction_long_exit: float | None = None
    slip_direction_short_entry: float | None = None
    slip_direction_short_exit: float | None = None
    markout_5bar: float | None = None
    participation_cap_pct: float | None = None
    metadata_json: str | None = None

    def to_insert_tuple(self) -> tuple[Any, ...]:
        return (
            self.trade_id,
            self.timestamp,
            self.strategy_id,
            self.symbol.upper(),
            self.side,
            float(self.qty),
            float(self.pnl),
            self.regime_id,
            self.session_type,
            self.liquidity_state,
            self.execution_tactic,
            self.champion_version_id,
            self.ai_policy_execution_state,
            self.promotion_id,
            self.expected_price,
            self.filled_price,
            self.slippage_pct,
            self.execution_direction_type,
            self.slip_direction_long_entry,
            self.slip_direction_long_exit,
            self.slip_direction_short_entry,
            self.slip_direction_short_exit,
            self.markout_5bar,
            self.participation_cap_pct,
            self.metadata_json,
        )


def ensure_live_attribution_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(LIVE_ATTRIBUTION_LEDGER_DDL)
        conn.executescript(PROMOTION_LEDGER_DDL)
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(live_attribution_ledger)")
        }
        for column_name, column_type in _LEDGER_COLUMN_MIGRATIONS:
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE live_attribution_ledger "
                    f"ADD COLUMN {column_name} {column_type}"
                )


def classify_execution_direction(
    order_side: str,
    *,
    position_side_before: str | None = None,
) -> str:
    """Map broker order side and prior position to a directional slippage bucket."""
    side = str(order_side).lower()
    prior = str(position_side_before or "flat").lower()

    if prior == "long":
        return EXEC_DIRECTION_LONG_EXIT if side == "sell" else EXEC_DIRECTION_LONG_ENTRY
    if prior == "short":
        return EXEC_DIRECTION_SHORT_EXIT if side == "buy" else EXEC_DIRECTION_SHORT_ENTRY
    return EXEC_DIRECTION_LONG_ENTRY if side == "buy" else EXEC_DIRECTION_SHORT_ENTRY


def bucket_directional_slippage(
    execution_direction_type: str,
    slippage_pct: float,
) -> dict[str, float | None]:
    """Assign realized slippage to a single directional ledger column."""
    buckets: dict[str, float | None] = {
        EXEC_DIRECTION_LONG_ENTRY: None,
        EXEC_DIRECTION_LONG_EXIT: None,
        EXEC_DIRECTION_SHORT_ENTRY: None,
        EXEC_DIRECTION_SHORT_EXIT: None,
    }
    if execution_direction_type in buckets:
        buckets[execution_direction_type] = float(slippage_pct)
    return buckets


def row_slippage_for_direction(row: Mapping[str, Any], direction: str) -> float | None:
    """Read directional slippage from a ledger row with legacy fallbacks."""
    column = f"slip_direction_{direction}"
    value = row.get(column)
    if value is not None:
        return float(value)
    if str(row.get("execution_direction_type") or "") == direction:
        slip = row.get("slippage_pct")
        if slip is not None:
            return float(slip)
    return None


def deduce_session_type(timestamp: datetime | None) -> str:
    """Map fill timestamp to institutional session bucket."""
    return resolve_trading_session(timestamp)


def resolve_liquidity_state(thin_liquidity_active: bool) -> str:
    return LIQUIDITY_THIN if thin_liquidity_active else LIQUIDITY_NORMAL


def resolve_execution_tactic(
    diagnostics: ExecutionDriftDiagnostics | None,
    *,
    broker_tactic: str | None = None,
) -> str:
    if broker_tactic == "passive_twap":
        return EXECUTION_PASSIVE
    if broker_tactic in ("aggressive_ioc_limit", "market_fallback"):
        return EXECUTION_AGGRESSIVE
    if diagnostics is None:
        return EXECUTION_AGGRESSIVE
    if diagnostics.elevated and not diagnostics.critical:
        return EXECUTION_PASSIVE
    if diagnostics.critical:
        return EXECUTION_MARKET_FALLBACK
    return EXECUTION_AGGRESSIVE


def resolve_promotion_id(
    symbol: str,
    regime_id: str,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> str | None:
    ensure_live_attribution_schema(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT log_id, new_candidate_id
                FROM promotion_ledger
                WHERE symbol = ? AND regime = ? AND action_type = 'PROMOTION'
                ORDER BY log_id DESC
                LIMIT 1
                """,
                (symbol.upper(), regime_id),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    log_id, candidate_id = row
    suffix = candidate_id or "unknown"
    return f"promo_{log_id}:{suffix}"


def resolve_champion_version_id(
    symbol: str,
    regime_id: str,
    fallback: int | None = None,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> int | None:
    if fallback is not None:
        return fallback
    ensure_live_attribution_schema(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT run_id
                FROM regime_champions
                WHERE symbol = ? AND regime = ?
                """,
                (symbol.upper(), regime_id),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def build_attribution_record(
    trade_data: TradeAttributionInput,
    environment_state: AttributionEnvironmentState,
    *,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> LiveAttributionRecord:
    session_type = deduce_session_type(trade_data.timestamp)
    liquidity_state = resolve_liquidity_state(environment_state.thin_liquidity_active)
    execution_tactic = resolve_execution_tactic(environment_state.execution_diagnostics)
    promotion_id = environment_state.promotion_id or resolve_promotion_id(
        trade_data.symbol,
        environment_state.regime_id,
        db_path=db_path,
    )
    champion_version_id = resolve_champion_version_id(
        trade_data.symbol,
        environment_state.regime_id,
        fallback=environment_state.champion_version_id,
        db_path=db_path,
    )
    metadata = {
        "bars_held": trade_data.bars_held,
        "session_labels": {
            "opening": SESSION_OPENING_CROSS,
            "midday": SESSION_MIDDAY_DOLDRUMS,
            "close": SESSION_CLOSING_IMBALANCE,
        },
        "thin_liquidity_sub_label": THIN_LIQUIDITY_SUB_LABEL
        if environment_state.thin_liquidity_active
        else None,
    }
    metadata.update(dict(environment_state.extra_metadata))
    if environment_state.execution_diagnostics is not None:
        diag = environment_state.execution_diagnostics
        metadata["execution_drift"] = {
            "average_realized_slippage_pct": diag.average_realized_slippage_pct,
            "modeled_slippage_pct": diag.modeled_slippage_pct,
            "drift_multiple": diag.drift_multiple,
            "elevated": diag.elevated,
            "critical": diag.critical,
        }

    execution_direction = (
        trade_data.execution_direction_type
        or classify_execution_direction(
            trade_data.side,
            position_side_before=trade_data.position_side_before,
        )
    )
    directional_slippage = bucket_directional_slippage(
        execution_direction,
        float(trade_data.slippage_pct),
    )
    metadata["execution_direction_type"] = execution_direction

    ts = trade_data.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)

    return LiveAttributionRecord(
        trade_id=trade_data.trade_id,
        timestamp=ts.isoformat(),
        strategy_id=trade_data.strategy_id,
        symbol=trade_data.symbol.upper(),
        side=trade_data.side,
        qty=float(trade_data.qty),
        pnl=float(trade_data.pnl),
        regime_id=environment_state.regime_id,
        session_type=session_type,
        liquidity_state=liquidity_state,
        execution_tactic=execution_tactic,
        champion_version_id=champion_version_id,
        ai_policy_execution_state=environment_state.ai_policy_execution_state,
        promotion_id=promotion_id,
        expected_price=float(trade_data.expected_price),
        filled_price=float(trade_data.filled_price),
        slippage_pct=float(trade_data.slippage_pct),
        execution_direction_type=execution_direction,
        slip_direction_long_entry=directional_slippage[EXEC_DIRECTION_LONG_ENTRY],
        slip_direction_long_exit=directional_slippage[EXEC_DIRECTION_LONG_EXIT],
        slip_direction_short_entry=directional_slippage[EXEC_DIRECTION_SHORT_ENTRY],
        slip_direction_short_exit=directional_slippage[EXEC_DIRECTION_SHORT_EXIT],
        markout_5bar=environment_state.markout_5bar,
        participation_cap_pct=environment_state.participation_cap_pct,
        metadata_json=json.dumps(metadata, separators=(",", ":")),
    )


def log_trade_attribution(
    trade_data: TradeAttributionInput,
    environment_state: AttributionEnvironmentState,
    *,
    db_path: Path = RESEARCH_VAULT_PATH,
    use_async_writer: bool = True,
) -> LiveAttributionRecord:
    """
    Persist a decorated trade close into the live attribution ledger.

    Session type is derived from ``trade_data.timestamp`` via ET session buckets.
    """
    ensure_live_attribution_schema(db_path)
    record = build_attribution_record(trade_data, environment_state, db_path=db_path)
    if use_async_writer:
        from src.persistence.db_queue import enqueue_trade_attribution, get_async_db_writer

        writer = get_async_db_writer()
        if writer.is_running:
            enqueue_trade_attribution(record, db_path=str(db_path))
            return record

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO live_attribution_ledger (
                trade_id, timestamp, strategy_id, symbol, side, qty, pnl,
                regime_id, session_type, liquidity_state, execution_tactic,
                champion_version_id, ai_policy_execution_state, promotion_id,
                expected_price, filled_price, slippage_pct, execution_direction_type,
                slip_direction_long_entry, slip_direction_long_exit,
                slip_direction_short_entry, slip_direction_short_exit,
                markout_5bar, participation_cap_pct, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                pnl = excluded.pnl,
                slippage_pct = excluded.slippage_pct,
                execution_direction_type = excluded.execution_direction_type,
                slip_direction_long_entry = excluded.slip_direction_long_entry,
                slip_direction_long_exit = excluded.slip_direction_long_exit,
                slip_direction_short_entry = excluded.slip_direction_short_entry,
                slip_direction_short_exit = excluded.slip_direction_short_exit,
                markout_5bar = COALESCE(excluded.markout_5bar, live_attribution_ledger.markout_5bar),
                metadata_json = excluded.metadata_json
            """,
            record.to_insert_tuple(),
        )
    return record


def fetch_attribution_rows(
    *,
    start: datetime,
    end: datetime | None = None,
    symbol: str | None = None,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> list[dict[str, Any]]:
    ensure_live_attribution_schema(db_path)
    end = end or datetime.now(timezone.utc)
    start_iso = start.astimezone(timezone.utc).isoformat()
    end_iso = end.astimezone(timezone.utc).isoformat()
    query = """
        SELECT *
        FROM live_attribution_ledger
        WHERE timestamp >= ? AND timestamp < ?
    """
    params: list[Any] = [start_iso, end_iso]
    if symbol is not None:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY timestamp ASC"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def record_to_dict(record: LiveAttributionRecord) -> dict[str, Any]:
    return asdict(record)
