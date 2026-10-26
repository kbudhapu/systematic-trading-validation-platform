"""Tests for live attribution ledger schema and logging hooks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.engine.attribution import (
    AttributionEnvironmentState,
    EXECUTION_PASSIVE,
    EXEC_DIRECTION_LONG_EXIT,
    LIQUIDITY_NORMAL,
    LIQUIDITY_THIN,
    TradeAttributionInput,
    classify_execution_direction,
    deduce_session_type,
    ensure_live_attribution_schema,
    fetch_attribution_rows,
    log_trade_attribution,
    resolve_execution_tactic,
    resolve_liquidity_state,
)
from src.router.risk_manager import (
    SESSION_CLOSING_IMBALANCE,
    SESSION_MIDDAY_DOLDRUMS,
    SESSION_OPENING_CROSS,
    ExecutionDriftDiagnostics,
)

ET = ZoneInfo("America/New_York")


def _diag(*, elevated: bool, critical: bool) -> ExecutionDriftDiagnostics:
    return ExecutionDriftDiagnostics(
        average_realized_slippage_pct=0.001,
        modeled_slippage_pct=0.0005,
        drift_multiple=2.0 if elevated else 1.0,
        elevated=elevated,
        critical=critical,
    )


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 45, SESSION_OPENING_CROSS),
        (12, 0, SESSION_MIDDAY_DOLDRUMS),
        (15, 30, SESSION_CLOSING_IMBALANCE),
    ],
)
def test_deduce_session_type(hour: int, minute: int, expected: str) -> None:
    ts = datetime(2026, 1, 15, hour, minute, tzinfo=ET)
    assert deduce_session_type(ts) == expected


def test_resolve_liquidity_state() -> None:
    assert resolve_liquidity_state(False) == LIQUIDITY_NORMAL
    assert resolve_liquidity_state(True) == LIQUIDITY_THIN


def test_resolve_execution_tactic_passive_on_elevated() -> None:
    tactic = resolve_execution_tactic(_diag(elevated=True, critical=False))
    assert tactic == EXECUTION_PASSIVE


def test_log_trade_attribution_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    ensure_live_attribution_schema(db_path)
    trade = TradeAttributionInput(
        trade_id="test-trade-1",
        timestamp=datetime(2026, 1, 15, 11, 0, tzinfo=ET),
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        side="sell",
        qty=10.0,
        pnl=125.50,
        expected_price=500.0,
        filled_price=501.25,
        slippage_pct=0.0025,
        bars_held=12,
        position_side_before="long",
    )
    env = AttributionEnvironmentState(
        regime_id="CALM_MR",
        thin_liquidity_active=False,
        execution_diagnostics=_diag(elevated=False, critical=False),
        champion_version_id=42,
        ai_policy_execution_state="PASSIVE_SHADOW",
        participation_cap_pct=0.95,
    )
    record = log_trade_attribution(trade, env, db_path=db_path)
    assert record.session_type == SESSION_MIDDAY_DOLDRUMS
    rows = fetch_attribution_rows(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        db_path=db_path,
    )
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "test-trade-1"
    assert rows[0]["pnl"] == pytest.approx(125.50)
    assert rows[0]["regime_id"] == "CALM_MR"
    assert rows[0]["champion_version_id"] == 42
    assert rows[0]["execution_direction_type"] == EXEC_DIRECTION_LONG_EXIT
    assert rows[0]["slip_direction_long_exit"] == pytest.approx(0.0025)
    assert rows[0]["slip_direction_long_entry"] is None


def test_log_trade_attribution_buckets_entry_slippage(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    trade = TradeAttributionInput(
        trade_id="entry-trade-1",
        timestamp=datetime(2026, 1, 15, 9, 45, tzinfo=ET),
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        side="buy",
        qty=10.0,
        pnl=0.0,
        expected_price=500.0,
        filled_price=500.4,
        slippage_pct=0.0008,
        position_side_before="flat",
    )
    env = AttributionEnvironmentState(regime_id="CALM_MR")
    record = log_trade_attribution(trade, env, db_path=db_path)
    assert record.execution_direction_type == classify_execution_direction(
        "buy", position_side_before="flat"
    )
    assert record.slip_direction_long_entry == pytest.approx(0.0008)
    assert record.slip_direction_long_exit is None


def test_log_trade_attribution_idempotent_update(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    trade = TradeAttributionInput(
        trade_id="dup-trade",
        timestamp=datetime(2026, 1, 15, 15, 15, tzinfo=ET),
        strategy_id="mean_reversion_spy",
        symbol="SPY",
        side="buy",
        qty=5.0,
        pnl=10.0,
        expected_price=600.0,
        filled_price=600.5,
        slippage_pct=0.0008,
    )
    env = AttributionEnvironmentState(regime_id="HIGH_VOL_MR")
    log_trade_attribution(trade, env, db_path=db_path)
    updated = TradeAttributionInput(
        trade_id="dup-trade",
        timestamp=trade.timestamp,
        strategy_id=trade.strategy_id,
        symbol=trade.symbol,
        side=trade.side,
        qty=trade.qty,
        pnl=12.0,
        expected_price=trade.expected_price,
        filled_price=trade.filled_price,
        slippage_pct=trade.slippage_pct,
    )
    log_trade_attribution(updated, env, db_path=db_path)
    rows = fetch_attribution_rows(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        db_path=db_path,
    )
    assert len(rows) == 1
    assert rows[0]["pnl"] == pytest.approx(12.0)
    assert rows[0]["session_type"] == SESSION_CLOSING_IMBALANCE
