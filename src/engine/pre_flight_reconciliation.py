"""
Cold-start broker reconciliation — align local SQLite runtime state with Alpaca truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from src.engine.degradation_manager import DegradationManager
from src.engine.governance import (
    EVENT_STATE_RECON_RECOVERY,
    ImmutableChangeJournal,
    TriggeredBy,
    engage_pre_flight_recon_lock,
    is_pre_flight_recon_locked,
)
from src.ingestor.assets import infer_asset_class
from src.models import Position
from src.persistence import db as persistence
from src.persistence.db import DB_PATH, RESEARCH_VAULT_PATH

QTY_TOLERANCE = 1e-4
PRICE_TOLERANCE = 0.02
PRE_FLIGHT_RECON_LOCK_SCOPE = "PRE_FLIGHT_RECON"
DEFAULT_BAR_INTERVAL_SECONDS = 900
FILL_LOOKBACK_DAYS = 30
FILL_LOOKBACK_EXPANSION_DAYS = (60, 90)
DEEP_POSITION_ENTRY_UNRESOLVED = "deep_position_entry_fill_unresolved"


class OpenOrderSnapshot(Protocol):
    order_id: str
    symbol: str
    side: str
    qty: float
    status: str


class BrokerFillSnapshot(Protocol):
    order_id: str
    symbol: str
    side: str
    filled_qty: float
    filled_at: datetime


class BrokerReconciliationClient(Protocol):
    async def get_positions(self) -> list[Position]: ...
    async def get_open_orders(self) -> list[OpenOrderSnapshot]: ...
    async def get_symbol_fills(
        self,
        symbol: str,
        *,
        lookback_days: int = FILL_LOOKBACK_DAYS,
    ) -> list[BrokerFillSnapshot]: ...


@dataclass(frozen=True)
class LegMemoryBound:
    strategy_id: str
    symbol: str
    in_position: bool
    position_side: str
    bars_in_trade: int
    qty: float
    avg_entry_price: float
    max_bars_in_trade: int = 40
    open_order_count: int = 0


@dataclass(frozen=True)
class PositionDelta:
    strategy_id: str
    symbol: str
    field_name: str
    local_value: float | str
    broker_value: float | str


@dataclass(frozen=True)
class PreFlightReconciliationResult:
    success: bool
    recovered: bool
    latched_soft_degrade: bool
    deltas: tuple[PositionDelta, ...]
    failure_reason: str
    journal_event_id: int | None
    metadata: dict[str, Any]


@dataclass
class PreFlightReconciliationEngine:
    """Compare broker portfolio truth against persisted strategy runtime snapshots."""

    broker: BrokerReconciliationClient
    strategy_symbols: Mapping[str, str]
    change_journal: ImmutableChangeJournal
    degradation_manager: DegradationManager | None = None
    strategy_bar_intervals: Mapping[str, int] = field(default_factory=dict)
    db_path: Path = DB_PATH
    vault_path: Path = RESEARCH_VAULT_PATH
    on_reconstruct_leg_memory: Callable[[Mapping[str, LegMemoryBound]], None] | None = None
    qty_tolerance: float = QTY_TOLERANCE
    price_tolerance: float = PRICE_TOLERANCE
    fill_lookback_days: int = FILL_LOOKBACK_DAYS
    market_now: Callable[[], datetime] | None = None
    defer_latch: bool = False

    async def execute(self) -> PreFlightReconciliationResult:
        locked, lock_reason = is_pre_flight_recon_locked(self.vault_path)
        if locked:
            return PreFlightReconciliationResult(
                success=False,
                recovered=False,
                latched_soft_degrade=True,
                deltas=(),
                failure_reason=lock_reason or "pre_flight_recon_lock_active",
                journal_event_id=None,
                metadata={"pre_existing_lock": True},
            )

        broker_positions = await self.broker.get_positions()
        broker_orders = await self.broker.get_open_orders()
        local_snapshots = persistence.load_strategy_runtime_snapshots(
            self.strategy_symbols,
            db_path=self.db_path,
        )
        local_pending = persistence.count_local_pending_orders(
            self.strategy_symbols,
            db_path=self.db_path,
        )

        broker_by_symbol = _broker_positions_by_symbol(broker_positions)
        mapped_symbols = {symbol.upper() for symbol in self.strategy_symbols.values()}
        irrecoverable_reason = _detect_irrecoverable_mismatch(
            broker_by_symbol,
            mapped_symbols,
            broker_orders,
        )
        if irrecoverable_reason:
            return self._latch_soft_degrade(
                irrecoverable_reason,
                metadata={
                    "broker_positions": _serialize_positions(broker_by_symbol),
                    "open_orders": _serialize_open_orders(broker_orders),
                },
            )

        deltas = _diff_snapshots(
            local_snapshots,
            broker_by_symbol,
            local_pending,
            _open_order_counts(broker_orders),
            qty_tolerance=self.qty_tolerance,
            price_tolerance=self.price_tolerance,
        )
        journal_event_id: int | None = None
        recovered = False

        if deltas:
            bounds, unresolved = await _apply_broker_baseline(
                self.broker,
                self.strategy_symbols,
                broker_by_symbol,
                local_snapshots,
                db_path=self.db_path,
                strategy_bar_intervals=self.strategy_bar_intervals,
                qty_tolerance=self.qty_tolerance,
                price_tolerance=self.price_tolerance,
                fill_lookback_days=self.fill_lookback_days,
                market_now=self._current_market_time(),
            )
            if unresolved:
                return self._latch_soft_degrade(
                    f"{DEEP_POSITION_ENTRY_UNRESOLVED}:{','.join(unresolved)}",
                    metadata={
                        "unresolved_strategies": list(unresolved),
                        "fill_lookback_days": self.fill_lookback_days,
                        "fill_lookback_expansion_days": list(FILL_LOOKBACK_EXPANSION_DAYS),
                    },
                )
            if self.on_reconstruct_leg_memory is not None:
                self.on_reconstruct_leg_memory(bounds)
            entry = self.change_journal.append(
                event_type=EVENT_STATE_RECON_RECOVERY,
                triggered_by=TriggeredBy.SYSTEM_AUTOMATIC,
                scope_key=PRE_FLIGHT_RECON_LOCK_SCOPE,
                previous_state={
                    "local_snapshots": {
                        sid: _snapshot_to_dict(local_snapshots.get(sid))
                        for sid in self.strategy_symbols
                    },
                    "local_pending_orders": dict(local_pending),
                },
                requested_state={
                    "broker_snapshots": {
                        sid: _snapshot_to_dict(bounds[sid])
                        for sid in bounds
                    },
                    "open_order_counts": _open_order_counts(broker_orders),
                },
                rationale="pre_flight_broker_reconciliation_recovery",
                metadata={
                    "delta_count": len(deltas),
                    "deltas": [delta.__dict__ for delta in deltas],
                },
            )
            journal_event_id = entry.journal_id
            recovered = True
        else:
            bounds = {
                sid: _snapshot_from_broker(
                    sid,
                    self.strategy_symbols[sid],
                    broker_by_symbol.get(self.strategy_symbols[sid].upper()),
                    prior=local_snapshots.get(sid),
                )
                for sid in self.strategy_symbols
            }
            persistence.persist_strategy_runtime_snapshots(bounds, db_path=self.db_path)

        persistence.sync_local_open_order_liability(
            _open_order_counts(broker_orders),
            self.strategy_symbols,
            db_path=self.db_path,
        )

        return PreFlightReconciliationResult(
            success=True,
            recovered=recovered,
            latched_soft_degrade=False,
            deltas=tuple(deltas),
            failure_reason="",
            journal_event_id=journal_event_id,
            metadata={
                "broker_position_count": len(broker_by_symbol),
                "open_order_count": len(broker_orders),
            },
        )

    def _latch_soft_degrade(
        self,
        reason: str,
        *,
        metadata: Mapping[str, Any],
    ) -> PreFlightReconciliationResult:
        if not self.defer_latch:
            engage_pre_flight_recon_lock(
                reason,
                metadata=dict(metadata),
                db_path=self.vault_path,
            )
            if self.degradation_manager is not None:
                self.degradation_manager.apply_soft_degrade(reason)
        return PreFlightReconciliationResult(
            success=False,
            recovered=False,
            latched_soft_degrade=True,
            deltas=(),
            failure_reason=reason,
            journal_event_id=None,
            metadata=dict(metadata),
        )

    def _current_market_time(self) -> datetime:
        if self.market_now is not None:
            return _coerce_utc(self.market_now())
        return datetime.now(timezone.utc)


def _broker_positions_by_symbol(positions: list[Position]) -> dict[str, Position]:
    by_symbol: dict[str, Position] = {}
    for position in positions:
        by_symbol[position.symbol.upper()] = position
    return by_symbol


def _open_order_counts(orders: list[OpenOrderSnapshot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for order in orders:
        symbol = str(order.symbol).upper()
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def _requires_whole_shares(symbol: str) -> bool:
    """Whether ``symbol`` must hold an integer quantity.

    Equities trade in whole shares, so a fractional equity qty/position is a genuine
    irrecoverable mismatch and must still latch. Crypto (and other fractional-eligible
    assets) legitimately carries fractional quantities -- enforcing the integer invariant
    there would FALSE-latch SOFT_DEGRADE (block_new_entries) the moment a valid 0.257 BTC
    order/position appears. Mirrors the sizing path's asset-class fractional derivation.
    """
    return infer_asset_class(symbol) != "crypto"


def _detect_irrecoverable_mismatch(
    broker_by_symbol: Mapping[str, Position],
    mapped_symbols: set[str],
    broker_orders: list[OpenOrderSnapshot],
) -> str:
    for symbol in broker_by_symbol:
        if symbol not in mapped_symbols:
            return f"unmapped_broker_symbol:{symbol}"

    for order in broker_orders:
        symbol = str(order.symbol).upper()
        if symbol not in mapped_symbols:
            return f"unmapped_open_order_symbol:{symbol}"
        qty = float(order.qty)
        if qty <= 0.0:
            return f"fractional_open_order_qty:{symbol}:{qty}"
        if _requires_whole_shares(order.symbol) and abs(qty - round(qty)) > QTY_TOLERANCE:
            return f"fractional_open_order_qty:{symbol}:{qty}"

    for symbol, position in broker_by_symbol.items():
        qty = float(position.qty)
        if qty <= 0.0:
            continue
        if _requires_whole_shares(symbol) and abs(qty - round(qty)) > QTY_TOLERANCE:
            return f"fractional_share_position_conflict:{symbol}:{qty}"
    return ""


def _diff_snapshots(
    local_snapshots: Mapping[str, persistence.StrategyRuntimeSnapshot],
    broker_by_symbol: Mapping[str, Position],
    local_pending: Mapping[str, int],
    broker_pending: Mapping[str, int],
    *,
    qty_tolerance: float,
    price_tolerance: float,
) -> list[PositionDelta]:
    deltas: list[PositionDelta] = []
    for strategy_id, snapshot in local_snapshots.items():
        symbol = snapshot.symbol.upper()
        broker = broker_by_symbol.get(symbol)
        broker_qty = float(broker.qty) if broker is not None else 0.0
        broker_side = broker.side if broker is not None else "flat"
        broker_avg = float(broker.avg_entry_price) if broker is not None else 0.0

        if abs(snapshot.qty - broker_qty) > qty_tolerance:
            deltas.append(
                PositionDelta(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    field_name="qty",
                    local_value=snapshot.qty,
                    broker_value=broker_qty,
                )
            )
        if snapshot.side != broker_side and not (
            snapshot.side == "flat" and broker_qty <= qty_tolerance
        ):
            deltas.append(
                PositionDelta(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    field_name="side",
                    local_value=snapshot.side,
                    broker_value=broker_side,
                )
            )
        if broker_qty > qty_tolerance and abs(snapshot.avg_entry_price - broker_avg) > price_tolerance:
            deltas.append(
                PositionDelta(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    field_name="avg_entry_price",
                    local_value=snapshot.avg_entry_price,
                    broker_value=broker_avg,
                )
            )

        local_open = int(local_pending.get(strategy_id, 0))
        broker_open = int(broker_pending.get(symbol, 0))
        if local_open != broker_open:
            deltas.append(
                PositionDelta(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    field_name="open_order_count",
                    local_value=float(local_open),
                    broker_value=float(broker_open),
                )
            )
    return deltas


async def resolve_position_entry_fill_ts(
    broker: BrokerReconciliationClient,
    symbol: str,
    *,
    position_qty: float,
    position_side: str,
    qty_tolerance: float,
    initial_lookback_days: int = FILL_LOOKBACK_DAYS,
) -> tuple[datetime | None, int]:
    lookbacks: list[int] = []
    for days in (initial_lookback_days, *FILL_LOOKBACK_EXPANSION_DAYS):
        normalized = max(int(days), 1)
        if normalized not in lookbacks:
            lookbacks.append(normalized)
    for days in lookbacks:
        fills = await broker.get_symbol_fills(symbol, lookback_days=days)
        entry_ts = find_position_entry_fill_ts(
            fills,
            symbol=symbol,
            position_qty=position_qty,
            position_side=position_side,
            qty_tolerance=qty_tolerance,
        )
        if entry_ts is not None:
            return entry_ts, days
    return None, lookbacks[-1]


async def _apply_broker_baseline(
    broker: BrokerReconciliationClient,
    strategy_symbols: Mapping[str, str],
    broker_by_symbol: Mapping[str, Position],
    local_snapshots: Mapping[str, persistence.StrategyRuntimeSnapshot],
    *,
    db_path: Path,
    strategy_bar_intervals: Mapping[str, int],
    qty_tolerance: float,
    price_tolerance: float,
    fill_lookback_days: int,
    market_now: datetime,
) -> tuple[dict[str, LegMemoryBound], tuple[str, ...]]:
    bounds: dict[str, LegMemoryBound] = {}
    unresolved: list[str] = []
    for strategy_id, symbol in strategy_symbols.items():
        broker_position = broker_by_symbol.get(symbol.upper())
        prior = local_snapshots.get(strategy_id)
        bars_override: int | None = None
        if (
            broker_position is not None
            and float(broker_position.qty) > qty_tolerance
            and _lacks_matching_local_telemetry(
                prior,
                broker_position,
                qty_tolerance=qty_tolerance,
                price_tolerance=price_tolerance,
            )
        ):
            entry_ts, _used_days = await resolve_position_entry_fill_ts(
                broker,
                symbol,
                position_qty=float(broker_position.qty),
                position_side=broker_position.side,
                qty_tolerance=qty_tolerance,
                initial_lookback_days=fill_lookback_days,
            )
            if entry_ts is None:
                unresolved.append(strategy_id)
            else:
                bar_interval = int(
                    strategy_bar_intervals.get(strategy_id, DEFAULT_BAR_INTERVAL_SECONDS)
                    or DEFAULT_BAR_INTERVAL_SECONDS
                )
                max_bars = int((prior.max_bars_in_trade if prior is not None else 40) or 40)
                bars_override = deduce_bars_in_trade(
                    entry_ts,
                    market_now,
                    bar_interval,
                    max_bars=max_bars,
                )
        bounds[strategy_id] = _snapshot_from_broker(
            strategy_id,
            symbol,
            broker_position,
            prior=prior,
            bars_in_trade_override=bars_override,
        )
    if unresolved:
        return {}, tuple(unresolved)
    persistence.persist_strategy_runtime_snapshots(bounds, db_path=db_path)
    return bounds, ()


def _lacks_matching_local_telemetry(
    prior: persistence.StrategyRuntimeSnapshot | None,
    broker: Position,
    *,
    qty_tolerance: float,
    price_tolerance: float,
) -> bool:
    if prior is None:
        return True
    broker_qty = float(broker.qty)
    if abs(prior.qty - broker_qty) > qty_tolerance:
        return True
    if prior.side != broker.side and not (
        prior.side == "flat" and broker_qty <= qty_tolerance
    ):
        return True
    if (
        broker_qty > qty_tolerance
        and abs(prior.avg_entry_price - float(broker.avg_entry_price)) > price_tolerance
    ):
        return True
    return False


def _coerce_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _signed_fill_inventory_delta(side: str, qty: float) -> float:
    normalized = str(side).strip().lower()
    if normalized == "buy":
        return float(qty)
    if normalized == "sell":
        return -float(qty)
    return 0.0


def _inventory_crossed_zero_boundary(
    prior_net: float,
    net: float,
    *,
    qty_tolerance: float,
) -> bool:
    if abs(net) <= qty_tolerance:
        return abs(prior_net) > qty_tolerance
    if prior_net > qty_tolerance and net < -qty_tolerance:
        return True
    if prior_net < -qty_tolerance and net > qty_tolerance:
        return True
    return False


def _signed_position_inventory_target(
    position_qty: float,
    position_side: str,
    *,
    qty_tolerance: float,
) -> float | None:
    qty = float(position_qty)
    side = str(position_side).strip().lower()
    if qty <= qty_tolerance or side not in {"long", "short"}:
        return None
    return qty if side == "long" else -qty


def find_position_entry_fill_ts(
    fills: list[BrokerFillSnapshot],
    *,
    symbol: str,
    position_qty: float,
    position_side: str,
    qty_tolerance: float = QTY_TOLERANCE,
) -> datetime | None:
    """
    Signed-inventory asset netting for cold-start position entry reconstruction.

    Anchors on the live broker position quantity and direction, then walks closed
    fills newest-first subtracting signed execution deltas until the running
    inventory crosses the zero boundary that opened the current directional block.
    """
    signed_target = _signed_position_inventory_target(
        position_qty,
        position_side,
        qty_tolerance=qty_tolerance,
    )
    if signed_target is None:
        return None

    symbol_key = str(symbol).strip().upper()
    running_inventory = float(signed_target)
    ordered_fills = sorted(
        (
            fill
            for fill in fills
            if str(fill.symbol).strip().upper() == symbol_key
            and float(fill.filled_qty) > qty_tolerance
        ),
        key=lambda row: row.filled_at,
        reverse=True,
    )
    if not ordered_fills:
        return None

    for fill in ordered_fills:
        prior_inventory = running_inventory
        fill_delta = _signed_fill_inventory_delta(
            str(fill.side),
            float(fill.filled_qty),
        )
        running_inventory -= fill_delta
        if _inventory_crossed_zero_boundary(
            prior_inventory,
            running_inventory,
            qty_tolerance=qty_tolerance,
        ):
            return _coerce_utc(fill.filled_at)
    return None


def deduce_bars_in_trade(
    entry_ts: datetime,
    now: datetime,
    bar_interval_seconds: int,
    *,
    max_bars: int,
) -> int:
    interval = max(int(bar_interval_seconds or DEFAULT_BAR_INTERVAL_SECONDS), 1)
    entry = _coerce_utc(entry_ts)
    current = _coerce_utc(now)
    elapsed_seconds = max(0.0, (current - entry).total_seconds())
    bars = int(elapsed_seconds // interval) + 1
    ceiling = max(int(max_bars or 1), 1)
    return max(1, min(bars, ceiling))


def _snapshot_from_broker(
    strategy_id: str,
    symbol: str,
    broker: Position | None,
    *,
    prior: persistence.StrategyRuntimeSnapshot | None,
    bars_in_trade_override: int | None = None,
) -> LegMemoryBound:
    symbol_key = symbol.upper()
    if broker is None or float(broker.qty) <= QTY_TOLERANCE:
        return LegMemoryBound(
            strategy_id=strategy_id,
            symbol=symbol_key,
            in_position=False,
            position_side="flat",
            bars_in_trade=0,
            qty=0.0,
            avg_entry_price=0.0,
            max_bars_in_trade=int(prior.max_bars_in_trade if prior is not None else 40),
            open_order_count=int(prior.open_order_count if prior is not None else 0),
        )

    max_bars = int((prior.max_bars_in_trade if prior is not None else 40) or 40)
    if bars_in_trade_override is not None:
        bars_in_trade = max(1, min(int(bars_in_trade_override), max_bars))
    else:
        prior_bars = int(prior.bars_in_trade if prior is not None else 0)
        bars_in_trade = max(1, min(prior_bars if prior_bars > 0 else 1, max_bars))
    open_orders = int(prior.open_order_count if prior is not None else 0)
    return LegMemoryBound(
        strategy_id=strategy_id,
        symbol=symbol_key,
        in_position=True,
        position_side=broker.side,
        bars_in_trade=bars_in_trade,
        qty=float(broker.qty),
        avg_entry_price=float(broker.avg_entry_price),
        max_bars_in_trade=max_bars,
        open_order_count=open_orders,
    )


def _snapshot_to_dict(bound: LegMemoryBound | persistence.StrategyRuntimeSnapshot | None) -> dict[str, Any]:
    if bound is None:
        return {}
    if isinstance(bound, LegMemoryBound):
        return {
            "strategy_id": bound.strategy_id,
            "symbol": bound.symbol,
            "in_position": bound.in_position,
            "position_side": bound.position_side,
            "bars_in_trade": bound.bars_in_trade,
            "qty": bound.qty,
            "avg_entry_price": bound.avg_entry_price,
        }
    return {
        "strategy_id": bound.strategy_id,
        "symbol": bound.symbol,
        "side": bound.side,
        "bars_in_trade": bound.bars_in_trade,
        "qty": bound.qty,
        "avg_entry_price": bound.avg_entry_price,
        "open_order_count": bound.open_order_count,
        "max_bars_in_trade": bound.max_bars_in_trade,
    }


def _serialize_positions(positions: Mapping[str, Position]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "qty": float(position.qty),
            "side": position.side,
            "avg_entry_price": float(position.avg_entry_price),
        }
        for symbol, position in positions.items()
    ]


def _serialize_open_orders(orders: list[OpenOrderSnapshot]) -> list[dict[str, Any]]:
    return [
        {
            "order_id": str(order.order_id),
            "symbol": str(order.symbol).upper(),
            "side": str(order.side),
            "qty": float(order.qty),
            "status": str(order.status),
        }
        for order in orders
    ]
