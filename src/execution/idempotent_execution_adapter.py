"""Idempotent execution adapter — the SINGLE live order path (E1).

Wraps a broker (AlpacaBroker live/paper, or a mock in tests) and becomes the only
route by which an order reaches the broker. It is a transparent proxy (`__getattr__`
delegates every other broker method unchanged) that intercepts ONLY `submit_orders`
to add the G1.3 guarantees to real orders:

  - deterministic `client_order_id` on every order (assigned upstream from the
    signal timestamp; a unique fallback is generated if unset so a missing id can
    never collide two distinct orders);
  - pre-submit dedupe (in-memory + a broker query by client_order_id) so a retry /
    reconnect / restart re-submit of the SAME logical order is a NO-OP, not a
    duplicate;
  - boot reconciliation (`reconcile_boot`) that trips ENTRY_GATE_HALT on any
    broker-vs-internal mismatch and never silently adopts broker state.

Behavior-preserving in the normal flow: when no dedupe fires, `submit_orders`
delegates to the inner broker with identical arguments and returns its results
unchanged — the only addition is the client_order_id carried on each order.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import structlog

from src.execution.idempotent_execution import (
    deterministic_client_order_id, reconcile_boot,
)
from src.engine.engine_preemption import RiskEscalationEngine
from src.models import OrderResult

log = structlog.get_logger()

DUPLICATE_NOOP_STATUS = "duplicate_noop"


class IdempotentExecutionAdapter:
    """The one order path. Install as the orchestrator's ``self.broker``."""

    def __init__(self, inner_broker, *, escalation: RiskEscalationEngine | None = None,
                 report_sink=None) -> None:
        self._inner = inner_broker
        self._escalation = escalation or RiskEscalationEngine()
        self._report_sink = report_sink or (lambda _r: None)
        self._seen: set[str] = set()          # client_order_ids submitted this process
        self._lock = threading.Lock()

    # transparent proxy for every broker method not overridden here
    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def inner(self):
        return self._inner

    @staticmethod
    def assign_client_order_id(order, *, signal_ts=None, epoch: int = 0) -> str:
        """Assign a deterministic client_order_id if unset. With a signal timestamp
        the id is restart-stable (the same bar reproduces it); without one, a unique
        fallback avoids any chance of colliding two distinct orders."""
        if order.client_order_id:
            return order.client_order_id
        if signal_ts is not None:
            order.client_order_id = deterministic_client_order_id(
                order.strategy_id or "leg", signal_ts, order.side.value, epoch)
        else:
            order.client_order_id = f"mbappe-{uuid.uuid4().hex[:32]}"
        return order.client_order_id

    async def _existing_broker_order(self, coid: str):
        getter = getattr(self._inner, "get_order_by_client_order_id", None)
        if getter is None:
            return None
        try:
            return await getter(coid)
        except Exception:
            return None

    def _noop_result(self, order) -> OrderResult:
        return OrderResult(symbol=order.symbol, side=order.side, qty=0.0,
                           filled_price=0.0, filled_at=datetime.now(timezone.utc),
                           order_id=order.client_order_id, status=DUPLICATE_NOOP_STATUS)

    async def submit_orders(self, orders, execution_contexts=None):
        """Idempotent submission. Each order carries a client_order_id; a dup (already
        seen this process, or already at the broker) is a NO-OP. New orders delegate
        to the inner broker unchanged (behavior-preserving when no dup fires)."""
        results: list = []
        to_submit: list = []
        for order in orders:
            coid = self.assign_client_order_id(order)
            # reserve the id atomically BEFORE any await, so concurrent/interleaved
            # submissions of the same id cannot both pass the check (no orphan dup).
            with self._lock:
                already_local = coid in self._seen
                if not already_local:
                    self._seen.add(coid)
            if already_local or (await self._existing_broker_order(coid)) is not None:
                log.info("idempotent_submit_dedup", client_order_id=coid, symbol=order.symbol)
                results.append(self._noop_result(order))
                continue
            to_submit.append(order)
        if to_submit:
            results.extend(await self._inner.submit_orders(
                to_submit, execution_contexts=execution_contexts))
        return results

    async def reconcile_boot(self, internal_positions: dict[str, float]):
        """Diff broker truth vs internal state at startup; ANY mismatch trips
        ENTRY_GATE_HALT + a DiagnosticReport (never silent adoption)."""
        broker_positions = await self._inner.get_positions()
        bp: dict[str, float] = {}
        for p in broker_positions:
            signed = p.qty if str(p.side).lower() == "long" else -p.qty
            bp[str(p.symbol).upper()] = signed
        return reconcile_boot(internal_positions, bp, escalation=self._escalation,
                              report_sink=self._report_sink)
