"""Harvey-Liu haircut Sharpe -- multiplicity-adjusted reporting (VTD section 2).

A reporting layer (never a pass/fail gate) that discounts an observed Sharpe for
the number of trials that produced it. Given an observed annual Sharpe over T
periods and a trial count N pulled from the PSD trial ledger, it reports the
single-test p-value, its Bonferroni / Holm / BHY multiplicity-adjusted p-values,
and the Sharpe each adjusted p implies at the same T (the "haircut Sharpe").

Reference: Harvey & Liu (2015), "Backtesting"; the quantstrat `haircut.Sharpe`
family. The t-statistic of a Sharpe over T periods is SR_period * sqrt(T); the
haircut inverts the adjusted p back through the same t-distribution.

With only (observed p, N) available -- the ledger stores a *count*, not the other
N-1 statistics -- the corrections take their principled single-observation forms:
Bonferroni min(N*p, 1); Holm equals Bonferroni for the most-significant test
(documented); BHY uses the Benjamini-Yekutieli arbitrary-dependence constant
c(N)=sum_{k=1..N} 1/k, i.e. min(N*c(N)*p, 1). This is a conservative reporting
layer, not the paper's simulation-based reconstruction of the full test set.
"""

from __future__ import annotations

import math
from pathlib import Path

from scipy import stats

from src.research.psd.trial_ledger import cumulative_n_trials

METHODS = ("bonferroni", "holm", "bhy")


def _c_of_n(n: int) -> float:
    """Benjamini-Yekutieli constant c(N) = sum_{k=1}^{N} 1/k (harmonic)."""
    return float(sum(1.0 / k for k in range(1, int(n) + 1))) if n >= 1 else 1.0


def sr_to_pvalue(sr_period: float, T: int) -> float:
    """Two-sided p-value of a per-period Sharpe over T periods.

    Test statistic t = SR_period * sqrt(T), which is asymptotically standard
    normal under H0 (Lo 2002; the Harvey-Liu haircut treatment). SR is in the
    returns' own frequency; annualise separately (SR_annual =
    SR_period * sqrt(periods_per_year))."""
    if T < 2:
        raise ValueError("T must be >= 2")
    t_stat = sr_period * math.sqrt(T)
    return float(2.0 * stats.norm.sf(abs(t_stat)))


def pvalue_to_sr(p: float, T: int) -> float:
    """Per-period Sharpe implied by a two-sided p-value over T periods."""
    if T < 2:
        raise ValueError("T must be >= 2")
    p = min(max(p, 1e-300), 1.0)
    t_stat = stats.norm.isf(p / 2.0)           # inverse survival for two-sided p
    return float(t_stat / math.sqrt(T))


def _adjusted_pvalues(p_single: float, N: int) -> dict:
    p_bonf = min(N * p_single, 1.0)
    p_holm = min(N * p_single, 1.0)            # top-ranked test == Bonferroni
    p_bhy = min(N * _c_of_n(N) * p_single, 1.0)
    return {"bonferroni": p_bonf, "holm": p_holm, "bhy": p_bhy}


def report_haircut(
    sr_annual: float,
    T: int,
    N: int,
    *,
    periods_per_year: float = 12.0,
) -> dict:
    """Full haircut report for an observed annual Sharpe.

    Returns the single-test p, the three adjusted p-values, the three haircut
    annual Sharpes, their haircut percentages, N and T. `periods_per_year` is the
    frequency of the T periods (12 for monthly, 52 weekly, 252 daily).
    """
    if N < 1:
        raise ValueError("N (trial count) must be >= 1")
    ann_factor = math.sqrt(periods_per_year)
    sr_period = sr_annual / ann_factor
    p_single = sr_to_pvalue(sr_period, T)
    adj = _adjusted_pvalues(p_single, N)

    haircut_sr: dict[str, float] = {}
    haircut_pct: dict[str, float] = {}
    for method, p_adj in adj.items():
        sr_p = pvalue_to_sr(p_adj, T)
        sr_a = sr_p * ann_factor
        haircut_sr[method] = sr_a
        haircut_pct[method] = (
            (sr_annual - sr_a) / sr_annual if abs(sr_annual) > 1e-12 else 0.0
        )
    return {
        "sr_annual": sr_annual, "T": T, "N": N, "periods_per_year": periods_per_year,
        "p_single": p_single, "adjusted_p": adj,
        "haircut_sr": haircut_sr, "haircut_pct": haircut_pct,
    }


def report_haircut_for_leg(
    leg_id: str,
    sr_annual: float,
    T: int,
    db_path: str | Path,
    *,
    periods_per_year: float = 12.0,
) -> dict:
    """Convenience: pull N from the PSD trial ledger (cumulative trials for the
    leg) and produce the haircut report. Ties the multiplicity N to the actual
    recorded selection effort, per doctrine section 2 + the trial-ledger design."""
    N = cumulative_n_trials(leg_id, db_path)
    N = max(N, 1)
    report = report_haircut(sr_annual, T, N, periods_per_year=periods_per_year)
    report["leg_id"] = leg_id
    return report
