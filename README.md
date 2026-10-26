# Systematic Trading Validation Platform

A research and paper-trading harness for deciding whether a trading strategy is real before any capital is at risk. Its premise is that individual signals decay, but a validation process that reliably rejects noise keeps its value. Most of the engineering here therefore goes into rejecting ideas honestly: pre-registration, leakage control, multiple-testing correction, and realistic costs.

This is a sanitized excerpt of a private repository, published for review. It is not a deployable trading system and does not run standalone: candidate strategies, live strategy parameters, the experiment registry, the database layer and infrastructure are not published. No live or candidate strategy's tuned parameters appear here.

## What we found

Hypotheses are pre-registered: pass/fail criteria are committed to git before results are computed, and the case studies below cite the commit pairs. Most were rejected, which is the expected outcome for honest research on public data.

| Investigation | Result |
|---|---|
| **Triage** | 29 pre-registered trials of 12 hypotheses, screened with permutation tests and Bonferroni-corrected thresholds, with 2× and 4× cost stress where registered. **27 of 29 rejected**; one passed and one passed as fragile. Only 4 of the 21 trials with a computed net Sharpe were positive (median −0.26). Costs alone killed some: one SPY trial went from a net Sharpe of 0.55 at 1× costs to 0.33 at 2× and −0.09 at 4×. |
| **Repurchase drift** | +1.12% per event in 2000-2016 (n = 5,993, p = 0.002, DSR > 0.99) decayed to +0.39% in 2017-2025 (n = 3,921, p = 0.36); by decade, +1.76% in the 2000s and +0.46% in the 2010s. Criteria were committed in `e1ba738` and the verdict landed four and a half hours later in `cbdce00`. Rejected as pre-registered: a real anomaly that appears to have been arbitraged away. |
| **Insider clusters** | 11,308 insider-buying clusters, 6,497 of them tradeable after liquidity and data filters, across 2,654 tickers (2017-2025), drawn from 2.0M SEC insider-ownership filings (1.8M of them Form 4). Mean net SPY-adjusted return of −1.97% per event, worse under 2× and 4× costs. The timing signal was real (clusters beat the same names at random times), but the level was negative. Rejected. |
| **Momentum** | Cleared triage (p = 0.001), then failed 5 of 6 pre-registered Stage-2 gates (DSR 0.31). Rejected. |
| **DDR-F1 equity swing** | Looked like roughly 6-7%/yr on the development sandbox (record only). On a survivorship- and crash-complete rebuild (29.1M daily rows, 16,099 tickers, 71.2% delisted, 2004-2022) it earned about +0.3%/yr. A reproduction gate on the rebuild caught two silent defects in the development data: a missing year (2018) and dropped sub-$1 rows. Leave-one-crash-out validation showed a −68.9% drawdown in a GFC-scale crash, about 5.3× the 13% drawdown budget. Deployed to a paper account and halted; killed before any real capital. |
| **ML configuration search** | A search over 48 configurations failed to beat the expected maximum under noise (best z = 0.92 against an expected maximum of 2.78). |
| **Leakage test** | On a 10-year QQQ walk-forward (500 out-of-sample weeks, 39 refits), the runner first reproduced every reported registered statistic to the reported precision. A 199-bar purge and embargo then moved Sharpe by −0.19 against a pre-registered 0.66 materiality bound, so the leak was immaterial. The same test on GLD and USO was also immaterial. |
| **Data fidelity** | Over 1,007 shared days of regular-hours 15-minute bars for SPY and QQQ, IEX-only data was missing under 0.5% of bars but 98.0-98.5% of consolidated volume relative to the SIP tape. The pre-market scanner moved to SIP data in July 2026. |
| **Live data collection** | A pre-market gap scanner logged 2,052 candidates across 834 US equities over 77 sessions, alongside a record of 152,915 historical trading halts. Its run log, kept since 2026-08-10, shows 52 runs with 0 errors. |
| **Paper run** | 148,520 engine cycles over 114 days, with 14 heartbeat gaps over 5 minutes in 435,770 beats and 295 automatic data-quality flattens. The engine placed no orders. |
| **FX** | Price-based FX signals were investigated in September 2026 and dropped before capital. The record gives the reasoning; its run outputs are not in this repository, so no statistics are quoted here. |

Write-ups:
- `docs/records/DDR_FULL_INVESTIGATION_PORTFOLIO.md` (concise version: `DDR_WRAPUP_RECORD.md`)
- `docs/records/FX_FULL_INVESTIGATION_PORTFOLIO.md`
- `docs/CASE_STUDY_repurchase_drift_decay.md`: a significant anomaly that failed its pre-registered recent-period test
- `docs/CASE_STUDY_insider_cluster_null.md`: an event study on 2.0M insider-ownership filings with a negative result
- `docs/CASE_STUDY_leakage_test.md`: a reproduction gate, then a pre-registered purge and embargo sensitivity test
- `docs/CASE_STUDY_data_fidelity_iex_sip.md`: what a single-venue feed hides
- `docs/CASE_STUDY_momentum_stage2_rejection.md`: a momentum rule that passed triage and then failed five of six Stage-2 gates
- `docs/CASE_STUDY_lead_lag_audit_correction.md`: a rejection whose reasoning was audited two days later, found partly wrong, and corrected in place with the verdict unchanged
- `docs/CASE_STUDY_mean_reversion_rejection.md`: a mean-reversion hypothesis traced from pre-registration to rejection

## Method

Four written doctrines govern the experiments. All four were committed on 2026-07-05, and later changes are recorded as labelled amendments. They govern every experiment registered after that date; the earliest experiments, registered on 2026-07-04, predate them. Freeze commits: PSD `cbe5975`, VTD `9bb9510`, LLD `7cd4942`, PAD `7cd4942`.

- **Parameter Selection Doctrine** (`docs/parameter_selection_doctrine.md`): few free parameters, a fixed timeframe menu, and an a priori selection rule. A small, honest trial count is what makes multiple-testing correction meaningful. The trial ledger records every grid searched: about 249M parameter configurations are counted toward deflation, nearly all from one grid sweep.
- **Validation & Testing Doctrine** (`docs/validation_testing_doctrine.md`): pre-registration, purged and embargoed walk-forward, full-moment Deflated Sharpe, Bonferroni correction, and a spread, slippage and square-root-impact cost model with borrow charges.
- **Leg Lifecycle Doctrine** (`docs/leg_lifecycle_doctrine.md`): a seven-state lifecycle (`CANDIDATE → VALIDATED → PAPER → ACTIVE → WATCH → SAFE_MODE → RETIRED`) with demotion monitors.
- **Portfolio Assembly Doctrine** (`docs/portfolio_assembly_doctrine.md`): risk-budgeted allocation across decorrelated clusters with per-cluster and per-leg caps.

### What is run vs. built

"Run" means exercised on real data. "Built" means implemented and unit-tested but not yet used in a recorded verdict.

| Component | Status |
|---|---|
| Pre-registration (provable by git commit order) | Run: criteria commits precede result commits (pairs cited in the case studies) |
| Purged and embargoed walk-forward | Run (the QQQ leakage test) |
| Full-moment Deflated Sharpe Ratio (skew and kurtosis estimated) | Run |
| Bonferroni correction | Run |
| Cost model; spread and slippage stressed at 2× and 4×, borrow held at 1× | Run, per experiment |
| Data-quality circuit breakers | Run: 295 automatic flattens over the 114-day paper run |
| Config parity auditor | Run at every startup |
| Research dashboard (Next.js + Supabase) | Run: 49 experiment artifacts, DSR per trial, permutation-test histograms, equity curves |
| CPCV split generator | Built |
| PBO estimator (CSCV) | Built, and calibrated against noise; not yet applied to a registered strategy |
| Harvey-Liu haircut with BHY adjustment | Built |
| Backtest/live signal parity | Tested on synthetic tapes |
| Leg lifecycle state machine; rolling-Sharpe, CUSUM, cost-divergence and drawdown monitors | Built; not yet wired to the live engine |
| Portfolio assembly | Built and dormant; with a single leg the allocator runs a static split |

## Architecture

```
src/
  engine/         signal evaluation, capacity governor, kill-switch, degradation latches (orchestrator omitted)
  backtest/       vectorized backtester, cost and borrow model, fill model
  execution/      order lifecycle, idempotent submission, reconciliation
  broker/         broker abstraction and simulated broker
  router/         order router (risk manager omitted)
  control/        governance, control-plane guards, safety-state recovery
  portfolio/      risk-budgeted assembly (dormant)
  lifecycle/      leg state machine and monitors (not yet wired)
  research/psd/   Parameter Selection Doctrine, purged and embargoed walk-forward, CPCV/PBO/DSR
  research/vtd/   Validation & Testing Doctrine: cost stress, permutation tests, haircuts
  persistence/    append-only stores, hash-chained journals (database layer omitted)
  ingestor/       market-data ingestion, feed-fidelity guards
  alerts/         multi-channel dead-man alerting
  strategies/     strategy chassis and a generic mean-reversion example
dashboard/        Next.js observability dashboard (Supabase, RLS-locked)
supabase/         schema and least-privilege Row-Level-Security migrations
docs/             doctrines, architecture, investigation records, case studies
tests/            excerpt of the test suite (see below)
```

The private repository has about 72K lines of Python in `src/` and 2,026 tests across 287 files; in an isolated run on 2026-10-23, 1,970 of 1,982 passed, and the 12 failures came from the isolation itself. This excerpt contains 165 Python source files (162 under `src/`) and 157 test files.

## Safety and reproducibility

- **Fails closed.** A kill-switch outranks every signal. Degradation latches halt new entries when data quality or feed fidelity drops; over the paper run they triggered 295 automatic flattens, and 186 incomplete reconciliation runs latched the engine into a degraded state rather than continuing.
- **Reproducible.** Research runs are seeded. In the ML pipeline dry run, two training runs were bit-identical and replay of 6,284 events had 0 mismatches. The leakage test began by reproducing a registered result to its reported precision, and verdicts can be recomputed from artifacts stored in the private repository.
- **Least privilege.** The dashboard uses only RLS-enforced public keys client-side and rejects a service-role key in the browser; privileged operations are server-only.
- **Scrubbed publish.** This tree is built from the private repository by an allowlist, scrub and quarantine step, followed by a secrets scan. Audits are kept private; the RLS migrations, policies and the tests that guard them are published here.

## Running the tests

The full suite needs modules that are not published. CI runs the self-contained subset: the 42 test files listed in `.github/public-ci-tests.txt` (293 tests), chosen mechanically as the files that import and pass in this tree without the omitted modules.

```
# Python 3.13
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m pytest -q --tb=short -p no:cacheprovider $(grep -v '^#' .github/public-ci-tests.txt)
```

## Status

Portfolio and demonstration repository. The components needed to trade a live account are deliberately absent.

## License

MIT. See `LICENSE`.
