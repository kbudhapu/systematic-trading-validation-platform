# DSR Band vs Bias — Decomposition + Regression Fix (W-A correction)

**Correction, 2026-07-11.** The first W-A artifact conflated two error sources into one "band" and,
worse, applied it symmetrically — which **loosened the gate**. This decomposes them, fixes the gate,
and re-emits the artifact. `psd_gates.knife_edge_verdict` is live on this artifact, so this is urgent.

## C1 — the decomposition (the operator's prediction, tested)

**SOURCE A — Gumbel/Euler-Mascheroni APPROXIMATION ERROR (a TRUE band).** Measured with **σ_SR held
IDENTICAL on both arms**; only E[max of N iid SR] varies (closed form vs seeded Monte-Carlo). At the
gate z is pinned at Φ⁻¹(0.95)=1.645, so **σ_SR cancels**: |dDSR_A| = φ(1.645)·|K_closed − K_MC| =
0.1031·|ε|·(coefficient). **Prediction confirmed — n_trials-only, and even smaller than the ~7e-3
guess:**

| n_trials | 2 | 16 | 50 | 200 | 1500 | 5000 |
|---|---|---|---|---|---|---|
| \|dDSR_A\| | 0.00525 | 0.00297 | 0.00223 | 0.00176 | 0.00132 | 0.00112 |

- **n_obs-independent** (the formula contains no n_obs term; σ_SR cancels). The 2-D table I built
  earlier was wrong; the band is **1-D on n_trials**.
- Source A ≤ ~5e-3 at every N (only N=2 nudges above the floor). So **the operational `dsr_band` is the
  registered 5e-3 floor**, with a negligible 0.00525 bump at N=2.

**SOURCE B — Gaussian σ_SR MISSPECIFICATION (a BIAS, not a band).** DSR_full − DSR_gauss at the gate,
under planted γ3=−1, γ4=10: **−0.014 to −0.200, ONE-DIRECTIONAL toward FALSE PASS** (DSR_full < DSR_gauss;
the Gaussian estimator over-certifies). **A tolerance band cannot fix a systematic bias.** This is now a
**separate, signed `dsr_bias` field that `psd_gates` does NOT consume** — a PSD S9 estimator-correction
question, escalated.

**How much of the old band was which:** essentially all of it was **Source B**. Old emitted band ranged
0.014–0.355; Source A is 0.001–0.005. So ≈99% of the old "band" was the bias, wrongly banded.

**Mechanism check (the operator's challenge).** "The skew term ∝ SR ∝ 1/√n_obs" — is that legitimate,
given `sr_pbar = sr_annual/√(252·26)` is not a function of n_obs? **Yes, at the gate.** The measurement
pins DSR_gauss = 0.95, which fixes the *gate-clearing* per-period Sharpe at `sr_pbar = σ_SR·(z+K) ∝
1/√n_obs`. sr_annual is fixed by the strategy; but the Sharpe that *clears the deflated bar* shrinks with
more data, and the skew perturbation to σ_SR scales with that Sharpe. So the **bias** is genuinely
n_obs-dependent — the fixture did not vary SR by hand; the gate condition did. (This n_obs dependence
lives entirely in Source B / `dsr_bias`; Source A / the band does not have it.)

## C2 — REGRESSION: the merged gate was LOOSENED. Fixed.
The knife-edge zone was applied **symmetrically** around 0.95. With the (wrong) 0.20 band that is
[0.75, 1.15] — so DSR = 0.80, a clean REJECT, became KNIFE_EDGE (operator adjudicates). Converting a
REJECT into a judgment call is **gate-loosening (SFD 4.2)**. **Fixed: `classify_scalar_gate` is now
ONE-SIDED** — KNIFE_EDGE fires only on the PASS side within band ([gate, gate+band] for DSR; [gate−band,
gate] for PBO, the mirror). **A sub-gate verdict is a REJECT and STAYS a REJECT; the band is never
widened downward.** Under a one-directional over-certifying bias a sub-gate verdict's true value is even
worse, so rejecting it is if anything more correct. Regression test added: a sub-gate DSR is REJECT at
every band magnitude 0…0.5.

## C3 — minimum-n_obs admissibility
- **Source A (the band) is ≤ ~5e-3 at every N and every n_obs** → it never meaningfully exceeds the
  floor, so there is **no minimum-n_obs required on Source-A grounds** (it is already below the floor
  everywhere).
- The "DSR uninformative at short samples" concern is **entirely the Source-B bias**: |bias| exceeds the
  5e-3 floor until **n_obs ≈ 60,000** (at N=200: 0.20 @130 · 0.072 @520 · 0.016 @5200 · 0.0077 @20k ·
  0.0043 @60k). Every realistic backtest (n_obs in the hundreds–low-thousands) is **bias-dominated**.
- **RECOMMENDATION (PSD S9, do NOT implement here):** a minimum-n_obs admissibility threshold would have
  to be ~60,000 observations to render the Gaussian-σ_SR DSR bias-negligible — impractically large (that
  is ~9 years of 15-min RTH bars, or ~230 years of daily). So a min-n_obs cut-off is the wrong lever: it
  would exclude essentially all real backtests. **The honest fix is estimator CORRECTION** (adopt the
  full σ_SR), because no realistic sample size makes the current DSR trustworthy at the gate. Until PSD
  S9 rules, treat every gate DSR as an **UPPER BOUND** (see interim posture).

## C4 — the re-emitted artifact (`data/research/knife_edge_bands.json`, schema/2)
- **`dsr_band`**: SOURCE A only, **1-D keyed on n_trials** (n_obs-independence confirmed → collapsed);
  `= max(Source A, 5e-3 floor)`. `raw_source_a` retained for transparency.
- **`dsr_bias`**: SOURCE B, **separate field**, 2-D (n_trials, n_obs), **SIGNED**, `is_band: false`,
  labelled a bias. `psd_gates` does NOT consume it — it exists for the PSD S9 estimator ruling.
- **`pbo_band`**: unchanged key space (n_slices, n_configs).
- All four contracts retained (round-up / above-domain HARD STOP / FLOOR_FALLBACK never zero / monotone
  ratchet); seed-reproducible, content-hash-pinned (hash excludes the volatile git stamp).

## INTERIM POSTURE (stated, until C-series + PSD S9 land)

> **RESOLVED (stamp 2026-07-30) — the σ_SR condition below was met by the PSD S9 amendment
> "FULL-MOMENT σ_SR for the DSR" (adopted 2026-07-13, A1; `docs/parameter_selection_doctrine.md`
> §S9).** The interim posture was gated on *"until PSD S9 rules on σ_SR"*; S9 has now ruled — the
> gate DSR is computed with the full-moment (skew/kurtosis-aware) σ_SR estimator, so the
> "treat every gate DSR as an UPPER BOUND / unannotated ceiling" stance is retired **as the interim
> stance**: the tightening it described is now folded into the ratified estimator rather than
> applied by hand. Kept below for provenance; the historical reading remains correct *for pre-S9
> gate outputs.*

`chained_backtest.py` already comments its DSR as a **"CEILING ESTIMATE."** It is right; `cpcv_pbo.py`
— the copy INSIDE the gate path — carries the same math **unannotated**. Until PSD S9 rules on σ_SR,
**the honest reading of every DSR the gate produces is an UPPER BOUND.** That reading is a **TIGHTENING**
(a DSR that looks like it clears 0.95 may truly be below), and tightening is always legal — so it is the
safe operating assumption in the meantime.
