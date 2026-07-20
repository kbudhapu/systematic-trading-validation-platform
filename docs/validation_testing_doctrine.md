# VALIDATION & TESTING DOCTRINE (VTD v1.0)
### mbappe project · frozen methodology · companion to PSD v1.0

**Status:** PRE-REGISTERED AND FROZEN at commit. Same amendment protocol as PSD: amendments registered in writing, with rationale, BEFORE any run whose outcome they could affect. Tuning this doctrine against results is prohibited by the doctrine itself.

**Scope:** every experiment, leg, and portfolio decision. VTD consolidates the existing validation battery, adds three literature-mandated components (haircut Sharpe reporting, early-stage MCPT, SPA/stepwise pre-capital gate), defines the full test pyramid by lifecycle stage, and specifies the ONLY legal feedback channels between testing and parameter selection.

---

## 0. PRINCIPLES

**V0 — Confirmatory and exploratory work are different activities and never share data.** Exploration (attribution, threshold sweeps, diagnostics) is unrestricted on training/diagnostic data. Any finding it produces is a HYPOTHESIS, not a result, until it clears a pre-registered confirmatory test on data it has never touched (fresh period, fresh universe, or the locked holdout). This is the two-track model; the registry is the wall between tracks.

**V1 — Every test needs a null.** A performance number without a chance-baseline is not evidence (Masters). The battery's job is to supply the null at every stage: permutation nulls (signal level), bootstrap CIs (return level), DSR/haircut (selection level), PBO (process level), SPA (universe level).

**V2 — The bar scales with the search.** Trial counts are first-class data (PSD S8 ledger). Reported significance is always shown BOTH raw and multiplicity-adjusted. A leg's claim is judged on the adjusted number.

**V3 — Feedback is a one-way ratchet.** Test outputs may generate new pre-registered experiments, PSD-compliance actions, forward-only L0 recalibrations, and budget alerts. Test outputs may NEVER modify the tested leg's parameters, criteria, ranges, regimes, or objectives for re-testing on the same data. (§4 enumerates legal/illegal channels.)

**V4 — Engine correctness precedes statistical validity.** A statistically perfect battery on a leaking backtest engine certifies garbage. The engine-correctness layer (synthetic ground truth, causality tests) is a standing prerequisite, not a one-time event.

---

## 1. THE TEST PYRAMID (by lifecycle stage — which tests, when, pass bars)

### Stage 0 — Engine correctness (standing; blocks everything)
Synthetic-ground-truth unit tests for every pipeline component: causality/no-lookahead (mutate future bar → outputs byte-identical: the resampler/cold-start pattern), purge/embargo leak demonstration (planted leak collapses to chance: hit-rate collapses toward chance), volume/PnL conservation, restart idempotency. NEW components ship with their ground-truth tests or do not merge. Suite must be green for any experiment to be citable.

### Stage 1 — Signal triage (NEW: early-stage MCPT, cheap kill-gate)
Before a signal family enters the full pipeline: Masters-style Monte Carlo Permutation Test on TRAINING data only.
- Method: permute log bar-to-bar changes (within-session blocks where session structure matters), rebuild synthetic price paths (statistical properties preserved, temporal patterns destroyed), re-run the FULL strategy logic on ≥1,000 permutations, p-value = fraction of permutations matching/exceeding real performance.
- Distinct from trade-list Monte Carlo: MCPT re-tests the logic itself, not rearrangements of already-obtained trades. It subsumes/extends the existing shuffled-data null-control (multi-draw, 5-leg precedent) into a standing, earliest-stage gate.
- Pass bar: p ≤ 0.05 on training data merely PERMITS pipeline entry (it is a necessary screen, not evidence of edge — in-sample MCPT pass is charged as exploration).
- Purpose: kill dead signal families for the cost of one overnight run instead of a full decade pipeline.

### Stage 2 — Leg validation (the existing battery, consolidated + haircut reporting)
On the pre-registered walk-forward OOS returns (purged/embargoed, PSD S6):
1. Stationary block bootstrap 95% Sharpe CI excludes zero (existing; Politis-Romano/Ledoit-Wolf).
2. Year-count criteria (existing; e.g. ≥8/10 non-negative, per-experiment registration).
3. DSR with cumulative ledger trial count (existing + PSD S8).
4. **NEW — Haircut Sharpe reporting (Harvey-Liu):** report the multiplicity-adjusted Sharpe under Bonferroni, Holm, AND BHY, using ledger N. The haircut is nonlinear — marginal Sharpes are heavily penalized, strong ones moderately — so the three-way report replaces any flat-discount rule of thumb, which is explicitly rejected. Headline claims use the adjusted figure; a leg whose adjusted Sharpe is ~0 is treated as marginal regardless of raw CI.
5. **NEW — Discovery bar:** a leg claimed as a NEW edge (not a replication of a pre-registered external result) must clear the t ≈ 3.0-equivalent hurdle on pooled OOS returns (Harvey-Liu-Zhu; 316+ mined factors make t=2.0 meaningless). Replications of published results (e.g. ORB) may register a lower bar a priori, justified by the external pre-registration their source provides.
6. Cost-stress battery (formalized): re-run the OOS pool at 2× and 4× modeled slippage + realistic fee tiers; report the Sharpe at each. A leg that dies at 2× is flagged capacity-fragile (small-cap legs judged against captured-spread costs, never 5bps).
7. Holdout (locked final 26 weeks) untouched until final pre-paper confirmation; one shot, registered.

### Stage 3 — Diagnostics (attribution — never pass/fail)
Regime slices (causal detector), per-year/per-instrument attribution, MFE/MAE decomposition, parameter-surface drift between refits. Outputs flow ONLY through §4 channels. Regime/period slices are constitutionally barred from becoming pass/fail criteria post hoc (a recorded precedent).

### Stage 4 — Portfolio / pre-capital gates
1. CPCV → PBO ≤ 0.10 per leg (built; PSD S9).
2. **NEW — SPA/Reality Check, specified:** Hansen SPA over the FULL candidate universe ever evaluated (all legs, dead and alive — the universe is the ledger, not the survivors) against the registered benchmark (risk-free and buy-and-hold variants), stationary-bootstrap nulls; then stepwise Romano-Wolf/stepwise-SPA to identify WHICH legs are genuinely superior, not merely whether the best is luck. Pass = the deployed set survives stepwise identification at 5% FWER. Runs before ANY real capital; not required for paper.
3. Correlation admission gate on realized OOS returns |ρ| < 0.3–0.4 (existing, PortfolioBrain).
4. Blend-level bootstrap CI on the combined book (the capital-tier CI is passed at portfolio level by construction — 3 legs Sharpe~0.8, ρ~0.2 → ~1.17).

### Stage 5 — Live reconciliation (the ultimate OOS)
Paper/live vs backtest divergence tracking as a STANDING test: realized fills vs modeled slippage, realized signal timing vs backtest timing, realized regime detector lag. Divergences feed §4 channel (c) — forward-only cost/model recalibration — and never retroactive re-scoring.

---

## 2. REFERENCE FORMULAS (reporting layer)

Haircut Sharpe (Harvey-Liu): p_single = t-dist p-value of observed SR over T periods; p_adj = Bonferroni: min(N·p,1) / Holm / BHY over ledger N; SR_haircut = SR implied by p_adj at the same T. Report all three + N. (quantstrat reference implementations exist: haircut.Sharpe, profit.hurdle.)

MCPT p-value: p = (1 + #{perm ≥ real}) / (1 + n_perm), n_perm ≥ 1,000, block-permutation length matched to the signal's dependence horizon; permutation respects session boundaries at intraday timeframes.

Both are computed on the existing multiprocessing pool (spawn context); MCPT permutation runs are embarrassingly parallel week-level jobs.

---

## 3. CONFIG PARITY (additive; guardrails untouched at their current locations)

```yaml
validation:
  doctrine_version: "1.0"
  mcpt: {n_perm: 1000, block: "auto", session_aware: true, entry_p: 0.05}
  haircut: {methods: ["bonferroni", "holm", "bhy"], report_all: true}
  discovery_bar: {new_edge_t: 3.0, replication_t: "register_a_priori"}
  cost_stress: {slippage_multipliers: [1.0, 2.0, 4.0]}
  spa: {benchmarks: ["rf", "buy_hold"], fwer: 0.05, stepwise: true,
        universe: "full_ledger"}
  holdout: {weeks: 26, shots: 1}
```
YAML + validation JSON schema updated atomically; per-strategy `params:` guardrails byte-identical (Task-1 precedent: guardrails live in per-strategy configs, not env root).

---

## 4. THE FEEDBACK ARCHITECTURE (the one-way ratchet, formalized)

Every Stage 1–5 run emits a structured DiagnosticReport (SQLite via existing AsyncDBWriter; append-only, same pattern as the trial ledger):
`(exp_id, leg_id, stage, verdict, diagnostics_json, generated_hypotheses[], psd_flags[], cost_observations[], budget_state)`

### Legal channels (the ONLY four)
- **(a) Hypothesis quarantine.** Attribution findings (regime concentration, instrument asymmetry, threshold sensitivity) are written as QUARANTINED hypothesis-registry entries: testable only via a NEW pre-registered experiment on data the finding has not touched. Precedent: a regime-attribution finding, quarantined into a new pre-registered experiment. The suite "suggests" — the registry enforces that suggestions are earned before they are believed.
- **(b) PSD-compliance flags.** OOS Gate-S recheck (z>2 on OOS aggregate), plateau drift between quarterly refits, multimodality onset → trigger the PRE-WRITTEN doctrine responses (e.g. "switch leg to top-K ensemble," "leg fails parameterization, reject"). These are rule executions, not tuning: the rule predates the result.
- **(c) Forward-only L0 recalibration.** Realized-vs-modeled cost divergence (live reconciliation, captured spreads) updates the L0 cost model for ALL future tests equally, effective from a registered date. Retroactive re-scoring of past experiments under the new model is reported as a labeled sensitivity note, never as a verdict change.
- **(d) Trial-budget alerts.** The ledger tracks confirmatory tests per dataset; when a dataset's budget is spent (registered cap per data window), the suite's "push" is: new data required (extend history, second universe, or forward collection). This is the alpha-spending discipline that keeps the decade cache from being quietly mined to death.

### Illegal channels (enumerated; constitute doctrine violation)
Post-hoc criterion changes; parameter/range/timeframe adjustment of a tested leg for re-test on the same data; regime or period carve-outs promoted to pass/fail; objective-function substitution after results; holdout re-use; "one more run" grid extensions; promoting a Stage-3 diagnostic to a Stage-2 verdict.

---

## 5. ADVERSARIAL AUDIT (failure modes of this doctrine)

1. **MCPT false comfort.** An IS permutation pass is weak evidence (IS + selection); its only licensed use is as a kill-gate. Mitigation: entry_p passes are logged as exploration, never cited in leg claims.
2. **SPA universe gaming.** Running SPA over survivors-only flatters the book. Mitigation: universe = the full ledger including dead legs, asserted in code against ledger row count.
3. **Quarantine laundering.** Repeatedly generating quarantined hypotheses from the same diagnostic slice until one passes is multiplicity by another name. Mitigation: quarantined hypotheses carry their generation count; DSR/haircut N for a quarantine-born experiment includes its sibling hypotheses from the same report.
4. **Channel (c) as backdoor.** "Recalibrating costs" right when a favorite leg needs it is tuning in disguise. Mitigation: recalibrations require a registered observation window (≥20 trading days of live divergence data) and apply to all legs simultaneously.
5. **Budget circumvention via resampling.** Rerunning on 30m bars of the same period is not new data. Mitigation: the budget key is the (instrument, period) window, timeframe-agnostic.
6. **Pyramid as bureaucracy.** Five stages can ossify into ritual. Mitigation: Stages 0–2 are automation (already largely built); Stage 3 is free-form; only Stages 4–5 add genuinely new build work (SPA module, reconciliation tracker) — both already on the pre-capital critical path, now specified.

---

## 6. WHAT REMAINS TO BUILD (delta only — most of VTD already exists)

| Component | Status |
|---|---|
| Bootstrap CI, year-count, DSR, null-control, holdout guard | BUILT (Queues 4–7) |
| Purged WF, CPCV/PBO, trial ledger, PSD gates | BUILT (PSD queue) |
| Early-stage MCPT module (session-aware block permutation) | NEW — small (parallels null-control code) |
| Haircut Sharpe reporting (Bonferroni/Holm/BHY) | NEW — small (formula layer over ledger) |
| Cost-stress battery (2×/4× slippage re-runs) | NEW — trivial (config sweep of existing sim) |
| SPA + stepwise Romano-Wolf module | NEW — medium (pre-capital gate) |
| DiagnosticReport emitter + quarantine registry table | NEW — small (mirrors trial-ledger pattern) |
| Live reconciliation tracker | NEW — medium (pre-capital; pairs with paper soak) |

*Frozen at commit. Amendments: pre-registered only. The suite's job is unchanged: make it impossible to believe a false edge, and cheap to find a true one.*

---

## 7. AMENDMENT (2026-07-11) — KNIFE-EDGE STOP (all scalar-vs-threshold gates)

**Registered in the clean window** (per F1, amendments defer once a relevant result is in flight; at
registration there are zero validated legs, zero resolved Phase-0 outcomes, and Stage-2 has not run —
this window closes on the next verdict). Operator-ruled, tooling-chat-corrected. Live immediately.

**Rule.** A verdict whose deciding statistic sits **within the DERIVED uncertainty band of its own
gate** is a **HARD STOP**: it is neither PASS nor REJECT — it is **KNIFE-EDGE**, reported (not honored)
to the operator with the band, the statistic, and the distance to the gate, and the operator rules on it
explicitly. Silent honoring of a within-band pass is the failure mode this closes.

**Bands are DERIVED from the estimator, not chosen:**

| Gate | Band |
|---|---|
| MCPT p vs α | `1 / n_permutations` |
| SPA / Romano-Wolf p vs α | `1 / n_boot` |
| DSR vs 0.95 | `max(measured propagated band, 5e-3)` — measured = W-A leg-2's empirical Gumbel/closed-form error |
| PBO vs 0.10 | `max(measured CSCV band, 0.01)` |

The DSR floor is **5e-3** (the strategy chat's original 5e-3 was ~40% narrower than the estimator's own
approximation error; the tooling chat's derivation stands as the floor); the PBO floor is **0.01**.

**Home = the VERDICT PATH.** Implemented in `src/research/psd/psd_gates.py`
(`classify_scalar_gate` / `knife_edge_verdict` / `mcpt_band` / `boot_band` / `dsr_band` / `pbo_band`),
**not** in the W-A cross-validation test — whose synthetic fixtures scatter DSR across [0,1] and would
fire the knife-edge constantly, protecting nothing.

**Dependency (floor-until-measured).** The DSR/PBO "measured" band components come from **W-A leg 2**
(tooling queue). Until W-A runs, the **floor values (5e-3 / 0.01) apply** and this amendment says so
explicitly; it is live now and tightened later by measurement — never loosened.

**Worked precedent (recorded, ruled BEFORE the Sharadar Stage-2 run).** A candidate leg that passes triage at **p = 0.0250 vs α = 0.025** with **n = 1000** permutations (band = **0.001**) is, because |p − α| ≤ band, **KNIFE-EDGE, not PASS** under this amendment; its recorded disposition is **unchanged**; the precedent is recorded because the rule was ruled before
the Stage-2 run, not after.

---

## 8. AMENDMENT (2026-07-13, A3) — CALENDAR/EVENT CONSTANTS MUST BE DERIVED, NEVER TRANSCRIBED

**Standing requirement (binding, research AND production).** *Every calendar/event constant (FOMC,
holidays, early closes, ex-div, expiries) — research AND production — must be DERIVED from its official
source with a freshness/consistency check validated at startup or in CI. Transcribed-from-knowledge
constants are triage-grade research artifacts only and may NEVER gate a live override.*

**Evidence.** Production `FOMC_DATES_UTC` carried 2024's Q4 meetings **year-bumped** (mechanism:
prior-year transcription), gating a live risk-posture override. The production FIX is ops-owned; this
requirement prevents recurrence everywhere.

**Reference implementation (research side).** The W-C FOMC/FRED utility already derives + cross-checks
FOMC dates against the official source (see `docs/research/FOMC_CROSSCHECK.md` /
`docs/research/FORWARD_CALENDAR_DIFF.md`); it is the reference for the freshness/consistency check this
requirement mandates. Registered as `STANDING -- calendar/event constants must be derived` in
`EXPERIMENT_REGISTRY.md`.
