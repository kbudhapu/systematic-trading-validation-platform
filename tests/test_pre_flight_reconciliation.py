"""Tests for cold-start broker pre-flight reconciliation."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engine.control_plane import ControlPlaneSupervisor, SupervisorState
from src.engine.degradation_manager import DegradationManager, OperationalMode
from src.engine.governance import (
    EVENT_STATE_RECON_RECOVERY,
    ImmutableChangeJournal,
    ensure_governance_schema,
    is_pre_flight_recon_locked,
    release_pre_flight_recon_lock,
)
from src.engine.pre_flight_reconciliation import (
    PreFlightReconciliationEngine,
    deduce_bars_in_trade,
    find_position_entry_fill_ts,
    resolve_position_entry_fill_ts,
)
from src.models import Position
from src.persistence import db as persistence


@dataclass
class FakeOpenOrder:
    order_id: str
    symbol: str
    side: str
    qty: float
    status: str = "new"


@dataclass
class FakeFill:
    order_id: str
    symbol: str
    side: str
    filled_qty: float
    filled_at: datetime


class FakeBroker:
    def __init__(
        self,
        positions: list[Position],
        orders: list[FakeOpenOrder] | None = None,
        fills: list[FakeFill] | None = None,
    ) -> None:
        self._positions = positions
        self._orders = orders or []
        self._fills = fills or []

    async def get_positions(self) -> list[Position]:
        return self._positions

    async def get_open_orders(self) -> list[FakeOpenOrder]:
        return self._orders

    async def get_symbol_fills(
        self,
        symbol: str,
        *,
        lookback_days: int = 30,
    ) -> list[FakeFill]:
        symbol_key = symbol.upper()
        return [fill for fill in self._fills if fill.symbol.upper() == symbol_key]


STRATEGY_MAP = {"mean_reversion_qqq": "QQQ"}


def _seed_local_state(
    db_path: Path,
    *,
    qty: float = 0.0,
    side: str = "flat",
    avg_entry: float = 0.0,
    bars_in_trade: int = 0,
    open_orders: int = 0,
) -> None:
    persistence.init_db(db_path)
    from src.engine.pre_flight_reconciliation import LegMemoryBound

    persistence.persist_strategy_runtime_snapshots(
        {
            "mean_reversion_qqq": LegMemoryBound(
                strategy_id="mean_reversion_qqq",
                symbol="QQQ",
                in_position=qty > 0,
                position_side=side if qty > 0 else "flat",
                bars_in_trade=bars_in_trade,
                qty=qty,
                avg_entry_price=avg_entry,
                open_order_count=open_orders,
            )
        },
        db_path=db_path,
    )
    persistence.sync_local_open_order_liability(
        {"QQQ": open_orders},
        STRATEGY_MAP,
        db_path=db_path,
    )


def test_pre_flight_clean_match(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path, qty=10.0, side="long", avg_entry=400.0, bars_in_trade=3)
        broker = FakeBroker(
            [Position("QQQ", 10.0, "long", 400.0)],
        )
        journal = ImmutableChangeJournal(db_path=vault_path)
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=journal,
            db_path=db_path,
            vault_path=vault_path,
        )
        result = await engine.execute()
        assert result.success is True
        assert result.recovered is False
        assert result.latched_soft_degrade is False
        assert result.deltas == ()

    asyncio.run(run())


def test_pre_flight_qty_delta_recovery(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path, qty=5.0, side="long", avg_entry=390.0, bars_in_trade=2)
        now = datetime(2026, 6, 24, 15, 30, tzinfo=timezone.utc)
        entry_ts = now - timedelta(minutes=45)
        broker = FakeBroker(
            [Position("QQQ", 10.0, "long", 400.0)],
            fills=[
                FakeFill("f1", "QQQ", "buy", 10.0, entry_ts),
            ],
        )
        journal = ImmutableChangeJournal(db_path=vault_path)
        memory_updates: dict[str, int] = {}

        def on_memory(bounds: dict) -> None:
            for sid, bound in bounds.items():
                memory_updates[sid] = bound.bars_in_trade

        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=journal,
            db_path=db_path,
            vault_path=vault_path,
            strategy_bar_intervals={"mean_reversion_qqq": 900},
            on_reconstruct_leg_memory=on_memory,
            market_now=lambda: now,
        )
        result = await engine.execute()
        assert result.success is True
        assert result.recovered is True
        assert len(result.deltas) >= 1
        assert result.journal_event_id is not None

        snap = persistence.load_strategy_runtime_snapshots(STRATEGY_MAP, db_path=db_path)[
            "mean_reversion_qqq"
        ]
        assert snap.qty == 10.0
        assert snap.avg_entry_price == 400.0
        assert memory_updates["mean_reversion_qqq"] == 4

        with sqlite3.connect(vault_path) as conn:
            row = conn.execute(
                "SELECT event_type FROM immutable_change_journal WHERE journal_id = ?",
                (result.journal_event_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == EVENT_STATE_RECON_RECOVERY

    asyncio.run(run())


def test_pre_flight_unmapped_symbol_latches_soft_degrade(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path)
        broker = FakeBroker(
            [
                Position("QQQ", 10.0, "long", 400.0),
                Position("SPY", 5.0, "long", 500.0),
            ],
        )
        degradation = DegradationManager()
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=ImmutableChangeJournal(db_path=vault_path),
            degradation_manager=degradation,
            db_path=db_path,
            vault_path=vault_path,
        )
        result = await engine.execute()
        assert result.success is False
        assert result.latched_soft_degrade is True
        assert "unmapped_broker_symbol:SPY" in result.failure_reason
        locked, reason = is_pre_flight_recon_locked(vault_path)
        assert locked is True
        assert degradation.current_state().mode == OperationalMode.SOFT_DEGRADE

    asyncio.run(run())


def test_pre_flight_fractional_order_latches(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path)
        broker = FakeBroker(
            [],
            orders=[FakeOpenOrder("o1", "QQQ", "buy", 1.5)],
        )
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=ImmutableChangeJournal(db_path=vault_path),
            db_path=db_path,
            vault_path=vault_path,
        )
        result = await engine.execute()
        assert result.latched_soft_degrade is True
        assert "fractional_open_order_qty" in result.failure_reason

    asyncio.run(run())


def test_pre_flight_existing_lock_blocks_boot(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        from src.engine.governance import engage_pre_flight_recon_lock

        engage_pre_flight_recon_lock("manual_hold", db_path=vault_path)
        broker = FakeBroker([])
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=ImmutableChangeJournal(db_path=vault_path),
            db_path=db_path,
            vault_path=vault_path,
        )
        supervisor = ControlPlaneSupervisor(
            vault_path=vault_path,
            pre_flight_engine=engine,
        )
        started = await supervisor.start(run_pre_flight=True)
        assert started is False
        assert supervisor.state == SupervisorState.FAILED

    asyncio.run(run())


def test_control_plane_start_succeeds_after_recovery(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path, qty=10.0, side="long", avg_entry=400.0)
        broker = FakeBroker([Position("QQQ", 10.0, "long", 400.0)])
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=ImmutableChangeJournal(db_path=vault_path),
            db_path=db_path,
            vault_path=vault_path,
        )
        supervisor = ControlPlaneSupervisor(
            vault_path=vault_path,
            pre_flight_engine=engine,
        )
        started = await supervisor.start(run_pre_flight=True)
        assert started is True
        assert supervisor.state == SupervisorState.RUNNING

    asyncio.run(run())


def test_release_pre_flight_recon_lock(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.db"
    ensure_governance_schema(vault_path)
    from src.engine.governance import engage_pre_flight_recon_lock

    engage_pre_flight_recon_lock("stale_state", db_path=vault_path)
    assert release_pre_flight_recon_lock(
        operator="ops",
        rationale="verified broker book",
        db_path=vault_path,
    )
    locked, _ = is_pre_flight_recon_locked(vault_path)
    assert locked is False


def test_deduce_bars_in_trade_from_entry_timestamp() -> None:
    entry = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)
    now = entry + timedelta(minutes=45)
    assert deduce_bars_in_trade(entry, now, 900, max_bars=40) == 4
    assert deduce_bars_in_trade(entry, entry, 900, max_bars=40) == 1


def test_find_position_entry_fill_ts_scales_through_partial_exits() -> None:
    entry = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    scale = datetime(2026, 6, 24, 11, 0, tzinfo=timezone.utc)
    trim = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    fills = [
        FakeFill("f1", "QQQ", "buy", 10.0, entry),
        FakeFill("f2", "QQQ", "sell", 5.0, trim),
        FakeFill("f3", "QQQ", "buy", 5.0, scale),
    ]
    assert find_position_entry_fill_ts(
        fills,
        symbol="QQQ",
        position_qty=10.0,
        position_side="long",
    ) == entry


def test_find_position_entry_fill_ts_short_to_long_flip() -> None:
    short_open = datetime(2026, 6, 24, 9, 0, tzinfo=timezone.utc)
    flip_long = datetime(2026, 6, 24, 10, 30, tzinfo=timezone.utc)
    scale_in = datetime(2026, 6, 24, 11, 30, tzinfo=timezone.utc)
    fills = [
        FakeFill("f1", "QQQ", "sell", 5.0, short_open),
        FakeFill("f2", "QQQ", "buy", 10.0, flip_long),
        FakeFill("f3", "QQQ", "buy", 3.0, scale_in),
    ]
    assert find_position_entry_fill_ts(
        fills,
        symbol="QQQ",
        position_qty=8.0,
        position_side="long",
    ) == flip_long


def test_find_position_entry_fill_ts_long_to_short_flip() -> None:
    long_open = datetime(2026, 6, 24, 9, 0, tzinfo=timezone.utc)
    flip_short = datetime(2026, 6, 24, 10, 30, tzinfo=timezone.utc)
    scale_short = datetime(2026, 6, 24, 11, 30, tzinfo=timezone.utc)
    fills = [
        FakeFill("f1", "QQQ", "buy", 6.0, long_open),
        FakeFill("f2", "QQQ", "sell", 10.0, flip_short),
        FakeFill("f3", "QQQ", "sell", 2.0, scale_short),
    ]
    assert find_position_entry_fill_ts(
        fills,
        symbol="QQQ",
        position_qty=6.0,
        position_side="short",
    ) == flip_short


def test_find_position_entry_fill_ts_returns_none_when_fills_do_not_reconcile() -> None:
    fills = [
        FakeFill(
            "f1",
            "QQQ",
            "buy",
            4.0,
            datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc),
        ),
    ]
    assert (
        find_position_entry_fill_ts(
            fills,
            symbol="QQQ",
            position_qty=8.0,
            position_side="long",
        )
        is None
    )


def test_find_position_entry_fill_ts_filters_unrelated_symbols() -> None:
    entry = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    fills = [
        FakeFill("spy", "SPY", "buy", 100.0, entry),
        FakeFill("qqq", "QQQ", "buy", 10.0, entry),
    ]
    assert find_position_entry_fill_ts(
        fills,
        symbol="QQQ",
        position_qty=10.0,
        position_side="long",
    ) == entry


def test_pre_flight_cold_start_flat_local_reindexes_bars(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path)
        now = datetime(2026, 6, 24, 16, 0, tzinfo=timezone.utc)
        entry_ts = now - timedelta(minutes=30)
        broker = FakeBroker(
            [Position("QQQ", 8.0, "long", 405.0)],
            fills=[FakeFill("f1", "QQQ", "buy", 8.0, entry_ts)],
        )
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=ImmutableChangeJournal(db_path=vault_path),
            db_path=db_path,
            vault_path=vault_path,
            strategy_bar_intervals={"mean_reversion_qqq": 900},
            market_now=lambda: now,
        )
        result = await engine.execute()
        assert result.success is True
        assert result.recovered is True
        snap = persistence.load_strategy_runtime_snapshots(STRATEGY_MAP, db_path=db_path)[
            "mean_reversion_qqq"
        ]
        assert snap.qty == 8.0
        assert snap.bars_in_trade == 3

    asyncio.run(run())


def test_resolve_position_entry_fill_ts_expands_lookback() -> None:
    class LookbackBroker:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def get_symbol_fills(
            self,
            symbol: str,
            *,
            lookback_days: int = 30,
        ) -> list[FakeFill]:
            self.calls.append(lookback_days)
            if lookback_days < 60:
                return []
            entry_ts = datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)
            return [FakeFill("old", "QQQ", "buy", 5.0, entry_ts)]

    async def run() -> None:
        broker = LookbackBroker()
        entry_ts, used_days = await resolve_position_entry_fill_ts(
            broker,
            "QQQ",
            position_qty=5.0,
            position_side="long",
            qty_tolerance=1e-4,
            initial_lookback_days=30,
        )
        assert entry_ts is not None
        assert used_days == 60
        assert broker.calls == [30, 60]

    asyncio.run(run())


def test_pre_flight_deep_position_unresolved_latches_lock(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "ops.db"
        vault_path = tmp_path / "vault.db"
        ensure_governance_schema(vault_path)
        _seed_local_state(db_path)
        now = datetime(2026, 6, 24, 16, 0, tzinfo=timezone.utc)
        broker = FakeBroker(
            [Position("QQQ", 8.0, "long", 405.0)],
            fills=[],
        )
        engine = PreFlightReconciliationEngine(
            broker=broker,
            strategy_symbols=STRATEGY_MAP,
            change_journal=ImmutableChangeJournal(db_path=vault_path),
            degradation_manager=DegradationManager(),
            db_path=db_path,
            vault_path=vault_path,
            market_now=lambda: now,
        )
        result = await engine.execute()
        assert result.success is False
        assert result.latched_soft_degrade is True
        assert "deep_position_entry_fill_unresolved" in result.failure_reason
        locked, _ = is_pre_flight_recon_locked(vault_path)
        assert locked is True

    asyncio.run(run())
