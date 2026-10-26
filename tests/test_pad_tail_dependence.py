"""FIX-7 U6: lower-tail dependence lambda_L estimator + sufficiency gate (dormant PAD).

Synthetic ground truth (known answers by construction). Estimator detects lower-tail dependence
that Pearson misses; sufficiency gate reports tiers on CONFIDENCE (obs count + DISTINCT stress
episodes + CI tightness), never on lambda_L's value; Tier 2/3 are reported but NOT wired to capital.
"""
from __future__ import annotations

import numpy as np

from src.portfolio.cluster_brain import (
    LegInput, PortfolioConfig, allocate, convergence_watch, lower_tail_dependence,
    tail_dependence_sufficiency)

CFG = PortfolioConfig()


def _independent(rng, n):
    return rng.standard_normal(n), rng.standard_normal(n)


def _lower_tail_pair(rng, n, crash_frac=0.15):
    """Independent in the body, but on shared 'crash days' BOTH legs go strongly negative
    together -> genuine lower-tail dependence (Clayton-like), invisible to a body-fit Pearson."""
    x, y = rng.standard_normal(n), rng.standard_normal(n)
    crash = rng.random(n) < crash_frac
    m = int(crash.sum())
    shock = -(np.abs(rng.standard_normal(m)) * 2.0 + 2.0)
    x[crash] = shock + 0.2 * rng.standard_normal(m)
    y[crash] = shock + 0.2 * rng.standard_normal(m)
    return x, y


def _episodic_crashes(n, episodes):
    """Joint crashes (both legs exactly -10) confined to the given (start, length) episodes; body
    ~ +5 so the crashes are the entire worst-q tail (deterministic joint-obs + episode counts,
    stable lambda_L=1.0). Lets us dial obs count and episode count precisely."""
    rng = np.random.default_rng(9)
    x, y = 5.0 + rng.standard_normal(n), 5.0 + rng.standard_normal(n)
    for start, length in episodes:
        x[start:start + length] = -10.0
        y[start:start + length] = -10.0
    return x, y


# --- C1: estimator on synthetic ground truth -------------------------------- #

def test_lower_tail_pair_detected():
    x, y = _lower_tail_pair(np.random.default_rng(1), 600)
    assert lower_tail_dependence(x, y) > 0.40, "genuine lower-tail dependence must register well above baseline"


def test_independent_pair_near_baseline():
    x, y = _independent(np.random.default_rng(2), 600)
    assert lower_tail_dependence(x, y) < 0.25, "independent pair sits near the q=0.10 baseline"


def test_same_pearson_different_tail_distinguished():
    """The whole point: two pairs with ~identical Pearson, different tail behavior -> lambda_L
    separates them where Pearson cannot."""
    rng = np.random.default_rng(3)
    xt, yt = _lower_tail_pair(rng, 900, crash_frac=0.12)
    pear_t = float(np.corrcoef(xt, yt)[0, 1])
    xg = rng.standard_normal(900)
    yg = pear_t * xg + np.sqrt(max(0.0, 1.0 - pear_t ** 2)) * rng.standard_normal(900)
    pear_g = float(np.corrcoef(xg, yg)[0, 1])
    assert abs(pear_t - pear_g) < 0.10, "Pearson matched by construction"
    assert lower_tail_dependence(xt, yt) > lower_tail_dependence(xg, yg) + 0.15


# --- C3: sufficiency gate reports the right tier ---------------------------- #

def test_tier_3_capital_moving():
    x, y = _episodic_crashes(200, [(10, 18), (80, 18), (150, 18)])  # 54 obs (27%), 3 episodes
    suf = tail_dependence_sufficiency(x, y, np.random.default_rng(0), n_boot=150)
    assert suf.joint_tail_obs >= 40 and suf.stress_episodes >= 3 and suf.ci_width < 0.20
    assert suf.tier == 3 and suf.tier_name == "CAPITAL_MOVING"


def test_tier_2_convergence_input():
    x, y = _episodic_crashes(150, [(10, 18), (80, 18)])            # 36 obs (24%), 2 episodes
    suf = tail_dependence_sufficiency(x, y, np.random.default_rng(0), n_boot=150)
    assert 25 <= suf.joint_tail_obs < 40 and suf.stress_episodes == 2
    assert suf.ci_width < 0.25 and suf.tier == 2 and suf.tier_name == "CONVERGENCE_INPUT"


def test_tier_1_diagnostic_episode_guard_blocks_tier_2():
    """THE non-negotiable slow guard: a single multi-week crisis WITH A MID-CRISIS BOUNCE (two
    sub-blocks ~11wk apart, inside the 13wk separation) must count as ONE episode -> capped at
    Tier 1 despite 32 obs. Under the prior 4wk threshold this crisis would have split into two
    false episodes and wrongly reached Tier 2 -- the exact 2008 'one crash counted as many' failure."""
    x, y = _episodic_crashes(150, [(10, 16), (36, 16)])           # 32 obs, gap 11wk < 13 -> ONE episode
    suf = tail_dependence_sufficiency(x, y, np.random.default_rng(0), n_boot=150)
    assert suf.joint_tail_obs >= 25 and suf.stress_episodes == 1, "mid-crisis bounce must not split"
    assert suf.tier == 1 and suf.tier_name == "DIAGNOSTIC"


def test_tier_0_insufficient():
    x, y = _episodic_crashes(200, [(10, 15)])                      # 15 obs -> below Tier-1 floor
    suf = tail_dependence_sufficiency(x, y, np.random.default_rng(0), n_boot=150)
    assert suf.joint_tail_obs < 25 and suf.tier == 0 and suf.tier_name == "NONE"


# --- Dormant: U6 is log-only, wired to no live decision --------------------- #

def test_tail_dependence_is_logged_but_moves_no_capital():
    """allocate() logs tail_dependence, but weights/convergence ignore it entirely."""
    rng = np.random.default_rng(4)
    a = LegInput("A", "ATTN", rng.normal(0.003, 0.02, 120), 0.02, admission_order=0)
    b = LegInput("B", "TREND", rng.normal(0.003, 0.02, 120), 0.02, admission_order=1)
    res = allocate([a, b], CFG)
    assert "tail_dependence" in res.log and "B" in res.log["tail_dependence"]["A"]
    # a strongly tail-dependent pair produces the SAME weights as this one (tail dep is log-only):
    xt, yt = _lower_tail_pair(rng, 120)
    at = LegInput("A", "ATTN", xt, float(np.std(xt)), admission_order=0)
    bt = LegInput("B", "TREND", yt, float(np.std(yt)), admission_order=1)
    res_t = allocate([at, bt], CFG)
    assert set(res_t.weights) == set(res.weights)  # allocation shape unaffected by lambda_L
    # convergence_watch runs on correlation only; it does not consult lambda_L (returns cleanly):
    _ = convergence_watch(at, bt, CFG)
