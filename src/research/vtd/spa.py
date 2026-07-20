"""Hansen (2005) SPA test + stepwise Romano-Wolf identification (VTD Stage 4).

The pre-capital portfolio gate: given a UNIVERSE of candidate strategies and a
benchmark, does ANY strategy genuinely beat the benchmark once data-snooping over
the whole universe is accounted for (Hansen SPA), and if so WHICH ones (stepwise
Romano-Wolf, controlling FWER)? Both use the Politis-Romano stationary bootstrap
(imported from src.research.bootstrap, not forked) with studentized statistics.

Universe guard (adversarial audit #2, doctrine section 5.2): SPA run over
survivors only flatters the book, so the universe MUST be the full ledger
including dead legs. `assert_full_universe` raises (never warns) when the supplied
universe is not the full recorded universe.

Conventions: `perf_matrix` is (T observations x L strategies) of per-period
returns; `benchmark_returns` is length T. Larger return = better. Benchmark
variants are just different arrays: rf=0 -> zeros; buy-and-hold -> the drift
series (both wired in the calibration).
"""

from __future__ import annotations

import numpy as np

from src.research.bootstrap import (
    BOOTSTRAP_DEFAULT_MEAN_BLOCK_LENGTH, stationary_bootstrap_resample,
)

_EPS = 1e-12


class UniverseGuardError(AssertionError):
    """Raised when the SPA universe is not the full recorded ledger universe."""


def assert_full_universe(universe_size: int, ledger_recorded_universe: int) -> None:
    """Assert the supplied universe equals the full recorded ledger universe for
    the scope. Survivors-only (a smaller universe) RAISES, per doctrine 5.2 -- it
    is never a warning, because a quietly shrunk universe is exactly how SPA gets
    gamed (adversarial audit #2)."""
    u, n = int(universe_size), int(ledger_recorded_universe)
    if u != n:
        detail = " (survivors-only universe)" if u < n else " (universe larger than ledger)"
        raise UniverseGuardError(
            f"SPA universe must be the FULL ledger of {n} strategies including dead "
            f"legs; got {u}{detail}."
        )


def _bootstrap_index_matrix(T: int, n_boot: int, mean_block_length: float,
                            rng: np.random.Generator) -> np.ndarray:
    """(n_boot x T) integer matrix of stationary-bootstrap row indices, reused for
    every strategy so cross-sectional dependence is preserved. Built by resampling
    an index vector with the shared Politis-Romano resampler (genuine reuse)."""
    base = np.arange(T)
    return np.vstack([
        stationary_bootstrap_resample(base, mean_block_length, rng).astype(np.int64)
        for _ in range(n_boot)
    ])


def _core(perf_matrix, benchmark_returns, n_boot, mean_block_length, seed):
    """Shared studentized-loss-differential machinery for SPA and stepwise RW."""
    perf = np.asarray(perf_matrix, dtype=np.float64)
    if perf.ndim != 2:
        raise ValueError("perf_matrix must be 2-D (T observations x L strategies)")
    bench = np.asarray(benchmark_returns, dtype=np.float64)
    T, L = perf.shape
    if bench.shape != (T,):
        raise ValueError(f"benchmark_returns must have length T={T}, got {bench.shape}")
    d = perf - bench[:, None]                      # (T, L) outperformance series
    d_bar = d.mean(axis=0)                          # (L,)

    rng = np.random.default_rng(seed)
    idx = _bootstrap_index_matrix(T, n_boot, mean_block_length, rng)   # (B, T)
    boot_means = np.stack([d[row].mean(axis=0) for row in idx])        # (B, L)

    # omega_k = bootstrap estimate of the std of sqrt(T) * d_bar_k
    omega = np.sqrt(T) * boot_means.std(axis=0, ddof=1)
    omega = np.where(omega < _EPS, _EPS, omega)
    w = np.sqrt(T) * d_bar / omega                  # studentized real stats (L,)
    return {"T": T, "L": L, "d_bar": d_bar, "boot_means": boot_means, "omega": omega, "w": w}


def spa_hansen(
    perf_matrix,
    benchmark_returns,
    *,
    n_boot: int = 1000,
    mean_block_length: float = BOOTSTRAP_DEFAULT_MEAN_BLOCK_LENGTH,
    seed: int = 0,
    ledger_universe_size: int | None = None,
) -> dict:
    """Hansen (2005) consistent SPA test.

    H0: no strategy in the universe beats the benchmark (max_k E[d_k] <= 0).
    Returns {"p_value", "statistic", "reject_5pct", "studentized", "n_boot"}.
    A small p rejects H0 -> at least one genuine outperformer exists.
    """
    c = _core(perf_matrix, benchmark_returns, n_boot, mean_block_length, seed)
    if ledger_universe_size is not None:
        assert_full_universe(c["L"], ledger_universe_size)
    T, w, omega, d_bar, boot_means = c["T"], c["w"], c["omega"], c["d_bar"], c["boot_means"]

    stat = max(float(w.max()), 0.0)
    # consistent recentring: keep (subtract) the mean only for non-badly-negative
    # models; very poor models are excluded from inflating the null.
    threshold = -np.sqrt(2.0 * np.log(np.log(max(T, 3))))
    keep = w >= threshold
    g = np.where(keep, d_bar, 0.0)                  # (L,)
    boot_stat = np.sqrt(T) * (boot_means - g) / omega       # (B, L)
    boot_max = np.maximum(boot_stat.max(axis=1), 0.0)       # (B,)
    p_value = float(np.mean(boot_max >= stat))
    return {
        "p_value": p_value, "statistic": stat, "reject_5pct": p_value < 0.05,
        "studentized": w, "n_boot": n_boot,
    }


def stepwise_rw(
    perf_matrix,
    benchmark_returns,
    *,
    fwer: float = 0.05,
    n_boot: int = 1000,
    mean_block_length: float = BOOTSTRAP_DEFAULT_MEAN_BLOCK_LENGTH,
    seed: int = 0,
    ledger_universe_size: int | None = None,
) -> dict:
    """Romano-Wolf stepwise multiple test: identify WHICH strategies beat the
    benchmark while controlling the family-wise error rate at `fwer`.

    Returns {"superior": sorted indices, "studentized", "n_steps", "n_boot"}.
    """
    c = _core(perf_matrix, benchmark_returns, n_boot, mean_block_length, seed)
    if ledger_universe_size is not None:
        assert_full_universe(c["L"], ledger_universe_size)
    L, w, omega, d_bar, boot_means = c["L"], c["w"], c["omega"], c["d_bar"], c["boot_means"]
    T = c["T"]

    # studentized, mean-recentred bootstrap stats (null of no outperformance)
    boot_std = np.sqrt(T) * (boot_means - d_bar) / omega     # (B, L)

    active = list(range(L))
    superior: list[int] = []
    n_steps = 0
    while active:
        n_steps += 1
        max_null = boot_std[:, active].max(axis=1)           # (B,)
        crit = float(np.quantile(max_null, 1.0 - fwer))
        newly = [k for k in active if w[k] > crit]
        if not newly:
            break
        superior.extend(newly)
        active = [k for k in active if k not in set(newly)]
    return {
        "superior": sorted(superior), "studentized": w,
        "n_steps": n_steps, "n_boot": n_boot,
    }
