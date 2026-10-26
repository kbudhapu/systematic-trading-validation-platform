"""Combinatorial Purged CV + PBO (doctrine S9).

- cpcv_splits(): combinatorial purged CV split generator. With n_groups=10 and
  n_test_groups=8, there are C(10,8)=45 train/test combinations and
  phi = C(10,8)*8/10 = 36 backtest paths. Reuses Task 3's purge/embargo masking.
- pbo_cscv(): Bailey & Lopez de Prado CSCV logit estimator on an
  (n_slices x n_configs) performance matrix. PBO = fraction of IS/OOS splits
  where the IS-best config lands below the OOS median (logit lambda < 0).
- report_pbo_dsr(): emits PBO and DSR together on identical time segmentation
  (S9 reporting rule).

Compute note: for a real leg, the per-split backtests route through the existing
week-level multiprocessing pool (spawn context, Windows/Ryzen); the droplet is
never used for timing-sensitive CPCV. This module only builds the split
structure and scores a precomputed performance matrix -- no strategy is run here.
"""

from __future__ import annotations

import math
from itertools import combinations
from collections.abc import Sequence

import numpy as np
import structlog

from src.research.psd.purged_wf import purge_embargo_train

log = structlog.get_logger()

_ZERO_TOL = 1e-12          # a per-period return this close to 0 is a structural zero (flat/no-trade)
_RADICAND_EPS = 1e-12      # sigma_SR radicand floor (A1.1); loud log if it binds
_EULER = 0.5772156649015328


def _contiguous_blocks(sorted_idx: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) blocks of a sorted index array."""
    if sorted_idx.size == 0:
        return []
    breaks = np.where(np.diff(sorted_idx) > 1)[0]
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks + 1, [sorted_idx.size]])
    return [(int(sorted_idx[s]), int(sorted_idx[e - 1]) + 1) for s, e in zip(starts, ends)]


def n_cpcv_paths(n_groups: int, n_test_groups: int) -> int:
    """phi = C(n_groups, n_test_groups) * n_test_groups / n_groups."""
    return math.comb(n_groups, n_test_groups) * n_test_groups // n_groups


def cpcv_splits(n_bars: int, n_groups: int = 10, n_test_groups: int = 8,
                e: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """Combinatorial purged CV splits. Partition bars into n_groups contiguous
    groups; each split's test = one C(n_groups, n_test_groups) combination, train
    = the remaining groups with purge+embargo applied at every contiguous test
    block. Returns C(n_groups, n_test_groups) (train_idx, test_idx) pairs."""
    bounds = np.linspace(0, n_bars, n_groups + 1).astype(int)
    groups = [np.arange(bounds[g], bounds[g + 1]) for g in range(n_groups)]
    splits = []
    for test_combo in combinations(range(n_groups), n_test_groups):
        test_idx = np.sort(np.concatenate([groups[g] for g in test_combo]))
        train_groups = [g for g in range(n_groups) if g not in test_combo]
        train_idx = (np.concatenate([groups[g] for g in train_groups])
                     if train_groups else np.array([], dtype=np.int64))
        for (ts, te) in _contiguous_blocks(test_idx):
            train_idx = purge_embargo_train(train_idx, ts, te, e)
        splits.append((np.sort(train_idx), test_idx))
    return splits


def pbo_cscv(perf_matrix: np.ndarray) -> dict:
    """CSCV PBO on an (n_slices x n_configs) performance matrix. Enumerates all
    C(S, S/2) equal IS/OOS partitions of the S slices; for each, ranks configs on
    IS, takes the IS-best, and records its relative OOS rank omega in (0,1);
    lambda = logit(omega). PBO = P(lambda < 0) = fraction where the IS-best is
    below the OOS median. Requires an even number of slices."""
    m = np.asarray(perf_matrix, dtype=np.float64)
    s, ncfg = m.shape
    if s % 2 != 0:
        raise ValueError(f"pbo_cscv needs an even slice count, got {s}")
    half = s // 2
    lambdas = []
    below = 0
    total = 0
    for is_slices in combinations(range(s), half):
        is_set = set(is_slices)
        oos_slices = [i for i in range(s) if i not in is_set]
        is_perf = m[list(is_slices)].mean(axis=0)
        oos_perf = m[oos_slices].mean(axis=0)
        n_star = int(np.argmax(is_perf))
        order = np.argsort(oos_perf)                      # ascending OOS rank
        rank_pos = int(np.where(order == n_star)[0][0])   # 0..ncfg-1
        omega = (rank_pos + 1) / (ncfg + 1)               # in (0,1)
        lambdas.append(math.log(omega / (1.0 - omega)))
        below += int(omega < 0.5)
        total += 1
    return {"pbo": below / total, "n_combinations": total,
            "lambda_mean": float(np.mean(lambdas)), "lambdas": lambdas}


def deflated_sharpe_ratio(sr_annual: float, n_trials: int, n_obs: int,
                          annualization: float = math.sqrt(252 * 26)) -> float:
    """DEPRECATED — Gaussian-sigma_SR CEILING estimate (PSD S9, A1 2026-07-13).

    This is the NORMAL-CASE special form: sigma_SR = sqrt((1 + 0.5*SR^2)/(n-1)), i.e. the full
    Bailey & Lopez de Prado estimator with gamma3=0, gamma4=3 hard-coded. Under real skew/kurtosis
    it OVER-CERTIFIES (one-directional toward FALSE PASS; see knife_edge_bands.dsr_bias / Source B),
    so every DSR it returns is an UPPER BOUND. The signed over-certification is the ``dsr_bias``
    diagnostic in data/research/knife_edge_bands.json, attached to THIS deprecated wrapper only.

    The verdict path must consume :func:`deflated_sharpe_ratio_full` instead (full-moment sigma_SR,
    gamma3/gamma4 estimated from the registered series). This scalar form is retained ONLY as the
    documented ceiling and for backward-compatible callers; its numbers are intentionally unchanged.
    Kept in sync with scripts.chained_backtest._deflated_sharpe_ratio (also annotated deprecated)."""
    from scipy.stats import norm
    if n_trials <= 1 or n_obs <= 1:
        return 0.0
    sr_pbar = sr_annual / annualization
    sigma_sr = math.sqrt((1.0 + 0.5 * sr_pbar ** 2) / max(n_obs - 1, 1))
    q1 = norm.ppf(1.0 - 1.0 / n_trials)
    q2 = norm.ppf(1.0 - math.exp(-1.0) / n_trials)
    sr_star = sigma_sr * ((1.0 - _EULER) * q1 + _EULER * q2)
    z = (sr_pbar - sr_star) / max(sigma_sr, 1e-12)
    return float(norm.cdf(z))


# ---------------------------------------------------------------------------
# FULL-MOMENT DSR (PSD S9 amendment, A1 2026-07-13) -- the estimator the verdict
# path consumes. Full Bailey & Lopez de Prado sigma_SR with gamma3 (skew) and
# gamma4 (kurtosis) ESTIMATED from the strategy's REGISTERED PRIMARY-STATISTIC
# SERIES (T3), and the T2 zero-return substitution guard.
# ---------------------------------------------------------------------------
def _series_moments(returns: Sequence[float]) -> tuple[float, float, int, float]:
    """(gamma3 skew, gamma4 NON-excess kurtosis, n_finite, zero_fraction) of a return series.

    Population (bias=True) standardized moments: gamma4 == 3.0 for a Gaussian (NOT excess). A
    degenerate series (fewer than 4 finite points, or zero variance) returns Gaussian moments
    (0.0, 3.0) so the estimator falls back to the normal case rather than dividing by zero."""
    r = np.asarray(returns, dtype=np.float64)
    r = r[np.isfinite(r)]
    n = int(r.size)
    zero_fraction = float(np.mean(np.abs(r) < _ZERO_TOL)) if n else 1.0
    if n < 4:
        return 0.0, 3.0, n, zero_fraction
    mu = float(r.mean())
    sd = float(r.std(ddof=0))
    if sd <= 0.0:
        return 0.0, 3.0, n, zero_fraction
    z = (r - mu) / sd
    return float(np.mean(z ** 3)), float(np.mean(z ** 4)), n, zero_fraction


def full_moment_sigma_sr(sr_pbar: float, n_obs: int, g3: float, g4: float) -> tuple[float, bool, bool]:
    """Full Jobson-Korkie / Mertens sigma_SR (BLP 2014): V[SR] = (1 - gamma3*SR + (gamma4-1)/4*SR^2)/(n-1).
    Keeps the skew and kurtosis terms the Gaussian special case drops. Returns
    (sigma_sr, kurtosis_floored, radicand_floored). GUARDS (A1.1): gamma4 floored at its mathematical
    lower bound 1 + gamma3^2 (Pearson inequality); the radicand floored at a small positive epsilon.
    Either floor binding is a loud caller-visible flag (never a silent clamp)."""
    lower = 1.0 + g3 * g3                                  # Pearson: kurtosis >= 1 + skew^2 always
    kurtosis_floored = g4 < lower
    g4 = max(g4, lower)
    var = 1.0 - g3 * sr_pbar + ((g4 - 1.0) / 4.0) * sr_pbar ** 2
    radicand_floored = var < _RADICAND_EPS
    var = max(var, _RADICAND_EPS)
    return math.sqrt(var / max(n_obs - 1, 1)), kurtosis_floored, radicand_floored


def deflated_sharpe_ratio_full(returns: Sequence[float], sr_annual: float, n_trials: int,
                               n_obs: int | None = None, *,
                               returns_fallback: Sequence[float] | None = None,
                               annualization: float = math.sqrt(252 * 26),
                               series_kind: str = "per_event") -> dict:
    """Corrected DSR (PSD S9, A1). Full-moment sigma_SR with gamma3/gamma4 ESTIMATED from the
    strategy's REGISTERED PRIMARY-STATISTIC SERIES `returns` (T3 series-pinning).

    T2 ZERO-RETURN GUARD: if `returns`' zero-fraction exceeds 50%, structural zeros inflate kurtosis
    by construction (flatness, not tail risk), which would flip the estimator's harm direction to
    FALSE REJECTION. The moments are then computed on `returns_fallback` (trade-level or daily-
    aggregated) and the substitution is DECLARED. If the guard fires but no fallback is supplied, the
    result is flagged `zero_guard_unsatisfied=True` (loud log) -- never silently trusting the zeros.

    Returns a dict DECLARING every moment + guard used (so the verdict records exactly what produced
    the DSR): dsr, sr_pbar, sigma_sr, g3, g4, kurt_excess, n_obs, zero_fraction, series_substituted,
    series_kind, kurtosis_floored, radicand_floored, zero_guard_unsatisfied, estimator."""
    from scipy.stats import norm
    g3, g4, n_used, zero_fraction = _series_moments(returns)
    substituted = False
    used_kind = series_kind
    zero_guard_unsatisfied = False
    if zero_fraction > 0.5:
        if returns_fallback is not None:
            g3, g4, n_used, _ = _series_moments(returns_fallback)
            substituted = True
            used_kind = "trade_level_or_daily"
        else:
            zero_guard_unsatisfied = True
            log.warning("dsr_zero_return_guard_unsatisfied", zero_fraction=round(zero_fraction, 4),
                        note="T2: >50% structural zeros but no trade-level/daily fallback supplied; "
                             "moments computed on the zero-inflated series are NOT trustworthy")
    n = int(n_obs if n_obs is not None else n_used)
    result = {"dsr": 0.0, "sr_pbar": 0.0, "sigma_sr": 0.0, "g3": g3, "g4": g4,
              "kurt_excess": g4 - 3.0, "n_obs": n, "zero_fraction": zero_fraction,
              "series_substituted": substituted, "series_kind": used_kind,
              "kurtosis_floored": False, "radicand_floored": False,
              "zero_guard_unsatisfied": zero_guard_unsatisfied, "estimator": "full_moment"}
    if n_trials <= 1 or n <= 1:
        return result
    sr_pbar = sr_annual / annualization
    sigma_sr, kf, rf = full_moment_sigma_sr(sr_pbar, n, g3, g4)
    if kf:
        log.warning("dsr_kurtosis_floored", g3=round(g3, 4), g4=round(g4, 4),
                    lower_bound=round(1.0 + g3 * g3, 4), note="A1.1: gamma4 raised to 1+gamma3^2")
    if rf:
        log.warning("dsr_radicand_floored", sr_pbar=round(sr_pbar, 6), g3=round(g3, 4), g4=round(g4, 4),
                    note="A1.1: sigma_SR radicand hit the epsilon floor (near-degenerate variance)")
    q1 = norm.ppf(1.0 - 1.0 / n_trials)
    q2 = norm.ppf(1.0 - math.exp(-1.0) / n_trials)
    sr_star = sigma_sr * ((1.0 - _EULER) * q1 + _EULER * q2)
    z = (sr_pbar - sr_star) / max(sigma_sr, 1e-12)
    result.update({"dsr": float(norm.cdf(z)), "sr_pbar": sr_pbar, "sigma_sr": sigma_sr,
                   "kurtosis_floored": kf, "radicand_floored": rf})
    return result


def report_pbo_dsr(perf_matrix: np.ndarray, best_sr_annual: float,
                   n_trials: int, n_obs: int) -> dict:
    """Emit PBO and DSR together (S9 reporting rule) on the same segmentation."""
    pbo = pbo_cscv(perf_matrix)
    dsr = deflated_sharpe_ratio(best_sr_annual, n_trials, n_obs)
    return {"pbo": pbo["pbo"], "pbo_pass": bool(pbo["pbo"] <= 0.10),
            "dsr": dsr, "n_trials": n_trials, "n_slice_combinations": pbo["n_combinations"]}
