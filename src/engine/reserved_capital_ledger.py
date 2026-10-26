"""
Reserved capital ledger — cycle-local buying power reservations for multi-leg contention.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from src.config import RiskConfig
from src.engine.portfolio_coordinator import CoordinatedLegPlan
from src.models import Account, SignalAction
from src.router.risk_manager import fractional_qty_allowed, position_size


@dataclass
class ReservedCapitalLedger:
    """Tracks committed buying power against the cycle-open broker snapshot."""

    _snapshot: Account | None = None
    _reserved_total: float = 0.0
    _reserved_by_strategy: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self._reserved_by_strategy is None:
            self._reserved_by_strategy = {}

    def begin_cycle(self, account: Account) -> None:
        self._snapshot = Account(
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
        )
        self._reserved_total = 0.0
        self._reserved_by_strategy = {}

    @property
    def snapshot_account(self) -> Account | None:
        return self._snapshot

    @property
    def reserved_total(self) -> float:
        return float(self._reserved_total)

    def available_buying_power(self) -> float:
        if self._snapshot is None:
            return 0.0
        return max(float(self._snapshot.buying_power) - self._reserved_total, 0.0)

    def execution_account_for(self, strategy_id: str) -> Account:
        if self._snapshot is None:
            return self.available_account()
        assert self._reserved_by_strategy is not None
        own_reserved = float(self._reserved_by_strategy.get(strategy_id, 0.0))
        other_reserved = max(self._reserved_total - own_reserved, 0.0)
        buying_power = max(float(self._snapshot.buying_power) - other_reserved, 0.0)
        cash = max(float(self._snapshot.cash) - other_reserved, 0.0)
        return Account(
            equity=float(self._snapshot.equity),
            cash=cash,
            buying_power=buying_power,
        )

    def available_account(self) -> Account:
        if self._snapshot is None:
            return Account(equity=0.0, cash=0.0, buying_power=0.0)
        remaining_bp = self.available_buying_power()
        remaining_cash = max(float(self._snapshot.cash) - self._reserved_total, 0.0)
        return Account(
            equity=float(self._snapshot.equity),
            cash=remaining_cash,
            buying_power=remaining_bp,
        )

    def reserve(self, strategy_id: str, amount: float) -> float:
        if self._snapshot is None:
            return 0.0
        assert self._reserved_by_strategy is not None
        prior = float(self._reserved_by_strategy.get(strategy_id, 0.0))
        self._reserved_total = max(self._reserved_total - prior, 0.0)
        available = self.available_buying_power()
        reserved = min(max(float(amount), 0.0), available)
        self._reserved_by_strategy[strategy_id] = reserved
        self._reserved_total += reserved
        return reserved

    def release(self, strategy_id: str) -> float:
        assert self._reserved_by_strategy is not None
        released = float(self._reserved_by_strategy.pop(strategy_id, 0.0))
        self._reserved_total = max(self._reserved_total - released, 0.0)
        return released

    def release_all(self) -> None:
        """Explicitly drop all outstanding reservations without resetting the snapshot.

        Called during cycle preemption so that stale reservations are cleared
        immediately rather than relying on begin_cycle() at the start of the
        next cycle — making the invariant explicit instead of implicit.
        """
        self._reserved_total = 0.0
        if self._reserved_by_strategy is not None:
            self._reserved_by_strategy.clear()

    def sync_from_broker(
        self,
        account: Account,
        *,
        strategy_id: str | None = None,
    ) -> None:
        self._snapshot = Account(
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
        )
        if strategy_id is not None:
            self.release(strategy_id)
        else:
            self._reserved_total = 0.0
            self._reserved_by_strategy = {}


def estimate_leg_buying_power_commitment(
    plan: CoordinatedLegPlan,
    account: Account,
    *,
    risk_config: RiskConfig,
    atr: float,
) -> float:
    signal = plan.signal
    if plan.blocked or signal is None or plan.force_liquidation:
        return 0.0
    if signal.action not in (SignalAction.LONG, SignalAction.SHORT):
        return 0.0
    price = float(signal.price)
    if price <= 0.0 or atr <= 0.0:
        return 0.0
    max_position_pct = float(
        plan.routing_params.get(
            "max_position_pct",
            plan.routing_params.get("effective_max_position_pct", risk_config.max_position_pct),
        )
        or risk_config.max_position_pct
    )
    # Fractional eligibility from ASSET CLASS (crypto), not the SHORT-only asset_context
    # fetch -- MUST match size_order's derivation so the commitment estimate and the actual
    # order size agree (both feed the same position_size int()-floor). SHORT byte-identical.
    allow_fractional = fractional_qty_allowed(signal.symbol, plan.asset_context)
    shares = position_size(
        account,
        atr,
        price,
        risk_config,
        risk_budget_fraction=plan.risk_fraction,
        max_position_pct=max_position_pct,
        allow_fractional=allow_fractional,
    )
    return max(float(shares) * price, 0.0)


def cap_plan_for_available_capital(
    plan: CoordinatedLegPlan,
    available_account: Account,
    *,
    risk_config: RiskConfig,
    atr: float,
    sizing_account: Account | None = None,
) -> tuple[CoordinatedLegPlan, float, dict[str, Any]]:
    sizing_source = sizing_account or available_account
    commitment = estimate_leg_buying_power_commitment(
        plan,
        sizing_source,
        risk_config=risk_config,
        atr=atr,
    )
    available = max(float(available_account.buying_power), 0.0)
    if commitment <= available + 1e-6:
        return plan, commitment, {"modified": False}

    if signal_is_entry(plan) and available <= 1e-6:
        blocked = _block_plan_for_capital_exhaustion(plan)
        return blocked, 0.0, {
            "modified": True,
            "action": "blocked",
            "requested_commitment": commitment,
            "available_buying_power": available,
            "scale_applied": 0.0,
        }

    scale = available / commitment if commitment > 0.0 else 0.0
    if scale <= 1e-6 and signal_is_entry(plan):
        blocked = _block_plan_for_capital_exhaustion(plan)
        return blocked, 0.0, {
            "modified": True,
            "action": "blocked",
            "requested_commitment": commitment,
            "available_buying_power": available,
            "scale_applied": 0.0,
        }

    routing_params = dict(plan.routing_params)
    base_max = float(
        routing_params.get(
            "max_position_pct",
            routing_params.get("effective_max_position_pct", risk_config.max_position_pct),
        )
        or risk_config.max_position_pct
    )
    combined_scale = max(scale, 0.0) * max(plan.sizing_multiplier, 0.0)
    if combined_scale <= 0.0:
        combined_scale = max(scale, 0.0)
    routing_params["max_position_pct"] = base_max * combined_scale
    routing_params["reserved_capital_contention_scale"] = scale

    adjusted = replace(
        plan,
        sizing_multiplier=plan.sizing_multiplier * scale,
        risk_fraction=plan.risk_fraction * scale,
        routing_params=routing_params,
    )
    adjusted_commitment = estimate_leg_buying_power_commitment(
        adjusted,
        available_account,
        risk_config=risk_config,
        atr=atr,
    )
    return adjusted, min(adjusted_commitment, available), {
        "modified": True,
        "action": "scaled",
        "requested_commitment": commitment,
        "available_buying_power": available,
        "scale_applied": scale,
        "adjusted_commitment": adjusted_commitment,
    }


def signal_is_entry(plan: CoordinatedLegPlan) -> bool:
    signal = plan.signal
    return (
        signal is not None
        and signal.action in (SignalAction.LONG, SignalAction.SHORT)
        and not plan.force_liquidation
    )


def _block_plan_for_capital_exhaustion(plan: CoordinatedLegPlan) -> CoordinatedLegPlan:
    signal = plan.signal
    cleared_signal = None
    if signal is not None and signal.action in (SignalAction.LONG, SignalAction.SHORT):
        cleared_signal = None
    return replace(
        plan,
        blocked=True,
        block_reason="reserved_capital_exhausted",
        sizing_multiplier=0.0,
        risk_fraction=0.0,
        signal=cleared_signal if signal_is_entry(plan) else signal,
    )
