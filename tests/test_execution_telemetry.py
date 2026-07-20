"""Tests for execution adaptor and broker telemetry gate."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.config import RiskConfig
from src.engine.execution_adaptor import (
    ExecutionFeedbackLoop,
    RoutingPosture,
    classify_order_size_bucket,
    compute_book_pressure,
    ensure_execution_feedback_schema,
)
from src.engine.telemetry_gate import BrokerTelemetryBridge, CapitalGate
from src.models import Account, Side
from src.router.risk_manager import RiskManager

ET = ZoneInfo("America/New_York")


def _fill_payload(**overrides) -> dict:
    base = {
        "trade_id": "t-1",
        "timestamp": datetime(2026, 6, 24, 11, 0, tzinfo=ET),
        "strategy_id": "mean_reversion_qqq",
        "symbol": "QQQ",
        "side": "buy",
        "qty": 25.0,
        "expected_price": 500.0,
        "filled_price": 500.25,
        "expected_slippage_pct": 0.0005,
        "routing_posture": "BALANCED_IOC",
        "participation_cap_pct": 0.95,
    }
    base.update(overrides)
    return base


def _bars_from_fill(fill_ts: datetime, *, count: int = 8) -> list[dict]:
    bars = []
    for i in range(count):
        bars.append(
            {
                "timestamp": fill_ts + timedelta(minutes=15 * (i + 1)),
                "close": 500.0 + (i + 1) * 0.1,
                "volume": 100_000 + i * 1_000,
            }
        )
    return bars


def test_process_fill_tracks_slippage_delta(tmp_path: Path) -> None:
    loop = ExecutionFeedbackLoop(db_path=tmp_path / "vault.db")
    record = loop.process_fill(_fill_payload(filled_price=500.5))
    assert record.realized_slippage_pct > record.expected_slippage_pct
    assert record.slippage_delta_pct > 0.0
    assert record.size_bucket == classify_order_size_bucket(25.0)


def test_calculate_post_fill_markouts_horizons(tmp_path: Path) -> None:
    loop = ExecutionFeedbackLoop(db_path=tmp_path / "vault.db")
    fill_ts = datetime(2026, 6, 24, 11, 0, tzinfo=timezone.utc)
    loop.process_fill(_fill_payload(timestamp=fill_ts, trade_id="t-markout"))
    breakdown = loop.calculate_post_fill_markouts(
        _fill_payload(timestamp=fill_ts, trade_id="t-markout", filled_price=500.0),
        _bars_from_fill(fill_ts),
    )
    assert len(breakdown.horizons) == 3
    assert breakdown.horizons[0].horizon_minutes == 1
    assert breakdown.horizons[1].horizon_minutes == 5
    assert breakdown.horizons[2].horizon_minutes == 30
    assert breakdown.session_type
    assert breakdown.size_bucket


def test_determine_routing_urgency_aggressive_on_alpha_decay() -> None:
    loop = ExecutionFeedbackLoop()
    loop.markout_window.extend([-12.0, -10.0, -9.0])
    decision = loop.determine_routing_urgency(
        {
            "expected_edge_bps": 20.0,
            "book_pressure": 0.2,
            "modeled_slippage_bps": 5.0,
        }
    )
    assert decision.posture == RoutingPosture.AGGRESSIVE_TAKER


def test_determine_routing_urgency_passive_on_book_pressure() -> None:
    loop = ExecutionFeedbackLoop()
    decision = loop.determine_routing_urgency(
        {
            "expected_edge_bps": 4.0,
            "book_pressure": 0.9,
            "modeled_slippage_bps": 5.0,
        }
    )
    assert decision.posture == RoutingPosture.PASSIVE_NBBO


def test_participation_cap_scales_with_volume() -> None:
    loop = ExecutionFeedbackLoop()
    thin = loop.resolve_participation_cap(
        {
            "symbol": "QQQ",
            "base_cap_pct": 0.95,
            "order_notional": 50_000.0,
            "reference_price": 500.0,
            "thin_liquidity_active": True,
            "trailing_volumes": [50_000.0, 45_000.0, 40_000.0],
        }
    )
    normal = loop.resolve_participation_cap(
        {
            "symbol": "QQQ",
            "base_cap_pct": 0.95,
            "order_notional": 50_000.0,
            "reference_price": 500.0,
            "thin_liquidity_active": False,
            "trailing_volumes": [200_000.0, 190_000.0, 180_000.0],
        }
    )
    assert thin.participation_cap_pct < normal.participation_cap_pct
    assert thin.vol_impact_scalar <= normal.vol_impact_scalar


def test_compute_book_pressure_buy_side() -> None:
    passive = compute_book_pressure(
        bid_size=100.0,
        ask_size=400.0,
        spread_pct=0.0015,
        side=Side.BUY,
    )
    aggressive = compute_book_pressure(
        bid_size=400.0,
        ask_size=100.0,
        spread_pct=0.0002,
        side=Side.BUY,
    )
    assert passive > aggressive


def test_margin_alert_emergency_signal() -> None:
    risk = RiskManager(RiskConfig())
    gate = CapitalGate(risk)
    bridge = BrokerTelemetryBridge(gate)
    stressed = Account(equity=100_000.0, cash=5_000.0, buying_power=8_000.0)
    signal = bridge.feed_capital_gate_telemetry(
        {"event": "account_sync", "account": stressed, "leg_count": 2}
    )
    assert signal.emergency_active
    assert signal.position_size_multiplier < 1.0
    assert "margin_alert" in signal.reason


def test_reject_storm_blocks_entries() -> None:
    risk = RiskManager(RiskConfig())
    gate = CapitalGate(risk)
    bridge = BrokerTelemetryBridge(gate)
    healthy = Account(equity=100_000.0, cash=80_000.0, buying_power=75_000.0)
    bridge.feed_capital_gate_telemetry(
        {"event": "account_sync", "account": healthy, "leg_count": 1}
    )
    for _ in range(3):
        bridge.feed_capital_gate_telemetry({"event": "order_reject", "reason": "insufficient_bp"})
    signal = bridge.feed_capital_gate_telemetry({"event": "order_reject", "reason": "insufficient_bp"})
    assert signal.emergency_active
    assert signal.block_new_entries
    assert gate.apply_emergency_to_max_position(0.95) == 0.0


def test_execution_feedback_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    ensure_execution_feedback_schema(db_path)
    loop = ExecutionFeedbackLoop(db_path=db_path)
    fill = _fill_payload(trade_id="schema-test")
    loop.process_fill(fill)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT trade_id, strategy_id, symbol, side, qty, "
            "expected_price, filled_price "
            "FROM execution_feedback_ledger WHERE trade_id = ?",
            ("schema-test",),
        ).fetchone()
    assert row is not None, "row was not written to execution_feedback_ledger"
    assert row[0] == "schema-test"
    assert row[1] == fill["strategy_id"]
    assert row[2] == fill["symbol"].upper()
    assert row[3] == fill["side"]
    assert row[4] == pytest.approx(fill["qty"])
    assert row[5] == pytest.approx(fill["expected_price"])
    assert row[6] == pytest.approx(fill["filled_price"])
