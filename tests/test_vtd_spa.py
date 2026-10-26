"""VTD Task 4 -- Hansen SPA + stepwise Romano-Wolf calibration. The gate is DONE
only when it does NOT flag an all-noise universe (size control / FWER) and DOES
identify exactly the planted superior strategies, deterministically, with the
universe guard raising on a survivors-only universe."""
from __future__ import annotations

import numpy as np
import pytest

from src.research.vtd.spa import (
    UniverseGuardError, assert_full_universe, spa_hansen, stepwise_rw,
)

T, L = 750, 200


def _noise(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 0.01, (T, L))


def test_all_noise_size_and_fwer_control() -> None:
    """200 i.i.d. Gaussian strategies vs a zero-mean (rf=0) benchmark: SPA must
    rarely reject (size ~5%) and stepwise must identify ~zero (FWER ~5%) across
    10 seeds -- mean false rejections <= ~0.05*10 = 0.5."""
    rej = 0
    total_false = 0
    for seed in range(10):
        perf = _noise(seed)
        bench = np.zeros(T)
        rej += int(spa_hansen(perf, bench, n_boot=800, seed=seed)["reject_5pct"])
        total_false += len(stepwise_rw(perf, bench, fwer=0.05, n_boot=800, seed=seed)["superior"])
    print(f"\nall-noise: SPA rejected {rej}/10 seeds; stepwise total false "
          f"rejections = {total_false} (mean {total_false / 10:.2f})")
    # naive per-comparison testing would flag ~0.05*200=10 per seed; control keeps
    # both far below that. Allow MC slack around the 5% nominal level.
    assert rej <= 2, f"SPA over-rejects under the null: {rej}/10"
    assert total_false / 10 <= 0.5, f"stepwise FWER not controlled: mean {total_false / 10}"


def test_planted_superior_identified_exactly_two() -> None:
    """Two genuine-edge strategies planted among 198 noise -> SPA rejects and
    stepwise identifies EXACTLY those two."""
    rng = np.random.default_rng(2024)
    perf = rng.normal(0, 0.01, (T, L))
    perf[:, 7] += 0.0025
    perf[:, 150] += 0.0025
    bench = np.zeros(T)
    spa = spa_hansen(perf, bench, n_boot=1000, seed=3)
    step = stepwise_rw(perf, bench, fwer=0.05, n_boot=1000, seed=3)
    print(f"\nplanted: SPA p={spa['p_value']:.4f} reject={spa['reject_5pct']} "
          f"stepwise superior={step['superior']}")
    assert spa["reject_5pct"] is True, "SPA must reject with two genuine edges present"
    assert step["superior"] == [7, 150], f"stepwise should identify exactly [7,150], got {step['superior']}"


def test_determinism_fixed_seed() -> None:
    perf = _noise(1)
    perf[:, 0] += 0.003
    bench = np.zeros(T)
    a = spa_hansen(perf, bench, n_boot=500, seed=9)
    b = spa_hansen(perf, bench, n_boot=500, seed=9)
    assert a["p_value"] == b["p_value"] and a["statistic"] == b["statistic"]
    s1 = stepwise_rw(perf, bench, n_boot=500, seed=9)
    s2 = stepwise_rw(perf, bench, n_boot=500, seed=9)
    assert s1["superior"] == s2["superior"]


def test_benchmark_variants_rf_and_buy_hold_wired() -> None:
    """Both benchmark variants (rf=0 and a buy-and-hold drift series) are wired
    and produce a valid p-value; a positive-drift benchmark is harder to beat, so
    it should not lower the p-value versus rf=0 for the same strategies."""
    rng = np.random.default_rng(7)
    perf = rng.normal(0.0002, 0.01, (T, L))            # slight positive drift each
    rf = np.zeros(T)
    buy_hold = rng.normal(0.0004, 0.008, T)            # benchmark with drift
    p_rf = spa_hansen(perf, rf, n_boot=500, seed=4)["p_value"]
    p_bh = spa_hansen(perf, buy_hold, n_boot=500, seed=4)["p_value"]
    print(f"\nbenchmark variants: p(rf=0)={p_rf:.3f}  p(buy_hold)={p_bh:.3f}")
    assert 0.0 <= p_rf <= 1.0 and 0.0 <= p_bh <= 1.0
    assert p_bh >= p_rf - 1e-9, "a harder (drift) benchmark should not make rejection easier"


def test_universe_guard_survivors_only_raises() -> None:
    """Doctrine 5.2: a survivors-only universe RAISES (never warns)."""
    with pytest.raises(UniverseGuardError, match="survivors-only"):
        assert_full_universe(120, 200)                 # 120 survivors of a 200 ledger
    # wired through the SPA entry point too
    perf = _noise(0)
    with pytest.raises(UniverseGuardError):
        spa_hansen(perf, np.zeros(T), n_boot=100, seed=0, ledger_universe_size=L + 50)


def test_universe_guard_full_universe_passes() -> None:
    assert_full_universe(200, 200)                     # exact match -> no raise
    perf = _noise(0)
    res = spa_hansen(perf, np.zeros(T), n_boot=200, seed=0, ledger_universe_size=L)
    assert 0.0 <= res["p_value"] <= 1.0


def test_input_validation() -> None:
    with pytest.raises(ValueError):
        spa_hansen(np.zeros(T), np.zeros(T), n_boot=50)          # 1-D perf
    with pytest.raises(ValueError):
        spa_hansen(_noise(0), np.zeros(T + 3), n_boot=50)        # benchmark length mismatch
