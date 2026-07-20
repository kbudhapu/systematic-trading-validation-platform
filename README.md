# Systematic Trading Validation Platform

A production-grade harness for **deciding whether a trading strategy is real**, and
for running the survivors safely. The engineering thesis of this project is that the
durable asset in systematic trading is not any individual signal — signals decay —
but the **machine that judges signals**: a pipeline disciplined enough that it
reliably rejects the noise that looks like edge, and governance strict enough that a
rejection actually stops capital from flowing.

This repository is that machine, shown deliberately. It is a portfolio artifact, not
a drop-in trading system.

> **What is intentionally omitted.** The validation framework here is demonstrated on
> a **deliberately-failed strategy** (see below). Live and candidate strategies, their
> tuned parameters, the hypothesis registry, alternative-data selection logic, and all
> infrastructure topology are **not published** — they are the active edge. Their
> absence is the point, not an oversight: a showcase of the *method* should not leak
> the *positions*.

---

## The validation philosophy

Most backtests lie to their authors, and they lie in predictable ways: parameters
tuned after seeing results, information leaking across the train/test boundary,
"significance" that ignores how many things were tried. The platform is built to make
those failure modes structurally hard to commit.

Four written doctrines govern every experiment and every leg. They are frozen at a
git commit and amended only by pre-registration — never after seeing an outcome the
amendment would flatter.

- **PSD — Parameter Selection Doctrine** (`docs/parameter_selection_doctrine.md`)
  Bounds the search: few free parameters, a fixed timeframe menu, an *a priori*
  selection rule. A small, honest trial count is what makes multiple-testing
  corrections meaningful instead of decorative.

- **VTD — Validation & Testing Doctrine** (`docs/validation_testing_doctrine.md`)
  Pre-registration; purged, embargoed, combinatorial walk-forward; cost stress at
  1×/2×/4×; a **Deflated Sharpe Ratio** gate and a **Probability of Backtest
  Overfitting** gate; multiple-testing haircuts (Bonferroni / Holm / BHY).

- **LLD — Leg Lifecycle Doctrine** (`docs/leg_lifecycle_doctrine.md`)
  The state machine a strategy leg lives in — `CANDIDATE → VALIDATED → PAPER → ACTIVE
  → WATCH → SAFE_MODE → RETIRED` — with rolling-Sharpe, cost-divergence, CUSUM, and
  drawdown monitors that can demote or retire a leg automatically.

- **PAD — Portfolio Assembly Doctrine** (`docs/portfolio_assembly_doctrine.md`)
  Risk-budgeted allocation across decorrelated return-driver clusters — a risk blend,
  not a switch — with correlation admission tests and per-cluster / per-leg caps.

The connective principle across all four is **evidence over memory**: decisions are
driven by pre-registered artifacts and reproducible verdicts, not by recollection of
what seemed to work.

## The worked example — a strategy the pipeline kills

[`docs/CASE_STUDY_mean_reversion_rejection.md`](docs/CASE_STUDY_mean_reversion_rejection.md)
carries one mean-reversion hypothesis end-to-end: hypothesis → bounded
parameterization → purged walk-forward → **statistical rejection** via DSR and PBO.
The conclusion is *"this is noise,"* which is exactly why it can be shown in full.
It exercises the entire machine and discloses nothing tradable. It uses clearly
labeled **illustrative** parameters — no tuned values appear anywhere in this repo.

## Featured investigations — two markets, two disciplined disposals

The methodology here is shown on **two full market investigations that both ended in a
"no"** — because knowing when *not* to deploy is most of the job. One strategy was
**killed on honest data**; one market was **mapped and stepped away from**. Together they
demonstrate both halves of the discipline: catching a backtest that is fooling you, and
deciding — with the reasoning made explicit — not to trade a market whose edge structure
you can map precisely.

### DDR (equities) — a strategy killed by honest data, before capital

**DDR-F1** (Daily Displacement Reversal), a long-only oversold-bounce equity swing,
**backtested at ~6.5%/yr** on the development sandbox and was **killed at ~+0.3%/yr on
honest data** — the gap is the entire point:

- **Registered** (*2026-08-10*) as a falsifiable pre-registration — placebo control arm,
  pinned constants, temporal-forward wall.
- **Improved** across ~14 avenues (entry, exit, stop, sizing, concurrency, …), each under
  an anti-overfit harness.
- **Stress-tested** by rebuilding the data survivorship- and crash-complete (2004–2022,
  including the 2008 GFC and the falling-knife/penny rows the sandbox had silently dropped).
- **Killed** (*2026-09-10*) when leave-one-crash-out cross-validation showed the edge was a
  sandbox artifact: break-even on honest data and crash-fragile — a GFC-scale crash breaches
  a 13% drawdown budget ~4.6×.

It demonstrates a reproduction gate that caught two silent defects in the development data;
survivorship-completeness (crashes kill stocks — a survivors-only backtest lies);
leave-one-crash-out validation with the mega-crash as the hard bar; and concrete deflation
of an ML combinatorial search.

### FX — a market mapped to a disciplined decision not to trade

**FX** was investigated end-to-end and **stepped away from** (*2026-09-10*) — not because the
search failed, but because the map came back clear. A harnessed search of price-based edge
found signals that were **real but sub-spread** (direction significant at z≈7.7 out-of-sample,
yet smaller than costs); the free structural flow proxy (CFTC COT positioning) tested **null
against a shuffled-context placebo**; and a literature survey confirmed the durable FX edges
are either **risk premia** (carry / vol-selling — beta, "pennies in front of a steamroller")
or **firm-gated** (order flow, CIP basis, speed). The disciplined response to a well-mapped
market whose alpha is structurally out of reach is to **record the map and reallocate.**

It demonstrates harnessed search of an entire edge class to an honest "real-but-sub-spread";
the **free-proxy-before-paid-data** discipline; literature triangulation (separating the
genuinely-useful tools from rigor-theater); the **"who pays"** lens (risk premium vs anomaly
vs structural/informational); and senior resource-allocation judgment.

- DDR — full investigation → [`docs/records/DDR_FULL_INVESTIGATION_PORTFOLIO.md`](docs/records/DDR_FULL_INVESTIGATION_PORTFOLIO.md)
- DDR — concise disposition → [`docs/records/DDR_WRAPUP_RECORD.md`](docs/records/DDR_WRAPUP_RECORD.md)
- FX — full investigation → [`docs/records/FX_FULL_INVESTIGATION_PORTFOLIO.md`](docs/records/FX_FULL_INVESTIGATION_PORTFOLIO.md)

> *A negative result, honestly earned and fully documented, is a stronger signal of
> quantitative rigor than a backtest you cannot tell is overfit — and knowing which markets
> not to trade is most of risk management.*

## Companion case studies — rejections from the real registry

Two further case studies trace real pre-registered experiments to their rejection. Both
disclose the validation record (event/trigger counts, p-values, gate structure) while
omitting every market-facing quantity (parameters, thresholds, effect sizes):

- [`docs/CASE_STUDY_momentum_stage2_rejection.md`](docs/CASE_STUDY_momentum_stage2_rejection.md)
  — a cross-sectional momentum rule that **cleared triage and then failed five of six
  Stage-2 gates**, with the deep-history replication reversing sign. Shows why a triage
  p = 0.001 is an input, not a verdict.
- [`docs/CASE_STUDY_lead_lag_audit_correction.md`](docs/CASE_STUDY_lead_lag_audit_correction.md)
  — an intra-industry lead-lag hypothesis **rejected**, then **audited three days later**:
  the audit found the stated reasoning partly wrong (a bootstrap CI measuring a cost drag,
  not the signal) and corrected the record in place without changing the verdict.

## Architecture

```
src/
  engine/         orchestration, signal evaluation, kill-switch, degradation latches
  backtest/       purged/embargoed walk-forward, cost model, fill model
  execution/      order lifecycle, idempotent submission, reconciliation
  broker/         broker abstraction
  router/         risk manager / capacity governor
  risk, control/  governance, control-plane guards, safety-state recovery
  portfolio/      PAD risk-budgeted assembly
  lifecycle/      LLD leg state machine + monitors
  research/psd/   Parameter Selection Doctrine implementation
  research/vtd/   Validation & Testing Doctrine implementation
  persistence/    append-only stores, hash-chained journals
  ingestor/       market-data ingestion, feed-fidelity guards
  alerts/         multi-channel dead-man alerting
  strategies/     strategy chassis + generic mean-reversion (worked example only)
dashboard/        Next.js control/observability dashboard (Supabase, RLS-locked)
supabase/         schema + least-privilege Row-Level-Security migrations
docs/             the doctrine stack, architecture, and audits
tests/            the machine's own test suite
```

### Risk, governance, and safety

The system is designed to **fail closed**. A kill-switch outranks every other signal;
degradation latches halt new entries when data quality or feed fidelity drops; drawdown
and reconciliation breakers can flatten and stop.

The control-plane and dashboard audit trail — least-privilege review, kill-state coherence,
RLS lockdown — is maintained but **deliberately not published**. Those documents enumerate
the attack surface of a system with a live brokerage connection, including findings that are
still open. A penetration-test report is not a portfolio piece while its findings are open,
and the same reasoning applies here. What is published instead is the machinery those audits
examine: the RLS migrations, the least-privilege policies, the entry-authority chokepoints,
and the tests that guard them, all present in this repository and readable.

## Reproducibility & security posture

- Deterministic, seeded research runs; verdicts are recomputable from stored artifacts.
- The dashboard reads only public (RLS-enforced) keys client-side; all privileged
  operations are server-only. No service-role key is ever exposed to the browser.
- No secrets, live account identifiers, or infrastructure addresses appear in this
  repository; the public tree is built from a private working tree by an auditable
  allowlist-and-scrub step and verified with a secrets scanner.

## Status

Portfolio / demonstration repository. It is not intended to be run as-is against a
live account, and the components required to do so are deliberately absent.
