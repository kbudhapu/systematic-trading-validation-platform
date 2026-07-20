"""psd_gates.py -- Parameter Selection Doctrine gates P and S, smoothing, and
the point-or-ensemble decision (doctrine sections 2-3).

Operates on a precomputed smoothed performance grid (Polars -> ndarray). No I/O,
no asyncio: pure CPU kernels safe to call from worker processes (spawn context).

Gate P canonical path is the NumPy ravel/meshgrid implementation from the
doctrine (production-adequate for coarse grids per the doctrine's own note). The
doctrine's `tuple_index` Numba sketch is explicitly a non-production stub, so the
Numba variant is OMITTED here (no stub reaches main); the NumPy path is the sole
reference semantics.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
from scipy import ndimage

# ---------------------------------------------------------------------------
# Shared neighbor-smoothed selection (moved here from chained_backtest so both
# the sparse selector and the dense surface smoother live in one module -- the
# doctrine S2 primitive). chained_backtest re-exports this; call sites unchanged.
# ---------------------------------------------------------------------------
def neighbor_smoothed_select(passers: dict, neighbor_keys_fn: Callable) -> object | None:
    """Pick the gate-passing combo with the highest neighbor-smoothed score.

    smoothed_score(c) = 0.5 * calmar(c)
                      + 0.5 * mean(calmar of gate-passing combos exactly one
                                   grid-step from c in any single dimension)

    Zero gate-passing neighbors -> 0.5 * calmar(c). Rewards combos on a plateau of
    jointly-passing neighbors over isolated single-combo spikes. `passers` maps an
    opaque grid-key -> record with a "calmar" float; `neighbor_keys_fn(key)` yields
    one-step neighbor keys (missing ones ignored). Returns the winning key or None.
    """
    best_key = None
    best_score = float("-inf")
    for key, rec in passers.items():
        own = rec["calmar"]
        nb = [passers[nk]["calmar"] for nk in neighbor_keys_fn(key) if nk in passers]
        score = 0.5 * own + (0.5 * (sum(nb) / len(nb)) if nb else 0.0)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key


def _one_step_kernel(ndim: int) -> np.ndarray:
    """3^ndim kernel with 1 at each +/-1 single-axis neighbor, 0 at centre and
    diagonals -- the 'one grid-step in any single dimension' neighborhood."""
    k = np.zeros((3,) * ndim, dtype=np.float64)
    centre = (1,) * ndim
    for d in range(ndim):
        for off in (-1, 1):
            idx = list(centre)
            idx[d] += off
            k[tuple(idx)] = 1.0
    return k


def smooth_surface(grid: np.ndarray) -> np.ndarray:
    """Dense generalization of `neighbor_smoothed_select`'s formula over an N-dim
    performance grid: smoothed[c] = 0.5*grid[c] + 0.5*mean(one-step neighbors).
    Edge cells average over their available neighbors (never halved, since any
    >=2-cell grid gives every cell >=1 neighbor). Uniform kernel, doctrine S2."""
    grid = np.asarray(grid, dtype=np.float64)
    if grid.ndim == 0 or grid.size == 1:
        return grid.copy()
    kern = _one_step_kernel(grid.ndim)
    nsum = ndimage.convolve(grid, kern, mode="constant", cval=0.0)
    ncnt = ndimage.convolve(np.ones_like(grid), kern, mode="constant", cval=0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        nmean = np.where(ncnt > 0, nsum / ncnt, 0.0)
    return np.where(ncnt > 0, 0.5 * grid + 0.5 * nmean, grid)


# ---------------------------------------------------------------------------
# Gate P -- plateau geometry (canonical NumPy path, doctrine section 3)
# ---------------------------------------------------------------------------
def gate_p(perf_grid: np.ndarray, axes_values: Sequence[np.ndarray],
           star_idx: tuple[int, ...],
           rel_band: float = 0.20, floor_ratio: float = 0.70) -> bool:
    """True iff min performance within +/- rel_band per axis (snapped to grid
    nodes) >= floor_ratio * perf(star). perf_grid is the SMOOTHED surface. A
    non-positive plateau centre is auto-fail (doctrine P4)."""
    star = np.asarray(star_idx)
    windows = []
    for d, vals in enumerate(axes_values):
        vals = np.asarray(vals, dtype=np.float64)
        c = vals[star[d]]
        mask = (vals >= c * (1 - rel_band)) & (vals <= c * (1 + rel_band))
        windows.append(np.where(mask)[0])
    star_perf = float(perf_grid[tuple(star_idx)])
    if star_perf <= 0.0:
        return False
    mesh = np.meshgrid(*windows, indexing="ij")
    neighborhood = perf_grid[tuple(m.ravel() for m in mesh)]
    return bool(neighborhood.min() >= floor_ratio * star_perf)


# ---------------------------------------------------------------------------
# Gate S -- Monte-Carlo sensitivity (Alvarez), with coarse-axis safeguard
# ---------------------------------------------------------------------------
def _axis_band_for_min_nodes(vals: np.ndarray, star_i: int, rel_band: float,
                             min_nodes: int = 5) -> float | None:
    """Widen rel_band (from the given start) until >= min_nodes grid nodes fall
    within the star's +/- band. Returns the effective band, or None if the axis
    has fewer than min_nodes nodes total (TOO_COARSE)."""
    if len(vals) < min_nodes:
        return None
    band = rel_band
    c = vals[star_i]
    for _ in range(64):
        n = int(np.sum((vals >= c * (1 - band)) & (vals <= c * (1 + band))))
        if n >= min_nodes:
            return band
        band *= 1.25
        if band >= 2.0:  # full-range and still short -> use whole axis
            return band
    return band


def gate_s(perf_grid: np.ndarray, axes_values: Sequence[np.ndarray],
           star_idx: tuple[int, ...], m_draws: int = 1000,
           rel_band: float = 0.20, seed: int = 42,
           min_nodes: int = 5) -> tuple[str, float]:
    """Returns ('PASS'|'GREY'|'FAIL'|'TOO_COARSE', z). Uniform +/- rel_band draws
    per axis snapped to nearest grid node (grid lookups, zero extra backtests).

    Coarse-axis safeguard (doctrine section 5.5): reports distinct snapped nodes
    per axis; if any axis snaps to < min_nodes distinct nodes, widen that axis's
    band to reach >= min_nodes; if the axis has fewer than min_nodes nodes total,
    return 'TOO_COARSE' -- never a silent PASS.
    """
    rng = np.random.default_rng(seed)
    star = np.asarray(star_idx)
    axes = [np.asarray(v, dtype=np.float64) for v in axes_values]

    eff_bands = []
    for d, vals in enumerate(axes):
        b = _axis_band_for_min_nodes(vals, star[d], rel_band, min_nodes)
        if b is None:
            return "TOO_COARSE", float("nan")
        eff_bands.append(b)

    idx_draws = np.empty((m_draws, len(axes)), np.int64)
    for d, vals in enumerate(axes):
        c = vals[star[d]]
        targets = rng.uniform(c * (1 - eff_bands[d]), c * (1 + eff_bands[d]), m_draws)
        idx_draws[:, d] = np.abs(vals[None, :] - targets[:, None]).argmin(1)

    # distinct snapped nodes per axis -- if still < min_nodes anywhere, too coarse
    for d in range(len(axes)):
        if len(np.unique(idx_draws[:, d])) < min(min_nodes, len(axes[d])):
            if len(axes[d]) < min_nodes:
                return "TOO_COARSE", float("nan")

    perfs = perf_grid[tuple(idx_draws[:, d] for d in range(len(axes)))]
    mu, sd = float(perfs.mean()), float(perfs.std(ddof=1))
    if sd <= 0.0:
        return "PASS", 0.0
    z = (float(perf_grid[tuple(star_idx)]) - mu) / sd
    if z <= 1.0:
        return "PASS", z
    if z <= 2.0:
        return "GREY", z
    return "FAIL", z


# ---------------------------------------------------------------------------
# KNIFE-EDGE STOP (VTD amendment 2026-07-11) -- all scalar-vs-threshold gates.
#
# A verdict whose deciding statistic sits within the DERIVED uncertainty band of
# its own gate is neither PASS nor REJECT: it is KNIFE_EDGE, a HARD STOP surfaced
# to the operator (band + statistic + distance), never silently honored. Bands are
# DERIVED from the estimator, not chosen. This lives in the VERDICT PATH (here),
# NOT in the cross-validation test (whose synthetic fixtures scatter DSR across
# [0,1] and would fire constantly, protecting nothing).
#
# Floors are live IMMEDIATELY; the DSR/PBO "measured" components are tightened once
# W-A leg 2 (tooling queue) supplies the empirical Gumbel/CSCV propagated bands.
# ---------------------------------------------------------------------------
DSR_BAND_FLOOR = 5e-3    # W-A leg-2 measured band replaces this once >5e-3; strategy chat's
                         # original 5e-3 was ~40% narrower than the estimator's own approx error
PBO_BAND_FLOOR = 0.01    # measured CSCV band replaces this once >0.01


def mcpt_band(n_permutations: int) -> float:
    """Derived resolution of an MCPT p-value: one permutation = 1/n."""
    return 1.0 / n_permutations if n_permutations > 0 else float("inf")


def boot_band(n_boot: int) -> float:
    """Derived resolution of an SPA / Reality-Check bootstrap p-value: 1/n_boot."""
    return 1.0 / n_boot if n_boot > 0 else float("inf")


def dsr_band(measured_band: float | None = None) -> float:
    """DSR-vs-0.95 band = max(measured propagated band, 5e-3 floor)."""
    return max(measured_band or 0.0, DSR_BAND_FLOOR)


def pbo_band(measured_band: float | None = None) -> float:
    """PBO-vs-0.10 band = max(measured CSCV band, 0.01 floor)."""
    return max(measured_band or 0.0, PBO_BAND_FLOOR)


def classify_scalar_gate(statistic: float, threshold: float, band: float,
                         higher_is_pass: bool) -> tuple[str, float]:
    """Three-state scalar-gate verdict, ONE-SIDED (corrected 2026-07-11). Returns
    (status, signed_distance), status in {'PASS','REJECT','KNIFE_EDGE'}.

    KNIFE_EDGE fires ONLY on the PASS side within `band` of the gate (a barely-passed verdict, which
    the operator must adjudicate rather than auto-honor). A verdict on the FAIL side is a REJECT and
    STAYS a REJECT -- the band is NEVER widened downward. Converting a clean REJECT into an
    operator-judgment call is gate-LOOSENING (SFD 4.2: automation must never lower/reinterpret a gate);
    under a one-directional over-certifying bias a sub-gate verdict's true value is even worse, so
    rejecting it is if anything more correct. Direction: `higher_is_pass` True (DSR: pass = stat > gate,
    knife zone [gate, gate+band]); False (p / PBO: pass = stat <= gate, knife zone [gate-band, gate])."""
    dist = statistic - threshold
    if higher_is_pass:
        if statistic < threshold:                      # below the gate -> clean REJECT, stays reject
            return ("REJECT", dist)
        if statistic < threshold + band:               # barely passed -> KNIFE_EDGE (pass side only)
            return ("KNIFE_EDGE", dist)
        return ("PASS", dist)
    if statistic > threshold:                          # above the gate -> clean REJECT
        return ("REJECT", dist)
    if statistic > threshold - band:                   # barely passed -> KNIFE_EDGE (pass side only)
        return ("KNIFE_EDGE", dist)
    return ("PASS", dist)


def _resolve_measured_band(kind: str, n: int | None, n_obs: int | None,
                           n_configs: int | None) -> dict:
    """DSR/PBO band from the W-A artifact `data/research/knife_edge_bands.json` (the four contracts live
    in the loader). Falls back to the registered floor + band_source=FLOOR_FALLBACK when the coords or
    the artifact are absent (CONTRACT 3: loud, never silent, never zero). BandDomainError propagates
    (CONTRACT 2: above-domain n_trials HARD STOPs — regenerate, never conservative_max)."""
    from src.research.psd import knife_edge_bands as kb
    if kind == "dsr":
        if n is None:                                  # dsr_band is 1-D on n_trials (Source A); n_obs
            return {"band": DSR_BAND_FLOOR, "band_source": "FLOOR_FALLBACK"}   # is NOT a band coordinate
        return kb.dsr_band_for(n, kb.load_bands())
    if n is None or n_configs is None:
        return {"band": PBO_BAND_FLOOR, "band_source": "FLOOR_FALLBACK"}
    return kb.pbo_band_for(n, n_configs, kb.load_bands())


def _dsr_summed_band(n_trials: int | None, n_obs: int | None, zero_frac: float | None,
                     g3: float | None, g4: float | None, series_kind: str) -> dict:
    """A1 CONSUMED DSR band = SOURCE A (Gumbel approx error, keyed n_trials) + SOURCE C (corrected-
    estimator sampling error, keyed series_kind,n_obs,zero_frac,g3,g4), STRAIGHT SUM. Independent
    error sources on one verdict: max under-covers when both are material; RSS assumes an
    undemonstrated independence structure; the sum is conservative (operator ruling). Source C is only
    added when the FULL-MOMENT coords (g3,g4) are present -- the deprecated Gaussian path has no
    estimated moments, so it gets Source A alone (and the interim |dsr_bias| posture, applied upstream
    until full_moment_live)."""
    from src.research.psd import knife_edge_bands as kb
    payload = kb.load_bands()
    a = _resolve_measured_band("dsr", n_trials, n_obs, None) if n_trials is not None \
        else {"band": DSR_BAND_FLOOR, "band_source": "FLOOR_FALLBACK"}
    if g3 is None or g4 is None:                        # Gaussian path: Source A only (headline unchanged)
        return {"band": a["band"], "band_source": a["band_source"],
                "components": {"source_a": a["band"], "source_a_status": a["band_source"],
                               "source_c": None, "source_c_status": None}}
    c = kb.dsr_estimator_band_for(series_kind, int(n_obs or 0), float(zero_frac or 0.0),
                                  float(g3), float(g4), payload)
    # Headline band_source stays in the known vocabulary (least-certain source wins); the A/C split
    # and per-source provenance live in `components`.
    rank = {"FLOOR_FALLBACK": 0, "CONSERVATIVE_MAX": 1, "MEASURED": 2}
    combined = min((a["band_source"], c["band_source"]), key=lambda s: rank.get(s, 2))
    return {"band": a["band"] + c["band"], "band_source": combined,
            "components": {"source_a": a["band"], "source_a_status": a["band_source"],
                           "source_c": c["band"], "source_c_status": c["band_source"]}}


def knife_edge_verdict(kind: str, statistic: float, threshold: float, *,
                       n: int | None = None, measured_band: float | None = None,
                       n_obs: int | None = None, n_configs: int | None = None,
                       g3: float | None = None, g4: float | None = None,
                       zero_frac: float | None = None, series_kind: str = "per_event") -> dict:
    """Verdict-path entry point for the four scalar gates. `kind` in {'mcpt','spa','dsr','pbo'};
    `n` = n_permutations (mcpt) / n_boot (spa) / n_trials (dsr) / n_slices (pbo). For dsr the consumed
    band is SOURCE A (n_trials) + SOURCE C (series_kind,n_obs,zero_frac,g3,g4) STRAIGHT SUM (A1); pass
    the full-moment moments (g3,g4[,zero_frac,series_kind]) to include Source C -- without them the
    Gaussian path gets Source A alone. `measured_band` still overrides (EXPLICIT). Returns
    {kind,status,statistic,threshold,band,distance,band_source[,band_components]} -- KNIFE_EDGE is a
    HARD STOP for the operator to rule on explicitly."""
    band_source = "DERIVED"
    components = None
    if kind == "mcpt":
        band, higher = mcpt_band(n or 0), False           # pass = p < alpha; band = 1/n_perm
    elif kind == "spa":
        band, higher = boot_band(n or 0), False           # band = 1/n_boot
    elif kind == "dsr":
        higher = True                                     # pass = DSR > 0.95
        if measured_band is not None:
            band, band_source = dsr_band(measured_band), "EXPLICIT"
        else:
            r = _dsr_summed_band(n, n_obs, zero_frac, g3, g4, series_kind)
            band, band_source, components = r["band"], r["band_source"], r["components"]
    elif kind == "pbo":
        higher = False                                    # pass = PBO <= 0.10
        if measured_band is not None:
            band, band_source = pbo_band(measured_band), "EXPLICIT"
        else:
            r = _resolve_measured_band("pbo", n, n_obs, n_configs); band, band_source = r["band"], r["band_source"]
    else:
        raise ValueError(f"unknown scalar gate kind: {kind!r}")
    status, dist = classify_scalar_gate(statistic, threshold, band, higher)
    out = {"kind": kind, "status": status, "statistic": statistic, "threshold": threshold,
           "band": band, "distance": dist, "band_source": band_source}
    if components is not None:
        out["band_components"] = components            # {source_a, source_c} for the summed DSR band
    return out


# ---------------------------------------------------------------------------
# Point-or-Ensemble decision (doctrine S5)
# ---------------------------------------------------------------------------
def _multimodal(smoothed: np.ndarray, frac: float = 0.9) -> tuple[bool, int]:
    """Connected-component labeling of the above-threshold region (threshold =
    frac * smoothed max). >= 2 components -> multimodal."""
    thr = frac * float(smoothed.max())
    mask = smoothed >= thr
    _, n = ndimage.label(mask)
    return (n >= 2, n)


def select_point_or_ensemble(smoothed_grid: np.ndarray,
                             axes_values: Sequence[np.ndarray],
                             gate_s_status: str | None = None,
                             k: int = 10) -> dict:
    """Doctrine S5. Default = point-select the smoothed plateau centre (argmax of
    the smoothed grid). Switch to the top-K equal-weight ensemble when ANY of:
    (a) Gate S is GREY; (b) the plateau is multi-modal (>= 2 disjoint high
    regions at 0.9*max); (c) the surface is 4-dimensional (4 free params).

    Returns {mode, reason, star_idx, members?}: mode in {'point','ensemble'};
    members = list of top-K grid-coord tuples (equal weight) when ensemble.
    """
    smoothed = np.asarray(smoothed_grid, dtype=np.float64)
    star_idx = tuple(int(i) for i in np.unravel_index(int(np.argmax(smoothed)), smoothed.shape))

    multimodal, n_components = _multimodal(smoothed)
    reasons = []
    if gate_s_status == "GREY":
        reasons.append("GREY")
    if multimodal:
        reasons.append("multimodal")
    if smoothed.ndim >= 4:
        reasons.append("4-param")

    if not reasons:
        return {"mode": "point", "reason": "smoothed-plateau-center",
                "star_idx": star_idx, "n_components": n_components}

    flat = smoothed.ravel()
    top = np.argsort(flat)[::-1][:k]
    members = [tuple(int(i) for i in np.unravel_index(int(t), smoothed.shape)) for t in top]
    return {"mode": "ensemble", "reason": "+".join(reasons), "star_idx": star_idx,
            "members": members, "k": len(members), "n_components": n_components}


def ensemble_positions(position_series: Sequence["object"]):
    """Equal-weight average of K per-config position series (Polars Series or
    1-D numpy arrays of equal length) -> the consensus position a live leg trades."""
    import polars as pl
    if not position_series:
        raise ValueError("ensemble_positions: empty ensemble")
    arrs = [np.asarray(s.to_numpy() if isinstance(s, pl.Series) else s, dtype=np.float64)
            for s in position_series]
    n = len(arrs[0])
    if any(len(a) != n for a in arrs):
        raise ValueError("ensemble_positions: position series must be equal length")
    avg = np.mean(np.vstack(arrs), axis=0)
    return pl.Series("ensemble_position", avg)
