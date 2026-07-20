"""Tests for governance durability, flatten protocol, and attribution rollback."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.engine.degradation_manager import DegradationManager, OperationalMode
from src.persistence.governance_state_store import (
    PendingOrderStore,
    load_governance_state,
    persist_governance_state,
)


def test_degradation_manager_persists_and_hydrates(tmp_path) -> None:
    db_path = tmp_path / "trading.db"
    manager = DegradationManager(db_path=db_path)
    manager.apply_soft_degrade("feed_degraded")
    reloaded = DegradationManager(db_path=db_path)
    state = reloaded.hydrate_from_vault()
    assert state.mode == OperationalMode.SOFT_DEGRADE
    assert state.reason == "feed_degraded"


def test_pending_order_store_stage_and_pop(tmp_path) -> None:
    store = PendingOrderStore(tmp_path / "trading.db")
    store.stage(
        "leg:QQQ:buy",
        strategy_id="leg",
        symbol="QQQ",
        side="buy",
        broker_order_id="ord-1",
    )
    assert store.load_all()["leg:QQQ:buy"] == "ord-1"
    assert store.pop("leg:QQQ:buy") == "ord-1"
    assert store.load_all() == {}


@pytest.mark.asyncio
async def test_unified_flatten_skips_duplicate_portfolio_flatten(tmp_path) -> None:
    from src.engine.flatten_protocol import UnifiedFlattenProtocol

    broker = MagicMock()
    broker.get_positions = AsyncMock(return_value=[])
    broker.close_all_positions = AsyncMock()
    store = PendingOrderStore(tmp_path / "trading.db")
    protocol = UnifiedFlattenProtocol(broker, store)
    protocol._portfolio_flattening = True
    result = await protocol.execute_portfolio_flatten("test")
    assert result.skipped is True
    broker.close_all_positions.assert_not_called()


def test_journal_signature_round_trip(tmp_path) -> None:
    from src.engine.governance import (
        ImmutableChangeJournal,
        TriggeredBy,
        verify_journal_signature,
    )

    journal = ImmutableChangeJournal(db_path=tmp_path / "vault.db")
    entry = journal.append(
        event_type="TEST_EVENT",
        triggered_by=TriggeredBy.SYSTEM_AUTOMATIC,
        previous_state={"a": 1},
        requested_state={"a": 2},
        rationale="unit test",
        scope_key="TEST",
    )
    assert verify_journal_signature(entry) is True


def test_attribution_slo_breaches_on_negative_sharpe(tmp_path, monkeypatch) -> None:
    import polars as pl

    from src.engine.policy_lifecycle import (
        AIPolicyLifecycleManager,
        DEFAULT_ATTRIBUTION_MAX_DRAWDOWN_PCT,
    )

    manager = AIPolicyLifecycleManager(db_path=tmp_path / "vault.db")
    frame = pl.DataFrame(
        {
            "timestamp": [datetime.now(timezone.utc).isoformat()] * 6,
            "strategy_id": ["leg"] * 6,
            "symbol": ["QQQ"] * 6,
            "pnl": [-1.0, -2.0, 1.0, -3.0, -1.0, -2.0],
            "slippage_pct": [0.0] * 6,
            "regime_id": ["CALM_MR"] * 6,
            "session_type": ["MIDDAY_DOLDRUMS"] * 6,
            "execution_tactic": ["AGGRESSIVE"] * 6,
        }
    )

    monkeypatch.setattr(
        "src.engine.policy_lifecycle._load_attribution_frame",
        lambda *args, **kwargs: frame,
    )
    verdict = manager.evaluate_live_attribution_slo(
        {
            "strategy_id": "leg",
            "symbol": "QQQ",
            "params": {"max_live_drawdown_pct": DEFAULT_ATTRIBUTION_MAX_DRAWDOWN_PCT},
        }
    )
    assert verdict.breached is True
    assert "live_sharpe_below_zero" in verdict.reasons


def test_governance_state_persist_load(tmp_path) -> None:
    db_path = tmp_path / "trading.db"
    persist_governance_state(
        degradation_mode="HARD_CRITICAL_DEGRADE",
        trigger_reason="test",
        entered_at="2026-01-01T00:00:00+00:00",
        flatten_latched=True,
        recovery_streak=0,
        db_path=db_path,
    )
    loaded = load_governance_state(db_path)
    assert loaded is not None
    assert loaded.degradation_mode == "HARD_CRITICAL_DEGRADE"
    assert loaded.flatten_latched is True


def test_tuner_sweep_grid_budget_rejects_oversized_grid(monkeypatch) -> None:
    from src.engine.tuner_limits import SearchGrid, enforce_sweep_grid_budget

    monkeypatch.setattr("src.engine.tuner_limits.MAX_SWEEP_INNER_COMBINATIONS", 10)
    oversized = SearchGrid(
        regime="CALM_MR",
        sma_long_grid=[200],
        sma_short_grid=[50],
        long_grid=np.ones(5),
        short_grid=np.ones(5),
        exit_grid=np.ones(5),
        max_bars_grid=np.ones(5),
    )
    with pytest.raises(RuntimeError, match="parameter sweep grid too large"):
        enforce_sweep_grid_budget(oversized)


def test_validate_production_journal_requires_signing_key(tmp_path, monkeypatch) -> None:
    from src.engine.governance import ImmutableChangeJournal, validate_production_governance_journal

    journal = ImmutableChangeJournal(db_path=tmp_path / "vault.db")
    monkeypatch.delenv("GOVERNANCE_SIGNING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOVERNANCE_SIGNING_KEY"):
        validate_production_governance_journal(journal, environment="production")


def test_pending_order_reconcile_removes_stale_staged_keys(tmp_path) -> None:
    store = PendingOrderStore(tmp_path / "trading.db")
    store.stage(
        "leg:QQQ:buy",
        strategy_id="leg",
        symbol="QQQ",
        side="buy",
        broker_order_id="stale-1",
    )
    report = store.reconcile_with_broker_open_orders(set())
    assert report["stale_removed"] == 1
    assert store.load_all() == {}


def test_hard_degrade_flatten_latch_consumed_once(tmp_path) -> None:
    manager = DegradationManager(db_path=tmp_path / "trading.db")
    manager.apply_hard_critical_degrade("broker_truth_breach")
    reloaded = DegradationManager(db_path=tmp_path / "trading.db")
    reloaded.hydrate_from_vault()
    assert reloaded.consume_flatten_request() is True
    assert reloaded.consume_flatten_request() is False


@pytest.mark.asyncio
async def test_portfolio_flatten_cascade_executes_cancel_await_flatten(tmp_path) -> None:
    from src.engine.flatten_protocol import UnifiedFlattenProtocol

    broker = MagicMock()
    position = MagicMock(symbol="QQQ", qty=10.0, side="long")
    broker.get_positions = AsyncMock(return_value=[position])
    broker.cancel_open_orders_for_symbol = AsyncMock(return_value=1)
    broker.await_open_orders_cleared = AsyncMock(return_value=(True, 0))
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())
    broker.close_all_positions = AsyncMock()
    store = PendingOrderStore(tmp_path / "trading.db")
    protocol = UnifiedFlattenProtocol(broker, store)
    result = await protocol.execute_portfolio_flatten("unit_test")
    assert result.executed is True
    assert result.symbols_flattened == ("QQQ",)
    broker.cancel_open_orders_for_symbol.assert_called_once_with("QQQ")
    broker.await_open_orders_cleared.assert_called_once()
    broker.force_flatten_symbol_position.assert_called_once()


def test_emergency_flatten_urgency_is_aggressive_taker() -> None:
    from src.engine.execution_adaptor import (
        RoutingPosture,
        determine_emergency_flatten_urgency,
    )

    decision = determine_emergency_flatten_urgency()
    assert decision.posture == RoutingPosture.AGGRESSIVE_TAKER
    assert decision.aggressiveness == 1.0
