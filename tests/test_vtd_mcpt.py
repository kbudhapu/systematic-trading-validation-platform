"""VTD Task 2 -- MCPT calibration. The module is DONE only when it kills the
known-good planted edge (low p) and clears pure noise (high p), and when the
permutation preserves per-session moments/volume while destroying autocorr and
never crossing a session boundary."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import polars as pl

from src.research.vtd.mcpt import mcpt_pvalue, permute_bars

_LOOKBACK = 10


# ---- a picklable black-box strategy: trend-following total log-PnL ------------
def momentum_pnl(bars: pl.DataFrame) -> float:
    """Long when price is above its `_LOOKBACK`-bar-ago level, else short; score
    is total realised log-PnL. Rewards positive return autocorrelation (a real,
    exploitable edge) and scores ~0 when temporal order is destroyed."""
    c = bars["close"].to_numpy().astype(np.float64)
    if len(c) < _LOOKBACK + 2:
        return 0.0
    logc = np.log(c)
    nxt = np.diff(logc)                              # nxt[t] = r_{t->t+1}
    pos = np.sign(c[_LOOKBACK:-1] - c[: -_LOOKBACK - 1])
    return float(np.dot(pos, nxt[_LOOKBACK:]))


def _bars_from_returns(returns: np.ndarray, *, start: datetime, step_min: int = 15,
                       session_len: int | None = None) -> pl.DataFrame:
    """Build an OHLCV frame from a log-return path. If session_len is given,
    insert a large timestamp gap every session_len bars (new session)."""
    n = len(returns) + 1
    close = 100.0 * np.exp(np.concatenate(([0.0], np.cumsum(returns))))
    ts = []
    t = start
    for i in range(n):
        if session_len and i > 0 and i % session_len == 0:
            t = t + timedelta(days=1)               # overnight gap -> new session
        else:
            t = t + timedelta(minutes=step_min) if i > 0 else t
        ts.append(t)
    rng = np.random.default_rng(0)
    hi = close * (1.0 + np.abs(rng.normal(0, 0.0005, n)))
    lo = close * (1.0 - np.abs(rng.normal(0, 0.0005, n)))
    return pl.DataFrame({
        "timestamp": ts, "open": close, "high": np.maximum(hi, close),
        "low": np.minimum(lo, close), "close": close,
        "volume": (np.arange(n, dtype=float) + 1.0) * 10.0,
    })


def _ar1_returns(n: int, phi: float, sigma: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, sigma, n)
    r = np.empty(n)
    r[0] = eps[0]
    for t in range(1, n):
        r[t] = phi * r[t - 1] + eps[t]
    return r


def _trend_returns(n: int, leg: int, drift: float, sigma: float, seed: int) -> np.ndarray:
    """Square-wave regime: +drift for `leg` bars, then -drift for `leg` bars,
    plus small noise. The regime length (leg) is deliberately longer than the
    strategy lookback/block, so a trend rule earns a genuine edge that a
    block-permutation (block=lookback) destroys."""
    rng = np.random.default_rng(seed)
    sign = np.where((np.arange(n) // leg) % 2 == 0, 1.0, -1.0)
    return sign * drift + rng.normal(0, sigma, n)


def test_planted_edge_low_pvalue() -> None:
    """A regime trend (leg=40 bars >> block=lookback=10): the trend strategy has
    a genuine edge, so its real score sits in the far right tail of the
    permutation null -> p <= 0.01. Block-matching preserves the innocent
    within-block autocorrelation but still destroys the exploitable regime, which
    is exactly the kill-gate behaviour the doctrine specifies."""
    r = _trend_returns(600, leg=40, drift=0.002, sigma=0.0005, seed=42)
    bars = _bars_from_returns(r, start=datetime(2020, 1, 2, 9, 30))
    res = mcpt_pvalue(momentum_pnl, bars, n_perm=300, seed=7,
                      block="auto", lookback=_LOOKBACK)
    print(f"\nplanted-edge MCPT p = {res['p_value']:.4f} "
          f"(real={res['real_stat']:.4f}, n_ge={res['n_ge']}/{res['n_perm']})")
    assert res["p_value"] <= 0.01, f"planted edge should give low p, got {res['p_value']:.4f}"


def test_pure_noise_high_pvalue_median() -> None:
    """i.i.d. returns (no exploitable structure): the real score is just one draw
    from the null, so the median p over 10 seeds is >= 0.20."""
    ps = []
    for seed in range(10):
        rng = np.random.default_rng(1000 + seed)
        r = rng.normal(0, 0.004, 500)
        bars = _bars_from_returns(r, start=datetime(2020, 1, 2, 9, 30))
        res = mcpt_pvalue(momentum_pnl, bars, n_perm=150, seed=seed,
                          block="auto", lookback=_LOOKBACK)
        ps.append(res["p_value"])
    med = float(np.median(ps))
    print(f"\npure-noise MCPT median p = {med:.3f} over 10 seeds: "
          f"{[round(p, 3) for p in ps]}")
    assert med >= 0.20, f"pure noise should give high median p, got {med:.3f}"


def test_permutation_preserves_moments_and_volume_destroys_autocorr() -> None:
    """Calibration (c): a block=1 permutation preserves the per-session increment
    multiset (hence moments) and volume exactly, and drives return autocorrelation
    to ~0."""
    r = _ar1_returns(500, phi=0.8, sigma=0.003, seed=5)   # high autocorr original
    bars = _bars_from_returns(r, start=datetime(2020, 1, 2, 9, 30))
    perm = permute_bars(bars, block=1, rng=np.random.default_rng(3))

    orig_c = bars["close"].to_numpy(); perm_c = perm["close"].to_numpy()
    # per-bar close increments including the anchor (first bar unchanged -> 0)
    incr_orig = np.concatenate(([0.0], np.diff(np.log(orig_c))))
    incr_perm = np.concatenate(
        ([np.log(perm_c[0]) - np.log(orig_c[0])], np.diff(np.log(perm_c))))
    assert np.allclose(np.sort(incr_orig), np.sort(incr_perm), atol=1e-9), \
        "increment multiset (hence per-session moments) must be preserved exactly"
    # volume multiset + total preserved
    assert np.isclose(bars["volume"].sum(), perm["volume"].sum())
    assert np.allclose(np.sort(bars["volume"].to_numpy()),
                       np.sort(perm["volume"].to_numpy()))

    def acf1(x: np.ndarray) -> float:
        x = x - x.mean()
        return float((x[:-1] * x[1:]).sum() / (x * x).sum())

    o_ret = np.diff(np.log(orig_c)); p_ret = np.diff(np.log(perm_c))
    print(f"\nacf1 original={acf1(o_ret):.3f} -> permuted={acf1(p_ret):.3f}")
    assert acf1(o_ret) > 0.5, "original series must be strongly autocorrelated"
    assert abs(acf1(p_ret)) < 0.10, "permutation must destroy autocorrelation (~0)"


def test_permutation_respects_session_boundaries() -> None:
    """Calibration (d): with three sessions, no block spans a boundary -- proven
    by the anchor bar of each session staying fixed and each session's increment
    multiset being preserved within itself (no cross-session leakage)."""
    r = _ar1_returns(299, phi=0.4, sigma=0.003, seed=9)
    bars = _bars_from_returns(r, start=datetime(2020, 1, 2, 9, 30), session_len=100)
    perm = permute_bars(bars, block=5, rng=np.random.default_rng(11))

    # reconstruct sessions the same way the module does (gap > 1.5x source)
    from src.research.psd.resample import _source_interval_minutes, _with_sessions
    src = _source_interval_minutes(bars["timestamp"])
    sess = _with_sessions(bars, src)["_session"].to_list()
    assert len(set(sess)) == 3, f"expected 3 sessions, got {len(set(sess))}"

    oc = bars["close"].to_numpy(); pc = perm["close"].to_numpy()
    ts = bars["timestamp"].to_list()
    assert perm["timestamp"].to_list() == ts, "timestamps must be held fixed"

    for s in sorted(set(sess)):
        idx = [i for i, v in enumerate(sess) if v == s]
        a = idx[0]
        assert np.isclose(oc[a], pc[a]), f"session {s} anchor bar must be unchanged"
        # within-session increment multiset preserved (no bar from another session)
        oi = np.concatenate(([0.0], np.diff(np.log(oc[idx]))))
        pi = np.concatenate(([np.log(pc[a]) - np.log(oc[a])], np.diff(np.log(pc[idx]))))
        assert np.allclose(np.sort(oi), np.sort(pi), atol=1e-9), \
            f"session {s} increments leaked across boundary"


def test_determinism_same_seed() -> None:
    r = _ar1_returns(300, phi=0.4, sigma=0.003, seed=1)
    bars = _bars_from_returns(r, start=datetime(2020, 1, 2, 9, 30))
    a = mcpt_pvalue(momentum_pnl, bars, n_perm=120, seed=13, block="auto", lookback=_LOOKBACK)
    b = mcpt_pvalue(momentum_pnl, bars, n_perm=120, seed=13, block="auto", lookback=_LOOKBACK)
    assert a["p_value"] == b["p_value"] and a["n_ge"] == b["n_ge"]


def test_pvalue_formula_bounds() -> None:
    """p = (1+n_ge)/(1+n_perm): min 1/(1+n_perm) when nothing beats real."""
    r = _ar1_returns(200, phi=0.6, sigma=0.003, seed=2)
    bars = _bars_from_returns(r, start=datetime(2020, 1, 2, 9, 30))
    res = mcpt_pvalue(momentum_pnl, bars, n_perm=100, seed=3, block="auto", lookback=_LOOKBACK)
    assert res["p_value"] >= 1.0 / (1.0 + res["n_perm"]) - 1e-12
    assert res["p_value"] <= 1.0
