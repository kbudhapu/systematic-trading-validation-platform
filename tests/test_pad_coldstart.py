"""PAD audit P3 — cold-start admission timeline (doctrine vs reality).

AUDIT ONLY. Confirms: (1) empty active set -> admission vacuously ADMITs; (2) a lone
leg-1 is capped and under-deployed (rest -> cash); (3) leg-2 is DEFERRED until it overlaps
leg-1's REALIZED history by >= min_overlap_weeks (52) — the source of the ~2-year 3-leg
assembly timeline (FINDING PB-3). See docs/PAD_AUDIT_2026-07.md.
"""
from __future__ import annotations

import numpy as np

from src.portfolio.cluster_brain import (
    LegInput,
    PortfolioConfig,
    admission_decision,
    allocate,
)


def _s(scale, n, seed):
    r = np.random.default_rng(seed).standard_normal(n)
    return (r - r.mean()) * scale


def test_p3_1_empty_active_admits_and_leg1_is_capped_underdeployed():
    cfg = PortfolioConfig()
    cand = LegInput("L1", "TREND", _s(0.01, 60, 1), oos_vol=0.01)
    # empty active set -> vacuous ADMIT
    assert admission_decision(cand, [], cfg).verdict == "ADMIT"
    # once ACTIVE alone: single-leg 25% cap binds, remainder -> cash (intended under-deployment)
    res = allocate([cand], cfg)
    print(f"\n[P3.1] lone-leg weight={res.weights['L1']:.4f} cash={res.cash_share:.4f}")
    assert res.weights["L1"] <= 0.25 + 1e-9
    assert abs(res.cash_share - (1.0 - res.weights["L1"])) < 1e-9
    assert res.cash_share >= 0.75 - 1e-9, "under-diversified book is intentionally >=75% cash"


def test_p3_2_leg2_deferred_until_52wk_overlap_with_leg1_realized():
    cfg = PortfolioConfig()  # min_overlap_weeks = 52
    # leg-1 has only 30 REALIZED weeks so far; candidate has a long OOS history (120 wks)
    leg1_realized_30 = LegInput("L1", "TREND", _s(0.01, 30, 2), oos_vol=0.01)
    cand_long = LegInput("L2", "ATTN", _s(0.01, 120, 3), oos_vol=0.01)
    dec_early = admission_decision(cand_long, [leg1_realized_30], cfg)
    print(f"\n[P3.2] leg-1 age=30wk -> verdict={dec_early.verdict} detail={dec_early.detail}")
    assert dec_early.verdict == "DEFER_OVERLAP", "overlap = min(cand, leg1_realized) = 30 < 52"
    # leg-1 now has 52 realized weeks: the overlap gate opens (decorrelated -> not REJECT_CORR)
    leg1_realized_52 = LegInput("L1", "TREND", _s(0.01, 52, 2), oos_vol=0.01)
    cand_dec = LegInput("L2", "ATTN", _s(0.01, 52, 99), oos_vol=0.01)  # independent -> low rho
    dec_open = admission_decision(cand_dec, [leg1_realized_52], cfg)
    rho = float(np.corrcoef(cand_dec.returns[-52:], leg1_realized_52.returns[-52:])[0, 1])
    print(f"[P3.2] leg-1 age=52wk, rho={rho:.3f} -> verdict={dec_open.verdict}")
    assert dec_open.verdict != "DEFER_OVERLAP", "gate opens at 52wk overlap"
