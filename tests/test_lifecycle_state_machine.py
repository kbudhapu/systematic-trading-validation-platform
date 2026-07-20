"""G2.3 lifecycle state machine + demotion monitors (LLD sections 1/4).

State-machine tests cover legal-transition enforcement, operator-demote-anytime /
no-operator-promote-past-gate, and DiagnosticReport emission. Monitor calibration
uses ground-truth-by-construction synthetic series."""
from __future__ import annotations

import numpy as np
import pytest

from src.lifecycle.demotion_monitors import (
    Tier, cost_divergence_monitor, cusum_drift, drawdown_bound, hit_rate_monitor,
    integrity_trip, rolling_sharpe_monitor, watch_persistence_trip,
)
from src.lifecycle.state_machine import (
    IllegalTransition, LifecycleState, LifecycleStateMachine, sizing_factor,
)


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

def test_ladder_promotions_legal_and_emit_reports() -> None:
    reports: list[dict] = []
    sm = LifecycleStateMachine("qqq", state=LifecycleState.CANDIDATE, report_sink=reports.append)
    sm.transition(LifecycleState.VALIDATED, reason="vtd_pass")
    sm.transition(LifecycleState.PAPER, reason="operator_go", actor="system")
    sm.transition(LifecycleState.ACTIVE, reason="paper_criteria_met")
    assert sm.state == LifecycleState.ACTIVE
    assert [r["to_state"] for r in reports] == ["VALIDATED", "PAPER", "ACTIVE"]
    assert all(r["kind"] == "lifecycle_transition" for r in reports)


def test_skipping_a_gate_is_illegal() -> None:
    sm = LifecycleStateMachine("qqq", state=LifecycleState.CANDIDATE)
    with pytest.raises(IllegalTransition):
        sm.transition(LifecycleState.ACTIVE, reason="skip")   # CANDIDATE -> ACTIVE skips gates


def test_operator_may_demote_anytime() -> None:
    sm = LifecycleStateMachine("qqq", state=LifecycleState.ACTIVE)
    sm.transition(LifecycleState.SAFE_MODE, reason="kill_switch", actor="operator")
    assert sm.state == LifecycleState.SAFE_MODE and sm.safe_mode_episodes == 1
    sm2 = LifecycleStateMachine("spy", state=LifecycleState.PAPER)
    sm2.transition(LifecycleState.RETIRED, reason="operator_retire", actor="operator")
    assert sm2.state == LifecycleState.RETIRED


def test_operator_may_not_promote_past_a_gate() -> None:
    sm = LifecycleStateMachine("qqq", state=LifecycleState.PAPER)
    with pytest.raises(IllegalTransition):
        sm.transition(LifecycleState.ACTIVE, reason="force", actor="operator")


def test_retired_is_terminal() -> None:
    sm = LifecycleStateMachine("qqq", state=LifecycleState.RETIRED)
    with pytest.raises(IllegalTransition):
        sm.transition(LifecycleState.ACTIVE, reason="revive", actor="operator")


def test_watch_sizing_factor_flows_to_sizing() -> None:
    """Calibration (d): WATCH applies a x0.5 sizing factor."""
    assert sizing_factor(LifecycleState.ACTIVE) == 1.0
    assert sizing_factor(LifecycleState.WATCH) == 0.5


# --------------------------------------------------------------------------- #
# Monitor calibration (ground truth by construction)
# --------------------------------------------------------------------------- #

MU0, SIGMA = 0.002, 0.02   # registered OOS weekly mean / std


def test_cusum_trips_on_grinding_decay_within_bounded_window() -> None:
    """(a) planted grinding decay (mean shifted -0.75 sigma) -> CUSUM trips within
    a bounded window."""
    rng = np.random.default_rng(1)
    decayed = rng.normal(MU0 - 0.75 * SIGMA, SIGMA, 60)
    v = cusum_drift(decayed, MU0, SIGMA)
    assert v.tier == Tier.SAFE_MODE
    assert v.detail["trip_index"] is not None and v.detail["trip_index"] <= 40


def test_cusum_no_false_positive_on_healthy_series() -> None:
    """(b) healthy series matching OOS stats -> NO trip across 10 seeds."""
    trips = 0
    for seed in range(10):
        rng = np.random.default_rng(100 + seed)
        healthy = rng.normal(MU0, SIGMA, 60)
        if cusum_drift(healthy, MU0, SIGMA).tripped:
            trips += 1
    assert trips == 0, f"CUSUM false-positive on healthy series: {trips}/10"


def test_single_fat_tail_week_cusum_silent_but_dd_breaches() -> None:
    """(c) a single -4 sigma week does NOT trip CUSUM, but a planted drawdown
    breach DOES trip the drawdown bound."""
    # a series exactly at the validated mean except one -4 sigma week isolates the
    # single-event response: S jumps to ~3.5 (< h=5) then decays -> no CUSUM trip.
    returns = [MU0] * 40
    returns[20] = MU0 - 4.0 * SIGMA               # one severe week
    v = cusum_drift(returns, MU0, SIGMA)
    assert v.tier == Tier.NONE and v.detail["s_max"] < 5.0

    # equity curve with a single deep drawdown > 1.25 x OOS MaxDD (oos=8%)
    equity = [100.0] * 10 + [100.0, 88.0] + [90.0] * 8   # ~12% peak-to-trough
    assert drawdown_bound(equity, oos_max_dd=0.08, multiplier=1.25).tier == Tier.SAFE_MODE


def test_unarmed_monitors_stay_silent_on_short_history() -> None:
    """(e) unarmed monitors stay silent on short history."""
    short_returns = [0.0] * 5     # far below the 26wk window / 12 armed weeks
    assert rolling_sharpe_monitor(short_returns, oos_ci_lower=0.5).detail["armed"] is False
    assert rolling_sharpe_monitor(short_returns, oos_ci_lower=0.5).tier == Tier.NONE
    short_trades = [True] * 10    # < 40 armed trades
    assert hit_rate_monitor(short_trades, oos_hit_rate=0.55).detail["armed"] is False
    assert hit_rate_monitor(short_trades, oos_hit_rate=0.55).tier == Tier.NONE


def test_rolling_sharpe_watch_on_sustained_underperformance() -> None:
    below = [MU0 - 2 * SIGMA] * 40   # persistently negative -> low rolling Sharpe
    v = rolling_sharpe_monitor(below, oos_ci_lower=0.5, window=26, breach_consecutive=4)
    assert v.tier == Tier.WATCH


def test_cost_divergence_watch_after_consecutive_days() -> None:
    modeled = [0.001] * 15
    realized = [0.001] * 3 + [0.002] * 12    # 12 consecutive days at 2x > 1.5x
    assert cost_divergence_monitor(realized, modeled).tier == Tier.WATCH
    # a single spike does not trip
    realized2 = [0.001] * 7 + [0.003] + [0.001] * 7
    assert cost_divergence_monitor(realized2, modeled).tier == Tier.NONE


def test_integrity_trip_instant_safe_mode() -> None:
    assert integrity_trip(hash_ok=True, parity_ok=True, reconciliation_ok=True).tier == Tier.NONE
    assert integrity_trip(hash_ok=False, parity_ok=True, reconciliation_ok=True).tier == Tier.SAFE_MODE


def test_watch_persistence_escalates_to_safe_mode() -> None:
    assert watch_persistence_trip(7, max_weeks=8).tier == Tier.NONE
    assert watch_persistence_trip(8, max_weeks=8).tier == Tier.SAFE_MODE
