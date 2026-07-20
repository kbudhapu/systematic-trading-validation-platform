"""RA-4 (FIX-3) → E1: single composed entry authority — NOW the one submission gate.

Entry permission was a distributed gauntlet scattered across the orchestrator's async entry path
(preemption escalation, portfolio/strategy halt, capital/pre-open gate, degradation, leg-level
safe_mode + commissioning FAIL_CLOSED). This collapses ALL of those into ONE total-ordered, pure,
decision-only function so there is a single place that answers "may this leg enter?".

E1 (CP7 Gate-B G-B1): `entry_allowed` is wired as the single authoritative gate at order
submission in `src/execution/idempotent_execution.py` (IdempotentSubmitter.submit's entry_guard),
so every order passes exactly one composed check instead of an ad-hoc OR of scattered booleans.
The 9-field composition remains parity-proven equivalent to the historical gauntlet
(tests/test_composed_authority_parity.py, unchanged). E1 ADDS two safety stores CP6 found the
gauntlet never consulted at order time — `pre_flight_recon_locked` (control_plane_latches) and
`risk_halted` (the LOCAL risk-halt source-of-truth, NEVER the browser-writable Supabase
risk_state mirror, CP6 F9) — both global blocks. These are strict additions (default False), so
the historical parity grid is unaffected; see tests/test_entry_authority_five_stores.py.
"""
from __future__ import annotations

from dataclasses import dataclass

ENTRY_ACTIONS = frozenset({"LONG", "SHORT"})


@dataclass(frozen=True)
class EntryContext:
    """Raw gate state read from the live system (this authority reads, never acts)."""
    action: str | None                          # "LONG" | "SHORT" | "EXIT" | None
    preemption_flatten_portfolio: bool = False  # escalation.should_flatten_portfolio()
    preemption_blocks_strategy: bool = False    # escalation.blocks_entries_for_strategy(sid)
    portfolio_halted: bool = False              # override_registry.is_portfolio_halted()
    strategy_halted: bool = False               # override_registry.is_strategy_halted(sid)
    capital_gate_block_new_entries: bool = False  # capital_gate.snapshot().block_new_entries
    degradation_block_new_entries: bool = False   # degradation state.block_new_entries
    safe_mode: bool = False                     # leg_state.safe_mode
    commissioning_gate_blocked: bool = False    # leg not on the commissioning allowlist (FAIL_CLOSED)
    force_liquidation: bool = False             # plan.force_liquidation (degradation exemption)
    # E1 additions — CP6 stores the historical gauntlet never consulted at order time:
    pre_flight_recon_locked: bool = False       # control_plane_latches.is_pre_flight_recon_locked()
    risk_halted: bool = False                   # LOCAL risk-halt source-of-truth (NOT the Supabase
                                                # risk_state mirror — reading that is the CP6 F9 trap)


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    block_reason: str | None = None


def entry_allowed(ctx: EntryContext) -> EntryDecision:
    """Compose every entry gate in the registered precedence (first BLOCK wins).

    Action semantics mirror the live gauntlet: portfolio-halt and preemption block ALL actions;
    strategy-halt / capital-gate / degradation block ENTRY actions only; safe_mode and the
    commissioning gate block the leg entirely.
    """
    is_entry = ctx.action in ENTRY_ACTIONS

    # 1. Preemption escalation (highest) -- blocks all actions.
    if ctx.preemption_flatten_portfolio or ctx.preemption_blocks_strategy:
        return EntryDecision(False, "preemption_escalation")
    # 1a. Pre-flight reconciliation lock (E1, store #2) -- global block, all actions.
    if ctx.pre_flight_recon_locked:
        return EntryDecision(False, "pre_flight_recon_lock")
    # 1b. Local risk halt (E1, store #5 local source-of-truth) -- global block, all actions.
    if ctx.risk_halted:
        return EntryDecision(False, "risk_halt")
    # 2. Portfolio halt (global) -- blocks all actions.
    if ctx.portfolio_halted:
        return EntryDecision(False, "portfolio_halt")
    # 3. Strategy halt -- entry actions only.
    if ctx.strategy_halted and is_entry:
        return EntryDecision(False, "strategy_halt")
    # 4. Capital / pre-open consumption gate -- entry actions only.
    if ctx.capital_gate_block_new_entries and is_entry:
        return EntryDecision(False, "capital_gate_block_new_entries")
    # 5. Degradation -- entry actions only, exempted by an active force-liquidation plan.
    if ctx.degradation_block_new_entries and is_entry and not ctx.force_liquidation:
        return EntryDecision(False, "degradation_block_entries")
    # 6. Leg-level gates.
    if ctx.safe_mode:
        return EntryDecision(False, "safe_mode")
    if ctx.commissioning_gate_blocked:
        return EntryDecision(False, "commissioning_fail_closed")

    return EntryDecision(True, None)
