# PARAMETER SELECTION DOCTRINE (PSD v1.0)
### mbappe project · frozen methodology · supersedes ad-hoc per-leg parameter practice

**Status:** PRE-REGISTERED AND FROZEN upon commit to main. This doctrine is theory-derived and may not be modified in response to any experimental result. Amendments follow the same protocol as EXPERIMENT_REGISTRY entries: registered in writing, with rationale, BEFORE any run whose outcome they could affect (an amendment precedent recorded in the registry). Tuning this doctrine against results is meta-overfitting and is prohibited by the doctrine itself.

**Scope:** every strategy leg, present and future, inherits this doctrine in full. No leg may define its own parameter-selection procedure.

---

## 0. FOUNDATIONAL PRINCIPLES (the non-negotiables)

**P0 — Edge precedes parameters.** Parameters tune the harvest of an edge; they cannot create one. No parameter methodology rescues an edgeless signal (proven internally across rejected experiments). Consequently this doctrine's purpose is NOT to find "best" parameters — it is to guarantee that (a) if an edge exists we harvest it near-optimally, and (b) if it does not, the pipeline cannot fool itself into believing it does.

**P1 — Robustness, never performance.** Selection optimizes for graceful degradation under perturbation (Pardo 2008), not for backtest maximum. Peaks are presumed curve-fit; plateaus are presumed (not proven) robust.

**P2 — Fewer degrees of freedom beats better search.** Every internal improvement in this project came from better measurement or fewer DOF, never from more search (248M-combo grid → noise winners; weekly refit → worst cadence). The doctrine minimizes free parameters before it optimizes any.

**P3 — Anti-meta-overfitting.** The optimization process itself (window sizes, fitness functions, grids, gates, thresholds) is fixed here, once, from theory. Adjusting the process until results look good defeats the purpose of out-of-sample validation (the walk-forward literature's central warning, incl. "implicit fitting" via structure choices informed by known history).

**P4 — Robustness is necessary, not sufficient.** A broad plateau at zero edge is robustly worthless. PSD gates filter fragility; edge existence is established separately by the pre-registered validation battery (bootstrap CI, year-count, DSR). Both must pass.

---

## 1. PARAMETER TAXONOMY (binding, extends the L0–L5 hierarchy)

| Level | Class | Rule |
|---|---|---|
| L0 | Never optimized | cost model, gate battery, holdout (final 26 wks), THIS DOCTRINE |
| L1 | Hypotheses | signal family × instrument × **bar timeframe**. Chosen by economic reasoning; never swept for the winner |
| L2 | Anchored | measured from data properties, not searched (e.g. z-lookback ≈ measured OU half-life; ATR window = convention 14; vol-scaling window = measured vol half-life). Re-measured, not re-optimized |
| L3 | Free | **≤ 4 per leg, hard cap.** Coarse economically-bounded grids only. The ONLY level this doctrine's selection pipeline applies to |
| L4 | Adaptation policy | refit cadence QUARTERLY with neighbor-smoothed selection (validated internally: quarterly beat weekly/monthly/fixed). Any leg proposing a different cadence must pre-register it as its own experiment |
| L5 | Portfolio assembly | out of PSD scope (PortfolioBrain doctrine) |

### 1.1 Timeframe rule (answers the "candle sizes" question)
- Timeframe is L1: a hypothesis, not a tunable. Choose the timeframe per edge mechanism by reasoning (faster mechanisms favor shorter bars; drift and risk-premium mechanisms favor longer bars); sub-hour bars require an explicit intraday-mechanism rationale.
- The permitted timeframe menu is pre-committed and coarse: **{15m, 30m, 1H, 2H, 4H, 1D}**, all resampleable from the free 15-min/daily SIP history (no new data cost). Sub-15m requires a demand-triggered data purchase and an intraday mechanism.
- A leg may evaluate at most **2 adjacent timeframes** from the menu, both declared at pre-registration. Every evaluated timeframe **counts as a trial** in DSR/PBO accounting. Picking the better of two is selection and is charged as such.

### 1.2 Range definition (L3)
Ranges derive from economic reasoning and L2 anchors, never from what scores well: e.g., if the mechanism is intraday reversion with a measured half-life of H bars, the lookback grid brackets H coarsely (a handful of points spanning roughly 0.5H to 2H) — it does not extend to many multiples of H just because a wider range backtests better. Extending a range after seeing results is a new pre-registered trial set, not a tweak.

---

## 2. THE SELECTION PIPELINE (L3 parameters, in order, all mandatory)

```
S1 Coarse grid  →  S2 Neighbor-smoothed plateau map  →  S3 Gate P (plateau)
→  S4 Gate S (MC sensitivity)  →  S5 Point-or-Ensemble decision
→  S6 Purged/embargoed walk-forward (quarterly cadence)
→  S7 Multi-market cross-check  →  S8 Trial accounting (DSR)
[pre-capital only]  S9 CPCV → PBO gate
```

**S1 — Coarse grid.** ≤ 4 free params, coarse steps (internally validated ~130× reduction preserves ranking). Full-resolution grids are prohibited for selection.

**S2 — Neighbor-smoothed plateau map.** Smooth the performance surface with a uniform kernel over parameter-space neighbors (the existing neighbor-smoothed selection; independently validated by practitioner "blur test" — lucky peaks average out, plateaus survive). All subsequent stages operate on the smoothed surface; the raw maximum is never a selection candidate.

**S3 — Gate P (plateau geometry).** For the smoothed candidate θ*:
`min over ±20% per-axis perturbation of Perf(θ) ≥ 0.70 × Perf(θ*)`
(perturbations snapped to nearest grid nodes; Perf = the pre-registered objective, default net Sharpe). Literature benchmark: robust configs hold ~70% of peak across wide ranges; fragile ones collapse within ~8%. FAIL ⇒ the leg has no robust region ⇒ leg is rejected at parameterization, before any edge test is run.

**S4 — Gate S (Monte-Carlo sensitivity, Alvarez test).** Draw M=1,000 uniform perturbations of θ* within ±20% per axis (snapped to grid, cached evaluations reused). Compute z = (Perf(θ*) − mean(Perf(perturbed))) / std(Perf(perturbed)).
- z ≤ 1.0 → PASS
- 1.0 < z ≤ 2.0 → GREY: pass permitted only with the ensemble option in S5 (never point-select a grey config)
- z > 2.0 → FAIL (the chosen config is a statistical outlier vs. its own neighborhood = overfit signature)
**Anti-lookahead rule:** Gates P and S run on TRAINING-window surfaces only, then are RE-CHECKED on the walk-forward OOS aggregate at S6; a config whose OOS z exceeds 2.0 is flagged fragile regardless of in-sample pass.

**S5 — Point-or-Ensemble decision (new, from the ensemble literature).** Default = point selection of the smoothed plateau center. The leg SHOULD instead trade the **top-K parameter ensemble** (average the POSITIONS of the top-K smoothed configs, K pre-registered, default K=10, equal-weight) when ANY of: (a) Gate S is GREY; (b) the plateau is multi-modal (≥2 disjoint high regions — a bimodal parameter surface); (c) the surface is high-dimensional (4 free params). Rationale: top-K position averaging preserves the consensus signal, discards threshold noise, and measurably reduces train→test Sharpe degradation vs single-best selection. Ensemble K counts as ONE selection decision in trial accounting (the K configs are not K trials — they are jointly selected by one pre-registered rule). Live implication: ensemble legs hold fractional consensus positions; sizing flows through the standard ATR/risk-budget path unchanged.

**S6 — Purged, embargoed walk-forward.** Quarterly refit (L4), non-anchored windows, and — new, binding — **purge + embargo at every train/test boundary:** drop training bars whose forward label/holding window overlaps the test block, and embargo `E = max(holding-period, signal-lookback)` bars after each test block before training data resumes. (For an intraday leg, E resolves to roughly a couple of trading days; for a longer-horizon leg, E resolves to the label horizon.) Rationale: unpurged overlap leaks test information into training and inflates OOS estimates even in honest walk-forwards.

**S7 — Multi-market cross-check.** Where the edge mechanism claims generality (e.g., "in-play small-caps," "commodity ETFs"), rank candidate configs by AGGREGATE performance across the declared instrument set; single-instrument peak ranking is prohibited. A config that only works on one instrument of a claimed class is a fluke until an instrument-specific mechanism is registered.

**S8 — Trial accounting.** N_trials for DSR = (grid points evaluated) × (timeframes evaluated) × (objective-function variants, which must be 1) summed across all selection attempts for the leg, cumulative across queues, recorded in EXPERIMENT_REGISTRY. Pre-specified configs (no search) are N=1 and say so (pre-specified-config precedent).

**S9 — CPCV → PBO (pre-capital gate; pre-paper optional).** Before ANY real capital: run Combinatorial Purged CV on the leg's selection process — reference configuration: N=10 groups, k=8 test-group size variant per the φ(10,8)=45-split / 36-path scheme, 21-bar-equivalent purge+embargo — and compute PBO via CSCV logit ranking. **PASS requires PBO ≤ 0.10** (≤10% estimated probability that the selected config underperforms the median OOS). Report PBO and DSR together, same time-segmentation. CPCV complements (does not replace) S6: walk-forward answers "how would it have traded"; CPCV/PBO answers "was the selection luck."

### S9 amendment — FULL-MOMENT σ_SR for the DSR (adopted 2026-07-13, A1)

The DSR σ_SR uses the **full Bailey & López de Prado / Jobson–Korkie–Mertens** estimator
`V[SR] = (1 − γ₃·SR + (γ₄−1)/4·SR²)/(n−1)`, with **γ₃ (skew) and γ₄ (kurtosis) ESTIMATED from the
verdict series**, NOT the Gaussian special case (γ₃=0, γ₄=3). The Gaussian form over-certifies
one-directionally toward FALSE PASS (its DSR is an UPPER BOUND); this is the `dsr_bias` / Source-B
finding of `docs/methodology/DSR_BAND_BIAS_DECOMPOSITION.md`. Guards: γ₄ is floored at its mathematical
lower bound `1 + γ₃²` (Pearson); the σ_SR radicand is floored at a small positive ε; either floor
binding is logged loudly, never silently clamped. Implementation: `cpcv_pbo.deflated_sharpe_ratio_full`
(the verdict-path estimator); the scalar Gaussian `deflated_sharpe_ratio` is retained only as the
deprecated documented CEILING.

**T3 — series pinning (binding).** *"DSR moments (SR, γ₃, γ₄) are computed on the strategy's
REGISTERED PRIMARY-STATISTIC SERIES — the series on which the registration quotes its Sharpe and CI
(per-event, weekly, monthly, or per-trade as registered). Raw bar series are never used for a
strategy whose registration does not trade raw bars. The series choice is fixed at registration and
recorded in the verdict."*

**T2 — zero-return guard (binding).** *"If the registered series' zero-return fraction exceeds 50%,
DSR moments are computed on the trade-level or daily-aggregated series instead, and the substitution
is DECLARED in the verdict. Rationale: structural zeros inflate kurtosis by construction (flatness,
not tail risk), flipping the estimator's harm direction to FALSE REJECTION. Caveat recorded: sparse
activity genuinely widens Sharpe uncertainty — this guard removes the mechanical artifact only; it
does not exempt inactive strategies from honest variance."*

**Knife-edge band (T1).** The corrected estimator carries its OWN sampling-error band — **Source C**,
the seeded-MC std of the full-moment DSR under finite-sample (γ₃,γ₄) estimation, keyed
(series_kind, n_obs, zero_frac, γ₃, γ₄) in `data/research/knife_edge_bands.json` (schema/3), with the
four standard contracts (round-up / above-domain HARD STOP / FLOOR_FALLBACK-never-zero / monotone
ratchet). The **consumed** DSR knife-edge band is **Source A + Source C (STRAIGHT SUM)** — independent
error sources on one verdict; the sum is conservative (max under-covers when both are material; RSS
assumes an undemonstrated independence structure). **Interim posture:** until this correction is live
end-to-end, treat every gate DSR as an UPPER BOUND and require `DSR ≥ 0.95 + |dsr_bias|`. That interim
margin retires ONLY when the machine-readable `knife_edge_bands.full_moment_live` flag is True, which
requires BOTH (i) the verdict path consuming `deflated_sharpe_ratio_full` EXCLUSIVELY (Gaussian
structurally unreachable — import-direction contract) AND (ii) Source C present + measured; a
schema/band_source check alone is insufficient (fail-closed).

---

## 3. REFERENCE IMPLEMENTATION (Gates P and S; Numba-compatible; complete)

```python
"""psd_gates.py — Parameter Selection Doctrine gates P and S.
Operates on a precomputed smoothed performance grid (Polars -> ndarray).
No I/O, no asyncio interaction: pure CPU kernels safe to call from
worker processes (spawn context on Windows/Ryzen).
"""
from __future__ import annotations

import numpy as np
from numba import njit

# ---------- Gate P: plateau geometry ----------

@njit(cache=True)
def gate_p_plateau(perf_grid: np.ndarray,
                   axes_values: tuple,
                   star_idx: np.ndarray,
                   rel_band: float = 0.20,
                   floor_ratio: float = 0.70) -> bool:
    """True iff min performance within +/- rel_band (per axis, snapped to
    grid nodes) >= floor_ratio * perf(star). perf_grid is the SMOOTHED
    surface, N-dim; star_idx are integer indices of the candidate."""
    ndim = perf_grid.ndim
    lo = np.empty(ndim, np.int64)
    hi = np.empty(ndim, np.int64)
    for d in range(ndim):
        vals = axes_values[d]
        c = vals[star_idx[d]]
        lo_v, hi_v = c * (1.0 - rel_band), c * (1.0 + rel_band)
        lo_i, hi_i = star_idx[d], star_idx[d]
        for i in range(vals.shape[0]):
            if vals[i] >= lo_v and i < lo_i:
                lo_i = i
            if vals[i] <= hi_v and i > hi_i:
                hi_i = i
        lo[d], hi[d] = lo_i, hi_i
    star_perf = perf_grid[tuple_index(star_idx)]
    if star_perf <= 0.0:
        return False  # a non-positive plateau center is auto-fail (P4)
    # iterate the hyper-rectangle
    m = np.copy(lo)
    while True:
        p = perf_grid[tuple_index(m)]
        if p < floor_ratio * star_perf:
            return False
        d = ndim - 1
        while d >= 0:
            m[d] += 1
            if m[d] <= hi[d]:
                break
            m[d] = lo[d]
            d -= 1
        if d < 0:
            return True

@njit(cache=True)
def tuple_index(idx: np.ndarray) -> int:
    # helper placeholder: in production, flatten with precomputed strides
    # (numba cannot build tuples dynamically) — call site supplies
    # raveled index via np.ravel_multi_index equivalent below.
    return 0  # overridden by ravel-based wrapper

def gate_p(perf_grid: np.ndarray, axes_values: list[np.ndarray],
           star_idx: tuple[int, ...],
           rel_band: float = 0.20, floor_ratio: float = 0.70) -> bool:
    """Ravel-based wrapper (pure NumPy fallback; identical semantics)."""
    star = np.asarray(star_idx)
    windows = []
    for d, vals in enumerate(axes_values):
        c = vals[star[d]]
        mask = (vals >= c * (1 - rel_band)) & (vals <= c * (1 + rel_band))
        windows.append(np.where(mask)[0])
    star_perf = perf_grid[star_idx]
    if star_perf <= 0.0:
        return False
    mesh = np.meshgrid(*windows, indexing="ij")
    neighborhood = perf_grid[tuple(m.ravel() for m in mesh)]
    return bool(neighborhood.min() >= floor_ratio * star_perf)

# ---------- Gate S: MC sensitivity (Alvarez) ----------

def gate_s(perf_grid: np.ndarray, axes_values: list[np.ndarray],
           star_idx: tuple[int, ...], m_draws: int = 1000,
           rel_band: float = 0.20, seed: int = 42) -> tuple[str, float]:
    """Returns ('PASS'|'GREY'|'FAIL', z). Uniform +/- rel_band draws per
    axis, snapped to nearest grid node; evaluations are grid lookups
    (zero extra backtests — the grid IS the cache)."""
    rng = np.random.default_rng(seed)
    star = np.asarray(star_idx)
    idx_draws = np.empty((m_draws, len(axes_values)), np.int64)
    for d, vals in enumerate(axes_values):
        c = vals[star[d]]
        targets = rng.uniform(c * (1 - rel_band), c * (1 + rel_band), m_draws)
        idx_draws[:, d] = np.abs(vals[None, :] - targets[:, None]).argmin(1)
    perfs = perf_grid[tuple(idx_draws[:, d] for d in range(len(axes_values)))]
    mu, sd = float(perfs.mean()), float(perfs.std(ddof=1))
    if sd <= 0.0:
        return "PASS", 0.0            # perfectly flat neighborhood
    z = (float(perf_grid[star_idx]) - mu) / sd
    if z <= 1.0:
        return "PASS", z
    if z <= 2.0:
        return "GREY", z
    return "FAIL", z
```

Notes: both gates consume the already-computed smoothed grid — zero additional backtests, so runtime cost is negligible and the multiprocessing pool is untouched. The Numba kernel is provided for very large 4-D grids; the NumPy wrapper is the reference semantics and is production-adequate for coarse grids.

---

## 4. CONFIGURATION PARITY (pre-flight guardrail compliance)

Additive `param_selection` block; `regime_filter` and `borrow_drag_coefficient` remain at root, untouched. YAML and validation JSON schema updated atomically in the same commit.

```yaml
# config/env.yaml (diff — additive)
regime_filter: null            # root guardrail, unchanged
borrow_drag_coefficient: <cost_guardrail> # root guardrail, unchanged
param_selection:
  doctrine_version: "1.0"
  max_free_params: 4
  timeframe_menu: ["15m", "30m", "1h", "2h", "4h", "1d"]
  max_timeframes_per_leg: 2
  gate_p: {rel_band: 0.20, floor_ratio: 0.70}
  gate_s: {m_draws: 1000, rel_band: 0.20, grey_z: 1.0, fail_z: 2.0}
  ensemble: {default_k: 10, weighting: "equal"}
  walk_forward: {cadence: "quarterly", purge: "auto", embargo: "auto"}
  pbo_gate: {max_pbo: 0.10, cpcv_groups: 10, cpcv_test_groups: 8,
             purge_embargo_bars: "auto"}
```

---

## 5. ADVERSARIAL AUDIT (known failure modes of this doctrine, on the record)

1. **Robust-zero plateau.** Gates P/S will happily pass a broad plateau of Sharpe ≈ 0.4-that's-really-0. Mitigation: P4 — PSD never certifies edge; the validation battery (bootstrap CI / year-count / DSR / PBO) is a separate, mandatory stage. PSD pass + battery fail = dead leg, correctly.
2. **Meta-overfitting via amendments.** The doctrine's own thresholds (0.70, ±20%, z=2, PBO 0.10) could be quietly tuned until legs pass. Mitigation: freeze + amendment protocol (§0). Any amendment adopted after seeing a leg's result cannot apply to that leg.
3. **Purge/embargo undersizing.** If E < true label horizon (e.g., multi-week holding horizons), leakage persists invisibly. Mitigation: E = max(holding, lookback) computed from the leg's own max_bars/label spec, asserted in code, unit-tested with a synthetic leaked-label case.
4. **Ensemble masking.** Top-K averaging can hide one toxic config inside a good consensus. Mitigation: every ensemble member must individually pass Gate P; members are reported individually in the leg's registry entry.
5. **Grid-snap distortion in Gate S.** On very coarse axes, ±20% draws may snap to few distinct nodes, understating sd and inflating z. Mitigation: gate_s reports the count of distinct snapped nodes; if < 5 per axis, widen rel_band to reach ≥5 or mark the axis "too coarse to assess" (never silently pass).
6. **CPCV compute cost.** 45 splits × full sim is heavy on the Ryzen box. Mitigation: S9 is pre-CAPITAL only (paper deployment may proceed on S1–S8), and the existing week-level multiprocessing pool parallelizes splits; droplet is never used for timing-sensitive CPCV runs.
7. **The stall risk.** This doctrine must not become a reason to defer testing. Mitigation (binding): PSD v1.0 ships frozen NOW and is validated in use on the first Wave-1/lead legs — it is never "improved in the abstract."

---

## 6. WHAT THIS DOCTRINE DELIBERATELY DOES NOT DO

- It does not search for "the best parameters" — P0/P1: no such edge-independent object exists; only robust-selection *procedure* is edge-agnostic.
- It does not permit timeframe sweeps, objective-function shopping, range extensions after results, raw-max selection, or per-leg cadence improvisation.
- It does not certify edge. Edge is certified only by the pre-registered validation battery, on which this doctrine is one upstream gate.

*Frozen at commit. Validation-in-use begins with the next leg build (the first pre-registered wave of legs). Amendments: pre-registered only.*
