"""L2 — unitized leg return series (pure math; no Supabase).

The single most important property: a PURE REALLOCATION (capital base moves, zero P&L) must not
move the curve. Unitization guarantees it by construction — these tests assert the construction.
"""
from __future__ import annotations

import pytest

from src.control.leg_return import (
    START_NAV,
    LegNavState,
    cumulative_return_pct,
    next_nav_state,
)


def _seed(capital: float = 50_000.0) -> LegNavState:
    return next_nav_state(None, pnl_cum=0.0, capital_base=capital)


# ── the headline assertion (queue L2.1) ─────────────────────────────────────
def test_pure_reallocation_leaves_nav_flat():
    """Inject a pure reallocation (capital 50k -> 80k -> 10k, pnl unchanged): indexed_nav must be
    EXACTLY flat. Reallocations issue/redeem units at the current NAV — the curve moves zero."""
    s = _seed(50_000.0)
    s = next_nav_state(s, pnl_cum=0.0, capital_base=80_000.0)   # brain reallocates UP
    assert s.nav == pytest.approx(START_NAV, abs=1e-12)
    s = next_nav_state(s, pnl_cum=0.0, capital_base=10_000.0)   # brain reallocates DOWN
    assert s.nav == pytest.approx(START_NAV, abs=1e-12)
    assert cumulative_return_pct(s) == pytest.approx(0.0, abs=1e-12)
    # units did move (that's where the reallocation went)
    assert s.units == pytest.approx(10_000.0 / START_NAV)


def test_reallocation_after_gains_still_flat():
    """Reallocation neutrality must hold mid-curve too, not just at NAV=100."""
    s = _seed(50_000.0)
    s = next_nav_state(s, pnl_cum=1_000.0, capital_base=50_000.0)  # +$1k pnl moves NAV
    nav_after_pnl = s.nav
    assert nav_after_pnl > START_NAV
    s = next_nav_state(s, pnl_cum=1_000.0, capital_base=90_000.0)  # pure realloc at higher NAV
    assert s.nav == pytest.approx(nav_after_pnl, abs=1e-12)


# ── P&L moves NAV correctly ──────────────────────────────────────────────────
def test_pnl_moves_nav_proportionally():
    """+1% P&L on the capital base -> +1% NAV (units constant through the P&L application)."""
    s = _seed(50_000.0)
    s = next_nav_state(s, pnl_cum=500.0, capital_base=50_500.0)  # +1% pnl; base grows with it
    assert s.nav == pytest.approx(101.0)
    assert cumulative_return_pct(s) == pytest.approx(1.0)


def test_loss_moves_nav_down():
    s = _seed(20_000.0)
    s = next_nav_state(s, pnl_cum=-400.0, capital_base=19_600.0)  # -2%
    assert s.nav == pytest.approx(98.0)
    assert cumulative_return_pct(s) == pytest.approx(-2.0)


def test_same_tick_pnl_and_reallocation_are_ordered_pnl_first():
    """P&L applies to the units that were outstanding while it accrued; the reallocation then
    re-targets units at the NEW nav. +1% pnl with a simultaneous doubling of capital must still
    print exactly +1% NAV."""
    s = _seed(50_000.0)
    s = next_nav_state(s, pnl_cum=500.0, capital_base=100_000.0)
    assert s.nav == pytest.approx(101.0)
    assert s.units == pytest.approx(100_000.0 / 101.0)


# ── seeding / continuity / edges ─────────────────────────────────────────────
def test_first_sight_seeds_at_100():
    s = _seed(64_000.0)
    assert s.nav == START_NAV
    assert s.units == pytest.approx(640.0)
    assert cumulative_return_pct(s) == 0.0


def test_zero_capital_keeps_prior_units_and_nav():
    """A leg allocated to zero (disabled) neither crashes nor resets — NAV holds, units hold so a
    later pnl print still lands somewhere defensible."""
    s = _seed(50_000.0)
    s = next_nav_state(s, pnl_cum=0.0, capital_base=0.0)
    assert s.nav == pytest.approx(START_NAV)
    assert s.units == pytest.approx(500.0)  # prior units retained


def test_pnl_delta_is_incremental_not_absolute():
    """pnl_cum is CUMULATIVE; the state must difference it, not re-apply it."""
    s = _seed(10_000.0)
    s = next_nav_state(s, pnl_cum=100.0, capital_base=10_100.0)   # +1%
    s = next_nav_state(s, pnl_cum=100.0, capital_base=10_100.0)   # no NEW pnl
    assert s.nav == pytest.approx(101.0)                          # unchanged, not compounded


def test_catastrophic_loss_clamps_not_crashes():
    s = _seed(10_000.0)
    s = next_nav_state(s, pnl_cum=-20_000.0, capital_base=1.0)    # lose 2x the base in a tick
    assert s.nav > 0.0  # clamped, series stays defined; dollar_pnl column carries the raw truth
