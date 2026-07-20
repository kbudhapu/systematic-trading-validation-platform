"""PB-1/P5 (FIX-3): single composed sizing authority -- BUILD-NOT-WIRE.

Sizing today is a multiplier stack split across risk_manager (the position cap) and the
quantity path (risk_budget_fraction). This collapses the composition into ONE place, owning the
full P5 stack, total-ordered per the registered STANDING-COMPOSEDAUTHORITY design.

This module FAITHFULLY REPRODUCES the CURRENT live composition (proven equivalent by the parity
matrix in tests/test_composed_authority_parity.py) so it is a safe drop-in when wired. In
particular the position cap is clamped MID-STACK exactly as the live path does
(risk_manager.py:1554, before vol_size_scalar and thin-liquidity) -- see finding RA-CAP-BYPASS.

FORWARD HARDENING (documented, NOT the live-equivalent behavior, NOT adopted here): at promotion
the clamp should move LAST and a post-composition assertion `final_cap <= max_position_pct`
should fail loud. Both close RA-CAP-BYPASS's unenforced-invariant gap. They are gated to the
separate promotion-gated wiring queue -- do NOT enable them in the equivalence path.

HARD STOP: nothing here is wired into the live order path; the live sizing call sites are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapContext:
    """Inputs to the live position-cap composition (risk_manager.resolve_live_max_position_pct)."""
    base_cap: float                     # risk_config.max_position_pct fed into optimize_portfolio_allocation
    max_position_pct: float             # the clamp ceiling (risk_config.max_position_pct)
    # [Z] binary zero (cap path)
    safe_mode: bool = False             # :1578 -> 0.0
    # [M] pre-clamp multipliers
    quality_multiplier: float = 1.0     # _quality_multiplier_from_score
    parity_multiplier: float = 1.0      # relative_capacity_weight * active_count
    corr_shutter_multiplier: float = 1.0  # _correlation_shutter_multiplier (0.75 book-aggregate)
    portfolio_throttle: float = 1.0     # PORTFOLIO_FACTOR_THROTTLE (net_beta/gross_imbalance)
    # [M] post-clamp multipliers (RA-CAP-BYPASS: applied AFTER the :1554 clamp; both <= 1.0 today)
    vol_size_scalar: float = 1.0        # apply_vol_target_sizing, bounded [0.25, 1.0]
    thin_liquidity_active: bool = False
    thin_liquidity_mult: float = 0.70   # THIN_LIQUIDITY_PARTICIPATION_MULT


@dataclass(frozen=True)
class BudgetContext:
    """Inputs to the composed risk_budget_fraction (the quantity path feeding position_size)."""
    pad_leg_budget: float = 1.0         # PAD weights[leg] (replaces 1/n_enabled when wired)
    governor_corr_clamp: float = 1.0    # 0.6 when |rho| >= MAX_SAFE_CORRELATION (0.85)
    governor_cold_start_clamp: float = 1.0  # 0.6 when < MIN_CORRELATION_SAMPLES bars
    lld_watch_mult: float = 1.0         # 0.5 in LLD WATCH
    # [Z] binary zeros (quantity path)
    exit_only: bool = False             # governor drawdown exit-only
    signal_conflict_block: bool = False  # coord resolve_signal_conflicts -> sizing_multiplier 0
    leg_disabled: bool = False          # coord resolve_portfolio_mode leg-enablement off
    breaker_halt: bool = False          # drawdown breaker / portfolio halt


@dataclass(frozen=True)
class SizingDecision:
    final_cap: float
    risk_budget_fraction: float
    zeroed_by: str | None = None


def composed_final_cap(ctx: CapContext) -> float:
    """Reproduce the live position-cap composition EXACTLY (mid-stack clamp), for equivalence.

    Order (risk_manager): SAFE_MODE->0 (:1578); base x quality x parity x corr x portfolio
    (:1547-1552); CLAMP to max_position_pct (:1554); x vol_size_scalar (:448); x thin (:1606).
    """
    if ctx.safe_mode:
        return 0.0
    cap = (ctx.base_cap * ctx.quality_multiplier * ctx.parity_multiplier
           * ctx.corr_shutter_multiplier * ctx.portfolio_throttle)
    cap = max(0.0, min(ctx.max_position_pct, cap))          # <-- mid-stack clamp (live-faithful)
    cap = cap * ctx.vol_size_scalar                          # post-clamp (bounded <=1.0 today)
    if ctx.thin_liquidity_active:
        cap = cap * ctx.thin_liquidity_mult                 # post-clamp (0.70)
    return cap


def composed_risk_budget_fraction(ctx: BudgetContext) -> tuple[float, str | None]:
    """Compose the quantity-path budget fraction: [Z] zeros short-circuit, else compound [M]."""
    for flag, name in (
        (ctx.breaker_halt, "breaker_halt"),
        (ctx.exit_only, "exit_only"),
        (ctx.leg_disabled, "leg_disabled"),
        (ctx.signal_conflict_block, "signal_conflict"),
    ):
        if flag:
            return 0.0, name
    frac = (ctx.pad_leg_budget * ctx.governor_corr_clamp
            * ctx.governor_cold_start_clamp * ctx.lld_watch_mult)
    return frac, None


def composed_sizing(cap_ctx: CapContext, budget_ctx: BudgetContext) -> SizingDecision:
    """The single composed sizing authority: owns the cap composition AND the budget fraction."""
    frac, zeroed = composed_risk_budget_fraction(budget_ctx)
    return SizingDecision(
        final_cap=composed_final_cap(cap_ctx),
        risk_budget_fraction=frac,
        zeroed_by=zeroed,
    )
