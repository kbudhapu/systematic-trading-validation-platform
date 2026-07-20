"""Execution robustness: idempotent submission, fill/rejection state, boot
reconciliation, and preemption-safe order lifecycle (garage G1.3).

Broker-agnostic on purpose: everything here operates against the small
``ExecutionBroker`` protocol, so it is exercised entirely with a mock client in
tests and NEVER submits a live or paper order. The live ``AlpacaBroker`` becomes
an ``ExecutionBroker`` by exposing submit / get_by_client_order_id / open_orders /
positions (integration point; not wired here to keep the live path untouched).

Design invariants:
- **Idempotency by construction.** Every order carries a deterministic
  client_order_id = hash(leg_id, signal_ts, side, epoch). Before any submit we
  query the broker by that id and NO-OP on a match; retries reuse the same id, so
  a duplicate-submit storm produces exactly one broker order.
- **No silent adoption.** Boot reconciliation DIFFS internal vs broker state and,
  on ANY mismatch, trips the block-new-entries safe state (RiskEscalationLevel
  .ENTRY_GATE_HALT) and emits a structured mismatch report -- it never overwrites
  internal state from the broker.
- **Preemption leaves no orphan.** Because the client_order_id is reserved (and
  deterministic) before the network send, a cycle preempted mid-order is safe:
  restart + reconcile finds the order by its id and adopts it exactly once.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from src.engine.engine_preemption import (
    EnginePreemptedException,
    PreemptionCheckpoint,
    RiskEscalationEngine,
    RiskEscalationLevel,
)
from src.engine.entry_authority import EntryDecision


class EntryBlockedException(Exception):
    """Raised at the single submission gate (E1) when the composed entry authority
    (entry_authority.entry_allowed) blocks the order. Carries the block reason. Because the
    client_order_id is reserved deterministically BEFORE this check, a blocked order leaves no
    orphan and no broker order is ever sent."""

    def __init__(self, decision: EntryDecision, *, leg_id: str, symbol: str) -> None:
        self.decision = decision
        self.leg_id = leg_id
        self.symbol = symbol
        super().__init__(
            f"entry blocked at submission: {decision.block_reason} (leg={leg_id} sym={symbol})"
        )

# Terminal broker statuses (no further fills expected).
TERMINAL_STATUSES = frozenset({"filled", "canceled", "cancelled", "expired", "rejected"})
FILLABLE_STATUSES = frozenset({"filled", "partially_filled"})


def deterministic_client_order_id(
    leg_id: str, signal_ts: datetime | str, side: str, epoch: int | str
) -> str:
    """Deterministic, collision-resistant client_order_id for an intended order.

    Same (leg_id, signal_ts, side, epoch) -> same id, so retries and post-restart
    re-submits are idempotent. Prefixed and truncated to stay within broker
    client-order-id length limits (Alpaca: 48 chars)."""
    ts = signal_ts.isoformat() if isinstance(signal_ts, datetime) else str(signal_ts)
    raw = f"{leg_id}|{ts}|{str(side).lower()}|{epoch}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:38]
    return f"mbappe-{digest}"


@dataclass(frozen=True)
class OrderIntent:
    leg_id: str
    symbol: str
    side: str                     # "buy" | "sell"
    qty: float
    signal_ts: datetime | str
    epoch: int

    @property
    def client_order_id(self) -> str:
        return deterministic_client_order_id(self.leg_id, self.signal_ts, self.side, self.epoch)


@dataclass
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str
    qty: float
    status: str = "new"           # new|partially_filled|filled|canceled|rejected|submission_failed
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    order_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in TERMINAL_STATUSES

    @property
    def remainder(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)


class ExecutionBroker(Protocol):
    """Minimal contract the robustness layer needs (mock in tests, Alpaca in prod)."""

    def submit(self, intent: OrderIntent) -> BrokerOrder: ...
    def get_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def positions(self) -> dict[str, float]: ...   # symbol -> signed qty


# --------------------------------------------------------------------------- #
# Idempotent submission
# --------------------------------------------------------------------------- #

@dataclass
class SubmitResult:
    order: BrokerOrder
    was_noop: bool                # True == an existing order matched the id (dedup)


class IdempotentSubmitter:
    """Query-before-submit idempotent order placement.

    Thread-safe: a duplicate-submit storm on one client_order_id yields exactly one
    broker order (the first submit wins; all others observe it and NO-OP).
    """

    def __init__(
        self,
        broker: ExecutionBroker,
        *,
        escalation: RiskEscalationEngine | None = None,
        report_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self._broker = broker
        self._escalation = escalation or RiskEscalationEngine()
        self._report_sink = report_sink or (lambda _r: None)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _coid_lock(self, coid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(coid, threading.Lock())

    def submit(
        self,
        intent: OrderIntent,
        *,
        preempt_check: Callable[[], bool] | None = None,
        entry_guard: Callable[[], EntryDecision] | None = None,
    ) -> SubmitResult:
        """Submit ``intent`` idempotently.

        E1 — SINGLE ENFORCEMENT POINT: ``entry_guard`` (optional) is the ONE composed
        kill-state gate at order submission. It is evaluated at the safe checkpoint AFTER the
        client_order_id is reserved but BEFORE the network send; if it returns a blocked
        EntryDecision an ``EntryBlockedException`` is raised and NO order is sent (kill outranks
        all — it is checked before preempt_check). The caller builds the decision by populating
        an EntryContext from the live stores and calling entry_authority.entry_allowed, so there
        is exactly one composed check here instead of an ad-hoc OR upstream.

        ``preempt_check`` (optional) is polled at the same checkpoint; if it returns True an
        EnginePreemptedException is raised, leaving no orphan (the id is deterministic, so a later
        re-submit dedupes)."""
        coid = intent.client_order_id
        with self._coid_lock(coid):
            existing = self._broker.get_by_client_order_id(coid)
            if existing is not None:
                return SubmitResult(order=existing, was_noop=True)

            # E1: the composed entry authority is the FIRST gate — kill outranks all.
            if entry_guard is not None:
                decision = entry_guard()
                if not decision.allowed:
                    raise EntryBlockedException(
                        decision, leg_id=intent.leg_id, symbol=intent.symbol
                    )

            if preempt_check is not None and preempt_check():
                raise EnginePreemptedException(
                    PreemptionCheckpoint(phase="submit", step="pre_send",
                                         strategy_id=intent.leg_id, symbol=intent.symbol),
                    escalation_level=self._escalation.snapshot().level,
                )

            order = self._broker.submit(intent)
            if order.status.lower() == "rejected":
                self._report_sink({
                    "kind": "order_rejected", "leg_id": intent.leg_id,
                    "symbol": intent.symbol, "side": intent.side,
                    "client_order_id": coid, "qty": intent.qty,
                })
            return SubmitResult(order=order, was_noop=False)


# --------------------------------------------------------------------------- #
# Position tracking (partial fills, rejections)
# --------------------------------------------------------------------------- #

@dataclass
class TrackedPosition:
    symbol: str
    qty: float = 0.0              # signed (long > 0, short < 0)
    avg_price: float = 0.0


class PositionTracker:
    """Applies broker fills to internal positions with explicit state transitions.

    - Partial fill: internal qty reflects the FILLED quantity; the remainder is
      reported for management under the existing order policy (not silently
      dropped).
    - Rejection / submission_failed: position state is UNCHANGED.
    """

    def __init__(self) -> None:
        self._positions: dict[str, TrackedPosition] = {}
        self._applied: set[str] = set()   # client_order_ids already applied (idempotent)

    def position(self, symbol: str) -> float:
        p = self._positions.get(symbol.upper())
        return p.qty if p else 0.0

    def snapshot(self) -> dict[str, float]:
        return {s: p.qty for s, p in self._positions.items() if abs(p.qty) > 1e-12}

    def apply(self, order: BrokerOrder, *, fill_price: float | None = None) -> float:
        """Apply an order's fill to internal state; return the unfilled remainder.

        Idempotent per client_order_id: applying the same order twice does not
        double-count (safe under retries/reconciliation)."""
        status = order.status.lower()
        if status in {"rejected", "submission_failed", "canceled", "cancelled", "new"} \
                and order.filled_qty <= 0.0:
            return order.remainder if status == "new" else 0.0
        if order.client_order_id in self._applied:
            return order.remainder
        if order.filled_qty <= 0.0:
            return order.remainder

        sym = order.symbol.upper()
        pos = self._positions.setdefault(sym, TrackedPosition(symbol=sym))
        signed = order.filled_qty if order.side.lower() == "buy" else -order.filled_qty
        price = fill_price if fill_price is not None else order.filled_avg_price
        new_qty = pos.qty + signed
        # volume-weighted average entry when adding to a same-side position
        if pos.qty == 0.0 or (pos.qty > 0) == (signed > 0):
            total = abs(pos.qty) + abs(signed)
            if total > 0:
                pos.avg_price = (abs(pos.qty) * pos.avg_price + abs(signed) * price) / total
        pos.qty = new_qty
        self._applied.add(order.client_order_id)
        return order.remainder


# --------------------------------------------------------------------------- #
# Boot-time reconciliation (no silent adoption)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReconcileMismatch:
    kind: str                     # "position_qty" | "unknown_broker_position" |
                                  # "missing_broker_position" | "orphan_open_order"
    symbol: str
    internal: float | None
    broker: float | None


@dataclass
class ReconcileReport:
    mismatches: list[ReconcileMismatch] = field(default_factory=list)
    tripped: bool = False

    @property
    def clean(self) -> bool:
        return not self.mismatches


def reconcile_boot(
    internal_positions: dict[str, float],
    broker_positions: dict[str, float],
    *,
    internal_open_coids: set[str] | None = None,
    broker_open_coids: set[str] | None = None,
    qty_tol: float = 1e-6,
    escalation: RiskEscalationEngine | None = None,
    report_sink: Callable[[dict], None] | None = None,
) -> ReconcileReport:
    """Diff internal vs broker state at startup. ANY mismatch trips ENTRY_GATE_HALT
    (block new entries) and emits a structured report; internal state is NEVER
    overwritten from the broker (silent adoption is prohibited)."""
    report = ReconcileReport()
    syms = {s.upper() for s in internal_positions} | {s.upper() for s in broker_positions}
    internal = {s.upper(): q for s, q in internal_positions.items()}
    broker = {s.upper(): q for s, q in broker_positions.items()}
    for sym in sorted(syms):
        iq = internal.get(sym, 0.0)
        bq = broker.get(sym, 0.0)
        if abs(iq) < qty_tol and abs(bq) >= qty_tol:
            report.mismatches.append(ReconcileMismatch("unknown_broker_position", sym, iq, bq))
        elif abs(iq) >= qty_tol and abs(bq) < qty_tol:
            report.mismatches.append(ReconcileMismatch("missing_broker_position", sym, iq, bq))
        elif abs(iq - bq) > qty_tol:
            report.mismatches.append(ReconcileMismatch("position_qty", sym, iq, bq))

    if internal_open_coids is not None or broker_open_coids is not None:
        ioc = internal_open_coids or set()
        boc = broker_open_coids or set()
        for coid in sorted(boc - ioc):
            report.mismatches.append(ReconcileMismatch("orphan_open_order", coid, None, None))

    if report.mismatches:
        report.tripped = True
        if escalation is not None:
            escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT,
                                  commanded_by="boot_reconciliation")
        if report_sink is not None:
            report_sink({
                "kind": "boot_reconciliation_mismatch",
                "mismatch_count": len(report.mismatches),
                "mismatches": [
                    {"kind": m.kind, "symbol": m.symbol,
                     "internal": m.internal, "broker": m.broker}
                    for m in report.mismatches
                ],
            })
    return report
