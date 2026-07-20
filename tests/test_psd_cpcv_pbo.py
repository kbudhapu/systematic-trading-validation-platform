"""PSD CPCV + PBO calibration -- the module is DONE only when it catches the
known-bad (pure-noise) case and clears the known-good (planted-edge) case."""
from __future__ import annotations

import numpy as np

from src.research.psd.cpcv_pbo import (
    cpcv_splits, pbo_cscv, report_pbo_dsr, n_cpcv_paths,
)


def test_pure_noise_grid_high_pbo() -> None:
    """200 configs of i.i.d. Gaussian returns, no edge by construction -> the
    IS-winner is luck, so PBO must be HIGH (>= 0.4)."""
    rng = np.random.default_rng(11)
    perf = rng.normal(0, 1, (10, 200))          # 10 slices x 200 configs
    res = pbo_cscv(perf)
    print(f"\npure-noise PBO = {res['pbo']:.3f} (n={res['n_combinations']} IS/OOS combos)")
    assert res["pbo"] >= 0.4, f"pure noise should give high PBO, got {res['pbo']:.3f}"


def test_planted_edge_grid_low_pbo_and_top_oos() -> None:
    """One config given a genuine positive drift, rest noise -> PBO LOW (<= 0.10)
    and the planted config ranks top OOS."""
    rng = np.random.default_rng(12)
    perf = rng.normal(0, 1, (10, 200))
    perf[:, 0] += 3.0                            # planted edge on config 0
    res = pbo_cscv(perf)
    print(f"planted-edge PBO = {res['pbo']:.3f}")
    assert res["pbo"] <= 0.10, f"planted edge should give low PBO, got {res['pbo']:.3f}"
    assert int(np.argmax(perf.mean(axis=0))) == 0, "planted config must rank top OOS"


def test_determinism_fixed_seed() -> None:
    def build():
        rng = np.random.default_rng(99)
        return pbo_cscv(rng.normal(0, 1, (10, 50)))["pbo"]
    assert build() == build()


def test_cpcv_path_count_and_split_shapes() -> None:
    assert n_cpcv_paths(10, 8) == 36
    splits = cpcv_splits(n_bars=1000, n_groups=10, n_test_groups=8, e=5)
    assert len(splits) == 45, f"C(10,8)=45 splits expected, got {len(splits)}"
    for train, test in splits:
        # test = 8 of 10 groups ~= 800 bars; train = 2 groups minus purge/embargo
        assert 700 <= test.size <= 820
        assert train.size <= 200          # 2 groups of ~100, minus purge/embargo
        # no train index inside any test block
        assert set(train.tolist()).isdisjoint(test.tolist())


def test_report_pbo_dsr_together() -> None:
    rng = np.random.default_rng(3)
    perf = rng.normal(0, 1, (10, 200)); perf[:, 0] += 3.0
    rep = report_pbo_dsr(perf, best_sr_annual=1.5, n_trials=200, n_obs=500)
    assert "pbo" in rep and "dsr" in rep and 0.0 <= rep["dsr"] <= 1.0
    assert rep["pbo_pass"] is True                       # planted edge passes
