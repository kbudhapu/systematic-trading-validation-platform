# Worked Example: Killing a Mean-Reversion Hypothesis

> **What this is.** A real, pre-registered experiment from the registry (the QQQ mean-reversion decade test), traced from
> registration to rejection, followed by a later pre-registered test of whether leakage at the
> train/test boundary could have changed the verdict. A rejected hypothesis discloses no edge, so
> it can be shown in full. No tuned or selected parameter values appear here.
>
> **Companion case studies.** [`CASE_STUDY_leakage_test.md`](CASE_STUDY_leakage_test.md) covers the
> leakage test in more depth; [`CASE_STUDY_momentum_stage2_rejection.md`](CASE_STUDY_momentum_stage2_rejection.md)
> and [`CASE_STUDY_lead_lag_audit_correction.md`](CASE_STUDY_lead_lag_audit_correction.md) are two
> further rejections from the same registry.

## 1. Question

Does an intraday z-score mean-reversion process on QQQ have sustained positive expectancy across a
decade of out-of-sample weeks, once its parameters are re-selected periodically the way they would
be in live use?

## 2. Pre-registered criteria

The pass/fail criteria were committed before the run (registry commit `b76d22d`, 2026-07-04,
"pre-execution revision to v2 criteria"):

- **Primary:** the 95% bootstrap confidence interval of the pooled weekly Sharpe ratio must exclude
  zero (stationary block bootstrap, 2,000 resamples, fixed seed).
- **Secondary:** at least 8 of 10 calendar years non-negative, at least 5 strictly positive, and no
  year worse than −10%.

## 3. Data and method

- **Data:** QQQ 15-minute bars from the consolidated (SIP) tape, regular trading hours only. The
  final 26 weeks before 2026-01-01 were held out of the research window.
- **Method:** a chained walk-forward. At each quarterly refit the parameters were re-selected from a
  bounded grid on a trailing training window, using a neighbour-smoothed selection rule fixed in
  advance (`docs/parameter_selection_doctrine.md`), then traded out-of-sample until the next refit.
  Weekly returns were pooled across all out-of-sample weeks.
- **Costs:** 5 bps of slippage per side.

## 4. Result

Over **500 out-of-sample weeks** (2016-05-30 to 2025-12-22) with **39 quarterly refits**:

| Measure | Value |
|---|---|
| Pooled compounded return | +1.78% |
| Pooled Sharpe | 0.065 |
| 95% bootstrap Sharpe CI | [−0.616, 0.697] (includes zero) |
| Years non-negative | 5 of 10 (8 required) |
| Worst year | −3.82% |

**Primary: FAIL. Secondary: FAIL. Verdict: REJECTED** — the process does not have sustained
positive expectancy on QQQ across the decade.

## 5. Could leakage have changed the verdict?

A later audit flagged that each training window ended exactly where its test week began, so
labels near the boundary could leak. The question was pre-registered with a locked decision rule
before any run (commit `9f5afaf`, 2026-07-15):

- **Run A** re-ran the registered experiment unchanged. Every reported statistic matched the
  registered output to the reported precision (Sharpe 0.065326, CI [−0.616046, 0.697036]).
- **Run B** applied a purge and embargo of 199 bars at every train/test boundary, using the same
  `purge_embargo_train` function in `src/research/psd/purged_wf.py`.
- **Decision rule:** the leak is material only if Sharpe moves by more than the registered CI
  half-width (0.6566), Calmar moves by more than its half-width (0.2680), or a gate flips.

Run B gave Sharpe −0.124: **ΔSharpe −0.189** and ΔCalmar −0.053, with both gates still failing.
**Verdict: NOISE** — the leak did not change the rejection (runs committed in `85867f6`,
2026-07-16). The direction was negative, meaning any leakage had mildly flattered the original run.

An errata appended on 2026-07-17 records that run A matched at summary-statistic precision, not
byte-for-byte, and that its raw run files were not kept. A separate hostile verification upheld the
NOISE verdict with that caveat.

## 6. Decision

Rejected. No promotion, no paper allocation, no capital. The hypothesis does not come back without
a new, independently pre-registered reason.

## 7. What it taught

- A Sharpe ratio near zero with a wide confidence interval is a rejection, not a "maybe": the
  primary criterion asks for the interval to exclude zero.
- Testing a suspected leak against a rule locked in advance turns "is this result contaminated?"
  into a mechanical answer instead of a judgment call.
- Reproduction claims need their raw outputs kept. The errata here is why the repo now requires
  diagnostic reports to commit their run outputs.

*The illustrative config in `config/strategies/mean_reversion_example.yaml` uses round demonstration
values; they are not the selected parameters of the leg described here.*
