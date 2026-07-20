"""Unitized per-leg return series (L2) — the PURE math, no I/O.

The problem: a leg's capital base moves for two totally different reasons — P&L, and the
portfolio brain REALLOCATING risk budget between legs. A naive "return = pnl / capital" curve
jumps on every reallocation. Unitization (open-end fund accounting) separates them:

  * P&L moves NAV:            nav += pnl_delta / units
  * reallocation moves UNITS: units = capital_base / nav   (issued/redeemed AT the current NAV)

so a pure reallocation leaves the curve exactly flat BY CONSTRUCTION (asserted in
tests/test_leg_return.py::test_pure_reallocation_leaves_nav_flat). indexed_nav starts at 100;
cumulative_return_pct = nav/100 - 1 is a time-weighted return, neutral to capital flows.
"""
from __future__ import annotations

from dataclasses import dataclass

START_NAV = 100.0


@dataclass(frozen=True)
class LegNavState:
    """The unitization state carried between telemetry ticks (mirrored per row)."""

    units: float
    nav: float          # indexed NAV (starts at START_NAV)
    pnl_cum: float      # cumulative dollar P&L last seen (realized + unrealized)
    capital_base: float # budgets[leg] × portfolio equity last seen


def next_nav_state(
    prev: LegNavState | None,
    pnl_cum: float,
    capital_base: float,
) -> LegNavState:
    """Advance the unitization by one observation.

    Order matters and is deliberate: P&L is applied to NAV FIRST (earned on the units that were
    outstanding while it accrued), THEN the capital base re-targets units at the NEW nav. A pure
    reallocation (pnl_delta == 0) therefore cannot move NAV, and a P&L move cannot be diluted by
    a same-tick reallocation.
    """
    if prev is None or prev.units <= 0.0 or prev.nav <= 0.0:
        # First sight of the leg (or a pathological prior): seed at START_NAV. Units represent
        # the current capital base; zero capital seeds zero units (NAV still defined).
        units = capital_base / START_NAV if capital_base > 0 else 0.0
        return LegNavState(units=units, nav=START_NAV, pnl_cum=pnl_cum, capital_base=capital_base)

    pnl_delta = pnl_cum - prev.pnl_cum
    nav = prev.nav + (pnl_delta / prev.units)
    if nav <= 0.0:
        # A leg cannot lose more than everything in one tick without the series being broken —
        # clamp and let the audit columns (dollar_pnl/capital_base) show the raw truth.
        nav = 1e-9

    # Reallocation: issue/redeem units at the CURRENT nav — NAV unchanged by construction.
    units = capital_base / nav if capital_base > 0 else prev.units

    return LegNavState(units=units, nav=nav, pnl_cum=pnl_cum, capital_base=capital_base)


def cumulative_return_pct(state: LegNavState) -> float:
    """Time-weighted cumulative return implied by the indexed NAV, in percent points."""
    return (state.nav / START_NAV - 1.0) * 100.0
