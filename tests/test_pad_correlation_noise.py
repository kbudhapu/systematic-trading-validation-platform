"""PAD audit P4 — correlation-estimator noise at PAD's window lengths (seeded Monte Carlo).

AUDIT ONLY. Quantifies whether PAD's correlation thresholds are statistically meaningful at
the doctrine window lengths, using the REAL cluster_brain._corr:
  - admission gate: |rho| < max_abs_corr (0.35) over the >=52-week overlap window
  - convergence watch: rho > 0.6 for 4 consecutive 26-week rolling windows
Reports false-reject / false-positive / false-admit rates. See docs/PAD_AUDIT_2026-07.md (PB-4).
"""
from __future__ import annotations

import numpy as np

from src.portfolio.cluster_brain import _corr

TRIALS = 10_000


def _iid(rng, n):
    return rng.standard_normal(n)


def _correlated(rng, n, rho):
    x = rng.standard_normal(n)
    y = rho * x + np.sqrt(max(0.0, 1.0 - rho * rho)) * rng.standard_normal(n)
    return x, y


def test_p4_1_admission_false_reject_on_true_diversifier():
    """Truly uncorrelated legs: P(|rho_hat| >= 0.35) = admission false-reject rate.
    Reported at the real admission window (52wk) and, for contrast, at 26wk."""
    for n in (52, 26):
        rng = np.random.default_rng(100 + n)
        rejects = sum(abs(_corr(*(_iid(rng, n), _iid(rng, n)))) >= 0.35 for _ in range(TRIALS))
        rate = rejects / TRIALS
        print(f"\n[P4.1] admission false-reject @ n={n}wk: {rate:.4f}")
        if n == 52:
            assert rate < 0.05, "admission gate should not eat true diversifiers at 52wk"
        if n == 26:
            # documents that a 26wk window would be materially noisier
            assert rate > 0.03


def test_p4_1b_convergence_false_positive_on_healthy_legs():
    """Truly uncorrelated legs over a 1-year book: P(rho_hat >= 0.6 for 4 consecutive
    26wk-rolling weeks) = convergence-watch false-positive rate."""
    rng = np.random.default_rng(202)
    W, WEEKS = 26, 78  # 78 weekly obs -> 53 rolling 26wk windows over ~1.5yr
    fp = 0
    for _ in range(TRIALS):
        a, b = _iid(rng, WEEKS), _iid(rng, WEEKS)
        run = 0
        hit = False
        for end in range(W, WEEKS + 1):
            if _corr(a[end - W:end], b[end - W:end]) > 0.6:
                run += 1
                if run >= 4:
                    hit = True
                    break
            else:
                run = 0
        fp += hit
    rate = fp / TRIALS
    print(f"[P4.1b] convergence false-positive (0.6x4wk, 26wk roll, ~1.5yr): {rate:.4f}")
    assert rate < 0.10, "convergence watch should rarely fire on genuinely healthy legs"


def test_p4_2_admission_false_admit_of_rho_0p5_leg():
    """A genuinely rho=0.5 leg: P(|rho_hat| < 0.35) = admission FALSE-ADMIT rate at 52wk."""
    rng = np.random.default_rng(303)
    admits = 0
    for _ in range(TRIALS):
        x, y = _correlated(rng, 52, 0.5)
        if abs(_corr(x, y)) < 0.35:
            admits += 1
    rate = admits / TRIALS
    print(f"[P4.2] admission FALSE-ADMIT of a true rho=0.5 leg @ 52wk: {rate:.4f}")
    # this is the material one: a real 0.5-correlated leg slips the <0.35 gate ~10% of the time
    assert 0.03 < rate < 0.25, "false-admit of a rho=0.5 leg is material (documented in PB-4)"
