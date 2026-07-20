"""FIX-3 parity matrix: the composed entry authority reproduces the live gauntlet's
block/allow decision across an EXHAUSTIVE grid of gate states -- the promotion prerequisite
(LLD L-D) that makes wiring-day safe. BUILD-NOT-WIRE: nothing here touches the live path.
"""
from __future__ import annotations

import itertools

from src.engine.entry_authority import ENTRY_ACTIONS, EntryContext, entry_allowed


def _live_gauntlet_reference(ctx: EntryContext) -> bool:
    """Faithful re-encoding of the live entry gauntlet's decision sequence, annotated with
    orchestrator.py line numbers. Returns True iff the live path would ALLOW the entry."""
    is_entry = ctx.action in ENTRY_ACTIONS
    # orchestrator _escalation_blocks_entries (:1011-1016) + escalation.should_flatten_portfolio (:4214)
    if ctx.preemption_flatten_portfolio or ctx.preemption_blocks_strategy:
        return False
    if ctx.portfolio_halted:                                   # :3441 (blocks all)
        return False
    if ctx.strategy_halted and is_entry:                       # :3445 (entry only)
        return False
    if ctx.capital_gate_block_new_entries and is_entry:        # :3474 (entry only)
        return False
    if ctx.degradation_block_new_entries and is_entry and not ctx.force_liquidation:  # :3484
        return False
    if ctx.safe_mode:                                          # leg_state.safe_mode
        return False
    if ctx.commissioning_gate_blocked:                         # commissioning FAIL_CLOSED
        return False
    return True


_BOOL_FIELDS = [
    "preemption_flatten_portfolio", "preemption_blocks_strategy", "portfolio_halted",
    "strategy_halted", "capital_gate_block_new_entries", "degradation_block_new_entries",
    "safe_mode", "commissioning_gate_blocked", "force_liquidation",
]
_ACTIONS = ["LONG", "SHORT", "EXIT", None]


def test_entry_authority_matches_live_gauntlet_over_full_grid():
    """2^9 x 4 = 2048 states: composed entry_allowed == the live gauntlet, everywhere."""
    checked = mismatches = 0
    for action in _ACTIONS:
        for combo in itertools.product([False, True], repeat=len(_BOOL_FIELDS)):
            ctx = EntryContext(action=action, **dict(zip(_BOOL_FIELDS, combo)))
            checked += 1
            if entry_allowed(ctx).allowed != _live_gauntlet_reference(ctx):
                mismatches += 1
    assert mismatches == 0, f"{mismatches}/{checked} states diverged from the live gauntlet"
    assert checked == 2048


def test_precedence_first_block_wins_reason():
    # preemption outranks a simultaneous portfolio/strategy halt (reason attribution)
    d = entry_allowed(EntryContext(action="LONG", preemption_flatten_portfolio=True,
                                   portfolio_halted=True, strategy_halted=True))
    assert d.allowed is False and d.block_reason == "preemption_escalation"


def test_clean_context_allows_and_exit_survives_entry_only_gates():
    assert entry_allowed(EntryContext(action="LONG")).allowed is True
    # an EXIT is NOT blocked by the entry-only gates (strategy-halt/capital/degradation)
    assert entry_allowed(EntryContext(action="EXIT", strategy_halted=True,
                                      capital_gate_block_new_entries=True,
                                      degradation_block_new_entries=True)).allowed is True
    # ...but a portfolio halt / preemption / safe_mode blocks even an EXIT (matches live)
    assert entry_allowed(EntryContext(action="EXIT", portfolio_halted=True)).allowed is False


# ---- composed SIZING authority parity (equivalence to the CURRENT live final_cap) ----
import itertools as _it

from src.engine.sizing_authority import (
    BudgetContext, CapContext, composed_final_cap, composed_risk_budget_fraction,
)


def _live_final_cap_reference(c: CapContext) -> float:
    """Faithful re-encoding of risk_manager.resolve_live_max_position_pct's cap composition,
    line-annotated. This is the CURRENT live path (mid-stack clamp) -- the composed authority
    reproduces it exactly (equivalence, NOT a bug-correction; RA-CAP-BYPASS is latent-not-live)."""
    if c.safe_mode:                                            # :1578
        return 0.0
    cap = (c.base_cap * c.quality_multiplier * c.parity_multiplier
           * c.corr_shutter_multiplier * c.portfolio_throttle)  # :1547-1552
    cap = max(0.0, min(c.max_position_pct, cap))               # :1554 clamp (mid-stack)
    cap = cap * c.vol_size_scalar                              # :448 post-clamp
    if c.thin_liquidity_active:
        cap = cap * c.thin_liquidity_mult                     # :1606 post-clamp
    return cap


def test_composed_final_cap_matches_live_over_grid():
    checked = mism = 0
    for base, ceil, q, par, corr, port, vol, thin, sm in _it.product(
        [0.10, 0.25], [0.20, 0.25, 1.0], [0.0, 0.5, 1.0], [0.5, 1.0, 1.5],
        [0.0, 1.0], [0.5, 1.0], [0.25, 0.5, 1.0], [False, True], [False, True],
    ):
        c = CapContext(base_cap=base, max_position_pct=ceil, safe_mode=sm,
                       quality_multiplier=q, parity_multiplier=par, corr_shutter_multiplier=corr,
                       portfolio_throttle=port, vol_size_scalar=vol, thin_liquidity_active=thin)
        checked += 1
        if abs(composed_final_cap(c) - _live_final_cap_reference(c)) > 1e-12:
            mism += 1
    assert mism == 0, f"{mism}/{checked} cap states diverged from the live final_cap composition"
    assert checked == 2 * 3 * 3 * 3 * 2 * 2 * 3 * 2 * 2


def test_cap_invariant_holds_today_but_is_unenforced_ra_cap_bypass():
    # With every post-clamp multiplier <= 1.0 (the live regime today), final_cap <= max_position_pct.
    c = CapContext(base_cap=0.25, max_position_pct=0.25, parity_multiplier=1.5,
                   vol_size_scalar=1.0, thin_liquidity_active=False)  # parity pushes group1 to 0.375
    assert composed_final_cap(c) <= c.max_position_pct + 1e-12, "cap holds when post-clamp mults <=1"
    # LATENT gap (RA-CAP-BYPASS): a hypothetical post-clamp vol_size_scalar > 1.0 WOULD breach the
    # clamped cap -- demonstrating the invariant is unenforced. Forward hardening (clamp-last +
    # a final_cap<=max_position_pct assertion) closes this; not adopted in the equivalence path.
    breach = CapContext(base_cap=0.30, max_position_pct=0.25, vol_size_scalar=1.5)
    assert composed_final_cap(breach) > breach.max_position_pct, "unenforced: >1.0 post-clamp breaches"


def test_composed_budget_fraction_zeros_and_compounds():
    for field in ("breaker_halt", "exit_only", "leg_disabled", "signal_conflict_block"):
        frac, z = composed_risk_budget_fraction(BudgetContext(**{field: True}))
        assert frac == 0.0 and z is not None
    frac, z = composed_risk_budget_fraction(
        BudgetContext(pad_leg_budget=0.5, governor_corr_clamp=0.6, lld_watch_mult=0.5))
    assert z is None and abs(frac - 0.5 * 0.6 * 1.0 * 0.5) < 1e-12
