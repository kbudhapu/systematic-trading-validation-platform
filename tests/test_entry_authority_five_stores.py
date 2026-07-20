"""E1 — the single composed entry authority consults every kill-state store CP6 identified,
and kill (preemption GLOBAL_FLATTEN_AND_HALT) outranks all. Pure, no live path."""
from __future__ import annotations

from src.engine.entry_authority import EntryContext, entry_allowed

# The five canonical kill-state stores (CP6) mapped to their EntryContext block field. The state
# file (circuit_breaker_state.json) folds into the override registry via hydrate, so it is
# represented by portfolio_halted; risk_state is the LOCAL source-of-truth (never the Supabase
# mirror). Each individually MUST block an entry.
FIVE_STORE_BLOCKS = {
    "escalation_latch": dict(preemption_flatten_portfolio=True),
    "human_override_registry": dict(portfolio_halted=True),
    "control_plane_latches": dict(pre_flight_recon_locked=True),
    "state_file_circuit_breaker": dict(portfolio_halted=True),  # hydrates into override registry
    "risk_state_local": dict(risk_halted=True),
}


def test_each_store_individually_blocks_entry():
    for store, fields in FIVE_STORE_BLOCKS.items():
        d = entry_allowed(EntryContext(action="LONG", **fields))
        assert d.allowed is False, f"{store} did not block a LONG entry"


def test_new_stores_block_all_actions_including_exit():
    # pre_flight_recon_lock and risk_halt are global safety locks — they block EXIT too.
    assert entry_allowed(EntryContext(action="EXIT", pre_flight_recon_locked=True)).allowed is False
    assert entry_allowed(EntryContext(action="EXIT", risk_halted=True)).allowed is False
    assert entry_allowed(EntryContext(action="LONG", pre_flight_recon_locked=True)).block_reason == "pre_flight_recon_lock"
    assert entry_allowed(EntryContext(action="LONG", risk_halted=True)).block_reason == "risk_halt"


def test_kill_outranks_all_stores():
    """Preemption (the kill) is highest precedence: it blocks and its reason wins even when every
    other store is simultaneously engaged."""
    d = entry_allowed(EntryContext(
        action="LONG",
        preemption_flatten_portfolio=True,
        pre_flight_recon_locked=True,
        risk_halted=True,
        portfolio_halted=True,
        strategy_halted=True,
        capital_gate_block_new_entries=True,
        degradation_block_new_entries=True,
        safe_mode=True,
        commissioning_gate_blocked=True,
    ))
    assert d.allowed is False
    assert d.block_reason == "preemption_escalation"


def test_clean_context_still_allows():
    assert entry_allowed(EntryContext(action="LONG")).allowed is True


def test_new_fields_default_false_do_not_alter_clean_allow():
    # Regression guard: the two E1 additions must not block a clean leg.
    assert entry_allowed(EntryContext(action="LONG",
                                      pre_flight_recon_locked=False,
                                      risk_halted=False)).allowed is True
