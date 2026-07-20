"""Tests for capacity governor and frictional cost filter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.engine.capacity_governor import (
    CapacityGovernor,
    ensure_capacity_governor_schema,
)
from src.engine.cost_filter import (
    FrictionalCostFilter,
    ShortSideCostContext,
    ensure_frictional_cost_schema,
)


def test_enforce_daily_turnover_limits_caps_proposed_notional(tmp_path: Path) -> None:
    governor = CapacityGovernor(
        db_path=tmp_path / "vault.db",
        equity_turnover_multiplier=3.0,
        max_participation_rate=0.01,
    )
    equity = 50_000.0
    governor.update_account_equity(equity)
    governor.record_realized_turnover(
        "QQQ",
        140_000.0,
        session_date="2026-06-24",
        current_equity=equity,
    )
    decision = governor.enforce_daily_turnover_limits(
        "QQQ",
        20_000.0,
        equity,
        session_date="2026-06-24",
    )
    assert decision.capped is True
    assert decision.allowed_notional == pytest.approx(10_000.0)
    assert decision.cap_notional == pytest.approx(150_000.0)
    assert decision.blocked is False


def test_enforce_daily_turnover_blocks_when_cap_exhausted(tmp_path: Path) -> None:
    governor = CapacityGovernor(
        db_path=tmp_path / "vault.db",
        equity_turnover_multiplier=2.0,
    )
    equity = 25_000.0
    governor.update_account_equity(equity)
    governor.record_realized_turnover(
        "SPY",
        50_000.0,
        session_date="2026-06-24",
        current_equity=equity,
    )
    decision = governor.enforce_daily_turnover_limits(
        "SPY",
        5_000.0,
        equity,
        session_date="2026-06-24",
    )
    assert decision.blocked is True
    assert decision.breach is True
    assert decision.session_locked is True
    assert decision.allowed_notional == 0.0


def test_turnover_breach_locks_subsequent_orders(tmp_path: Path) -> None:
    governor = CapacityGovernor(
        db_path=tmp_path / "vault.db",
        equity_turnover_multiplier=1.0,
    )
    equity = 10_000.0
    governor.record_realized_turnover(
        "QQQ",
        12_000.0,
        session_date="2026-06-24",
        current_equity=equity,
    )
    assert governor.session_locked is True
    decision = governor.enforce_daily_turnover_limits(
        "QQQ",
        100.0,
        equity,
        session_date="2026-06-24",
    )
    assert decision.session_locked is True
    assert decision.blocked is True


def test_clamp_participation_rate_trims_large_orders() -> None:
    # Regression: equity integer semantics must be unchanged post-fix.
    governor = CapacityGovernor(max_participation_rate=0.01)
    volumes = [100_000.0] * 20
    decision = governor.clamp_participation_rate("QQQ", 10_000, volumes)
    assert decision.clamped is True
    assert decision.clamped_shares == 1_000
    assert decision.participation_rate == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# GD-R4 regression: fractional crypto orders must not be truncated to zero
# ---------------------------------------------------------------------------

def test_clamp_participation_rate_passes_through_fractional_btc_under_cap() -> None:
    # GD-R4: qty=0.47 BTC with high crypto volume — should pass through unclamped.
    # Pre-fix: int(0.47)==0 → zero_order_size drop. Post-fix: 0.47 passes through.
    governor = CapacityGovernor(max_participation_rate=0.01)
    # Crypto daily volume ~ 50,000 BTC equivalent units; 0.47 / 50_000 << 0.01
    volumes = [50_000.0] * 20
    decision = governor.clamp_participation_rate("BTC/USD", 0.47, volumes)
    assert decision.clamped_shares != 0, (
        "GD-R4: fractional BTC qty 0.47 was truncated to zero by int() coercion"
    )
    assert decision.clamped_shares == pytest.approx(0.47)
    assert decision.reason == "within_participation_limit"
    assert decision.clamped is False


def test_clamp_participation_rate_fractional_boundary_0999() -> None:
    # GD-R4 boundary: qty just below 1.0 must not be int-truncated to 0.
    governor = CapacityGovernor(max_participation_rate=0.01)
    volumes = [50_000.0] * 20
    decision = governor.clamp_participation_rate("BTC/USD", 0.999, volumes)
    assert decision.clamped_shares != 0, (
        "GD-R4 boundary: qty=0.999 was truncated to zero by int() coercion"
    )
    assert decision.clamped_shares == pytest.approx(0.999)
    assert decision.clamped is False


def test_clamp_participation_rate_fractional_btc_clamped_when_over_cap() -> None:
    # GD-R4 clamping: 5.0 BTC when max_participation_rate=0.01 and volume=100.0
    # max_shares = floor(100.0 * 0.01) = 1.0 share; 5.0 > 1.0 → clamped.
    governor = CapacityGovernor(max_participation_rate=0.01)
    volumes = [100.0] * 20
    decision = governor.clamp_participation_rate("BTC/USD", 5.0, volumes)
    assert decision.clamped is True
    assert decision.clamped_shares == pytest.approx(1.0)
    assert decision.reason == "order_shares_trimmed_to_participation_cap"


def test_apply_capacity_governor_passes_fractional_btc_order_to_broker() -> None:
    """End-to-end GD-R4: the full call chain from scaled_qty through
    clamp_participation_rate must not drop fractional qty.

    This traces the exact two-step path in _apply_capacity_governor_to_orders:
      scaled_qty = abs(order.qty) * scale        # → 0.47 (float)
      participation = governor.clamp_participation_rate("BTC/USD", scaled_qty, volumes)
      final_qty = float(participation.clamped_shares)  # must be 0.47, not 0.0

    Pre-fix: int(0.47) == 0 inside clamp_participation_rate → clamped_shares=0 → order dropped.
    Post-fix: 0.47 passes through → clamped_shares=0.47 → final_qty=0.47.
    """
    governor = CapacityGovernor(max_participation_rate=0.10)
    volumes = [50_000.0] * 20

    # Reproduce the exact two lines from _apply_capacity_governor_to_orders
    order_qty = 0.47
    scale = 1.0  # no turnover cap
    scaled_qty = abs(order_qty) * scale  # 0.47

    # Pre-fix path (what the bug does): int(scaled_qty) == 0 → drop
    # Post-fix path: pass scaled_qty directly as float
    participation = governor.clamp_participation_rate("BTC/USD", scaled_qty, volumes)
    final_qty = float(participation.clamped_shares)

    assert final_qty > 0.0, (
        f"GD-R4 end-to-end: final_qty={final_qty} after clamp_participation_rate — "
        "order would be dropped by _apply_capacity_governor_to_orders"
    )
    assert final_qty == pytest.approx(0.47)


def test_calculate_expected_vs_realized_impact_updates_coefficients(
    tmp_path: Path,
) -> None:
    governor = CapacityGovernor(db_path=tmp_path / "vault.db")
    first = governor.calculate_expected_vs_realized_impact(
        {
            "trade_id": "t-1",
            "symbol": "QQQ",
            "qty": 500.0,
            "side": "buy",
            "trailing_volume": 100_000.0,
            "expected_price": 500.0,
            "filled_price": 500.5,
            "markout_5m": 12.0,
        }
    )
    assert first.expected_impact_bps > 0.0
    assert first.realized_impact_bps == pytest.approx(12.0)

    second = governor.calculate_expected_vs_realized_impact(
        {
            "trade_id": "t-2",
            "symbol": "QQQ",
            "qty": 500.0,
            "side": "buy",
            "trailing_volume": 100_000.0,
            "expected_price": 500.0,
            "filled_price": 500.5,
            "markout_5m": 12.0,
        }
    )
    assert second.sqrt_coefficient != first.sqrt_coefficient or second.linear_coefficient != first.linear_coefficient

    with sqlite3.connect(tmp_path / "vault.db") as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM market_impact_ledger"
        ).fetchone()
    assert count is not None and int(count[0]) == 2


def test_apply_composite_cost_penalties_short_side_heavy(tmp_path: Path) -> None:
    filt = FrictionalCostFilter(db_path=tmp_path / "vault.db")
    baseline = filt.apply_composite_cost_penalties(
        composite_score=3.0,
        turnover_tax=0.2,
        execution_bps=8.0,
        borrow_fee_rate=0.02,
        short_recall_risk_premium=0.01,
        short_bias=0.9,
        persist=False,
    )
    stressed = filt.apply_composite_cost_penalties(
        composite_score=3.0,
        turnover_tax=0.2,
        execution_bps=8.0,
        borrow_fee_rate=0.08,
        short_recall_risk_premium=0.05,
        short_context=ShortSideCostContext(
            borrow_fee_rate=0.08,
            easy_to_borrow=False,
            thin_availability=True,
            recall_risk_score=0.8,
            borrow_stable=False,
        ),
        short_bias=1.4,
        persist=False,
    )
    assert stressed.adjusted_score < baseline.adjusted_score
    assert stressed.breakdown.short_operational_multiplier > 1.0


def test_asymmetric_cost_filter_penalizes_short_hold_exits(tmp_path) -> None:
    from src.engine.cost_filter import FrictionalCostFilter, ShortSideCostContext
    from src.engine.slippage_calibration import default_asymmetric_multipliers

    calibration_path = tmp_path / "session_slippage_multipliers.json"
    calibration_path.write_text(
        json.dumps(
            {
                "session_slippage_multipliers": default_asymmetric_multipliers(),
            }
        ),
        encoding="utf-8",
    )
    filt = FrictionalCostFilter(db_path=tmp_path / "vault.db", calibration_path=calibration_path)
    short_hold = filt.adjust_parameter_row(
        {
            "composite_score": 3.0,
            "full_trades": 120.0,
            "long_threshold_sigma": 1.5,
            "short_threshold_sigma": 2.0,
            "max_bars_in_trade": 8,
            "symbol": "QQQ",
        },
        execution_bps=5.0,
        base_slippage_pct=0.0005,
        short_context=ShortSideCostContext(recall_risk_score=0.7),
    )
    long_hold = filt.adjust_parameter_row(
        {
            "composite_score": 3.0,
            "full_trades": 120.0,
            "long_threshold_sigma": 1.5,
            "short_threshold_sigma": 2.0,
            "max_bars_in_trade": 40,
            "symbol": "QQQ",
        },
        execution_bps=5.0,
        base_slippage_pct=0.0005,
        short_context=ShortSideCostContext(recall_risk_score=0.7),
    )
    assert short_hold < long_hold


def test_schema_initialization(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    ensure_capacity_governor_schema(db_path)
    ensure_frictional_cost_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "capacity_daily_totals" in tables
    assert "market_impact_ledger" in tables
    assert "frictional_cost_adjustments" in tables
