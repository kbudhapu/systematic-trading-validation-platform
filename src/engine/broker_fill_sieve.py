"""
Asynchronous broker fill reconciliation sieve — post-submit polling and cycle tracking.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import structlog

from src.engine.execution_adaptor import (
    apply_broker_authority_to_leg_memory,
    detect_leg_broker_memory_drift,
    reconcile_broker_primary_fill,
)
from src.engine.leg_performance import match_position
from src.engine.strategy_leg import LegState
from src.models import Order, OrderResult, Position, Side

log = structlog.get_logger()

INCOMPLETE_CYCLE_CANCEL_THRESHOLD = 3
TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "partially_filled", "canceled", "cancelled", "expired", "rejected"}
)
OPEN_ORDER_STATUSES = frozenset({"new", "accepted", "pending_new", "submitted", "open"})


class FillReconciliationBroker(Protocol):
    async def get_positions(self) -> list[Position]: ...

    async def short_burst_refresh_order_results(
        self,
        orders: list[Order],
        results: list[OrderResult],
    ) -> list[OrderResult]: ...

    async def refresh_order_result(
        self,
        order: Order,
        result: OrderResult,
    ) -> OrderResult: ...

    async def cancel_open_orders_for_symbol(self, symbol: str) -> int: ...

    async def count_open_orders_for_symbol(self, symbol: str) -> int: ...


@dataclass
class ChildFillSlice:
    order_id: str | None
    requested_qty: float
    side: Side
    filled_qty: float = 0.0
    status: str = "submitted"


@dataclass
class PendingFillBundle:
    strategy_id: str
    symbol: str
    slices: list[ChildFillSlice] = field(default_factory=list)
    positions_before: list[Position] = field(default_factory=list)
    unresolved_cycles: int = 0

    @property
    def requested_qty(self) -> float:
        return sum(max(slice_.requested_qty, 0.0) for slice_ in self.slices)

    @property
    def filled_qty(self) -> float:
        return sum(max(slice_.filled_qty, 0.0) for slice_ in self.slices)

    def is_terminal(self) -> bool:
        if not self.slices:
            return True
        return all(slice_.status.lower() in TERMINAL_ORDER_STATUSES for slice_ in self.slices)

    def needs_tracking(self) -> bool:
        if not self.slices:
            return False
        if any(slice_.status.lower() in OPEN_ORDER_STATUSES for slice_ in self.slices):
            return True
        if self.filled_qty + 1e-9 < self.requested_qty:
            return True
        return any(
            slice_.status.lower() == "partially_filled"
            or (
                slice_.requested_qty > 0.0
                and slice_.filled_qty + 1e-9 < slice_.requested_qty
            )
            for slice_ in self.slices
        )


def _bundle_key(strategy_id: str, symbol: str) -> str:
    return f"{strategy_id}:{symbol.upper()}"


def _build_slices(orders: list[Order], results: list[OrderResult]) -> list[ChildFillSlice]:
    if not results:
        return []
    order_lookup = {f"{order.symbol}:{order.side.value}": order for order in orders}
    slices: list[ChildFillSlice] = []
    for result in results:
        order = order_lookup.get(f"{result.symbol}:{result.side.value}")
        requested_qty = float(order.qty) if order is not None else float(result.qty)
        side = order.side if order is not None else result.side
        slices.append(
            ChildFillSlice(
                order_id=result.order_id,
                requested_qty=requested_qty,
                side=side,
                filled_qty=max(float(result.qty), 0.0),
                status=str(result.status or "submitted").lower(),
            )
        )
    return slices


def _bundle_from_submission(
    *,
    strategy_id: str,
    symbol: str,
    orders: list[Order],
    results: list[OrderResult],
    positions_before: list[Position],
) -> PendingFillBundle:
    return PendingFillBundle(
        strategy_id=strategy_id,
        symbol=symbol.upper(),
        slices=_build_slices(orders, results),
        positions_before=list(positions_before),
    )


class BrokerFillReconciliationSieve:
    """Tracks unresolved fills across cycles and forces broker-authoritative leg memory."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingFillBundle] = {}
        self._lock = asyncio.Lock()

    async def pending_bundle_count(self) -> int:
        async with self._lock:
            return len(self._pending)

    async def has_pending_bundle_for_leg(self, strategy_id: str, symbol: str) -> bool:
        """Return True if an unresolved fill bundle exists for this exact leg."""
        key = _bundle_key(strategy_id, symbol)
        async with self._lock:
            return key in self._pending

    async def reconcile_post_submit(
        self,
        *,
        leg: LegState,
        orders: list[Order],
        results: list[OrderResult],
        positions_before: list[Position],
        broker: FillReconciliationBroker,
        risk_manager_note: Callable[[int], None] | None = None,
    ) -> list[Position]:
        if not results:
            return positions_before
        cfg = leg.config
        refreshed = await broker.short_burst_refresh_order_results(orders, results)
        positions = await self._apply_broker_authority(
            leg=leg,
            orders=orders,
            results=refreshed,
            positions_before=positions_before,
            broker=broker,
            risk_manager_note=risk_manager_note,
        )
        bundle = _bundle_from_submission(
            strategy_id=cfg.strategy_id,
            symbol=cfg.symbol,
            orders=orders,
            results=refreshed,
            positions_before=positions_before,
        )
        async with self._lock:
            key = _bundle_key(cfg.strategy_id, cfg.symbol)
            if bundle.needs_tracking():
                self._pending[key] = bundle
            else:
                self._pending.pop(key, None)
        return positions

    async def register_ioc_residual(
        self,
        *,
        leg: LegState,
        orders: list[Order],
        results: list[OrderResult],
        positions_before: list[Position],
        residual_qty: float,
        entry_scale_factor: float,
        broker: FillReconciliationBroker,
        risk_manager_note: Callable[[int], None] | None = None,
    ) -> list[Position]:
        if residual_qty <= 1e-9 or not results:
            return positions_before
        cfg = leg.config
        refreshed = await broker.short_burst_refresh_order_results(orders, results)
        positions = await self._apply_broker_authority(
            leg=leg,
            orders=orders,
            results=refreshed,
            positions_before=positions_before,
            broker=broker,
            risk_manager_note=risk_manager_note,
        )
        bundle = _bundle_from_submission(
            strategy_id=cfg.strategy_id,
            symbol=cfg.symbol,
            orders=orders,
            results=refreshed,
            positions_before=positions_before,
        )
        bundle.unresolved_cycles = 0
        async with self._lock:
            key = _bundle_key(cfg.strategy_id, cfg.symbol)
            self._pending[key] = bundle
        log.warning(
            "ioc_residual_flagged_for_sieve",
            strategy_id=cfg.strategy_id,
            symbol=cfg.symbol,
            residual_qty=residual_qty,
            entry_scale_factor=entry_scale_factor,
        )
        return positions

    async def advance_pending_cycles(
        self,
        *,
        legs: dict[str, LegState],
        broker: FillReconciliationBroker,
        risk_manager_note: Callable[[int], None] | None = None,
    ) -> None:
        async with self._lock:
            bundles = list(self._pending.items())
        for key, bundle in bundles:
            leg = legs.get(bundle.strategy_id)
            if leg is None:
                async with self._lock:
                    self._pending.pop(key, None)
                continue
            await self._advance_bundle(
                leg=leg,
                bundle=bundle,
                broker=broker,
                risk_manager_note=risk_manager_note,
            )

    async def _advance_bundle(
        self,
        *,
        leg: LegState,
        bundle: PendingFillBundle,
        broker: FillReconciliationBroker,
        risk_manager_note: Callable[[int], None] | None = None,
    ) -> None:
        key = _bundle_key(bundle.strategy_id, bundle.symbol)
        orders = [
            Order(
                symbol=bundle.symbol,
                side=slice_.side,
                qty=slice_.requested_qty,
                strategy_id=bundle.strategy_id,
            )
            for slice_ in bundle.slices
        ]
        results = [
            OrderResult(
                symbol=bundle.symbol,
                side=slice_.side,
                qty=slice_.filled_qty,
                filled_price=0.0,
                filled_at=leg.window.latest().timestamp
                if leg.window.latest() is not None
                else datetime.now(timezone.utc),
                order_id=slice_.order_id,
                status=slice_.status,
            )
            for slice_ in bundle.slices
        ]
        refreshed: list[OrderResult] = []
        for order, result in zip(orders, results):
            if result.order_id:
                result = await broker.refresh_order_result(order, result)
            refreshed.append(result)
        bundle.slices = _build_slices(orders, refreshed)

        positions = await self._apply_broker_authority(
            leg=leg,
            orders=orders,
            results=refreshed,
            positions_before=bundle.positions_before,
            broker=broker,
            risk_manager_note=risk_manager_note,
        )

        if bundle.needs_tracking():
            bundle.unresolved_cycles += 1
            if bundle.unresolved_cycles >= INCOMPLETE_CYCLE_CANCEL_THRESHOLD:
                cancelled = await broker.cancel_open_orders_for_symbol(bundle.symbol)
                if cancelled:
                    log.warning(
                        "incomplete_fill_bracket_cancelled",
                        strategy_id=bundle.strategy_id,
                        symbol=bundle.symbol,
                        unresolved_cycles=bundle.unresolved_cycles,
                        cancelled_orders=cancelled,
                    )
                # GD-R1: surface the residual explicitly rather than dropping silently.
                # Auto-resubmitting is intentionally avoided — chasing a partial fill
                # after N unresolved cycles risks entering at a materially worse price.
                # Instead, emit a critical structured event so an operator can act and
                # so the position's underweight state is visible in telemetry.
                filled = bundle.filled_qty
                requested = bundle.requested_qty
                residual = max(requested - filled, 0.0)
                entry_scale = filled / requested if requested > 0.0 else 0.0
                log.error(
                    "partial_fill_residual_abandoned",
                    strategy_id=bundle.strategy_id,
                    symbol=bundle.symbol,
                    requested_qty=requested,
                    filled_qty=filled,
                    residual_qty=residual,
                    entry_scale_factor=entry_scale,
                    unresolved_cycles=bundle.unresolved_cycles,
                    action_required=(
                        "position is underweight its intended allocation; "
                        "review and manually top up or exit if necessary"
                    ),
                )
                async with self._lock:
                    self._pending.pop(key, None)
            else:
                async with self._lock:
                    self._pending[key] = bundle
            return

        async with self._lock:
            self._pending.pop(key, None)

    async def _apply_broker_authority(
        self,
        *,
        leg: LegState,
        orders: list[Order],
        results: list[OrderResult],
        positions_before: list[Position],
        broker: FillReconciliationBroker,
        risk_manager_note: Callable[[int], None] | None = None,
    ) -> list[Position]:
        positions = await broker.get_positions()
        if risk_manager_note is not None:
            risk_manager_note(1)
        cfg = leg.config
        prior = match_position(cfg.symbol, positions_before)
        current = match_position(cfg.symbol, positions)
        order_by_key = {f"{order.symbol}:{order.side.value}": order for order in orders}

        for result in results:
            if result.filled_price <= 0.0 and result.qty <= 0.0:
                if str(result.status).lower() not in TERMINAL_ORDER_STATUSES:
                    continue
            order = order_by_key.get(f"{result.symbol}:{result.side.value}")
            requested_qty = float(order.qty) if order is not None else float(result.qty)
            recon = reconcile_broker_primary_fill(
                requested_qty=requested_qty,
                fill_result=result,
                prior_position=prior,
                broker_position=current,
                bars_in_trade=leg.bars_in_trade,
            )
            leg.bars_in_trade = recon.bars_in_trade
            prior = current
            if recon.is_partial and recon.residual_qty > 1e-9:
                cancelled = await broker.cancel_open_orders_for_symbol(cfg.symbol)
                if cancelled:
                    log.warning(
                        "partial_fill_bracket_adjustment",
                        leg=cfg.strategy_id,
                        symbol=cfg.symbol,
                        residual_qty=recon.residual_qty,
                        broker_qty=recon.broker_qty,
                        cancelled_orders=cancelled,
                    )

        drift_detected = detect_leg_broker_memory_drift(
            bars_in_trade=leg.bars_in_trade,
            broker_position=current,
        )
        if drift_detected:
            positions = await broker.get_positions()
            if risk_manager_note is not None:
                risk_manager_note(1)
            current = match_position(cfg.symbol, positions)
            leg.bars_in_trade = apply_broker_authority_to_leg_memory(
                bars_in_trade=leg.bars_in_trade,
                broker_position=current,
                prior_position=match_position(cfg.symbol, positions_before),
            )
            self._emit_reconciliation_telemetry(
                leg=leg,
                broker_position=current,
                reconcile_status="MISMATCH",
                extra={"forced_position_refresh": True},
            )
        else:
            self._emit_reconciliation_telemetry(
                leg=leg,
                broker_position=current,
                reconcile_status="SUCCESS",
            )

        return positions

    def _emit_reconciliation_telemetry(
        self,
        *,
        leg: LegState,
        broker_position: Position | None,
        reconcile_status: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        cfg = leg.config
        payload: dict[str, Any] = {
            "reconcile_status": reconcile_status,
            "leg": cfg.strategy_id,
            "symbol": cfg.symbol,
            "bars_in_trade": leg.bars_in_trade,
            "broker_qty": float(broker_position.qty) if broker_position else 0.0,
            "broker_side": broker_position.side if broker_position else "flat",
        }
        if extra:
            payload.update(extra)
        if reconcile_status == "MISMATCH":
            log.warning("broker_primary_fill_reconciled", **payload)
        else:
            log.info("broker_primary_fill_reconciled", **payload)
