# Case Study: Testing a Suspected Leak Against a Rule Locked in Advance

## Question

An internal backtest audit found that in the QQQ mean-reversion decade test, every training window ended exactly
where its test week began. Bars near that boundary carry labels that overlap the test period, so
information could leak into parameter selection. Was the registered rejection of that test contaminated
by that leak?

## Pre-registered rule

The test was pre-registered in `9f5afaf` (2026-07-15 12:16 −07:00), with the leg choice, the embargo
length and the decision rule locked before any run.

- **Leg:** the QQQ mean-reversion decade test, a 10-year chained walk-forward (500 out-of-sample weeks, 39 quarterly refits).
  It was chosen because it actually refits at every boundary, and because its verdict sits closest to
  the pass/fail line.
- **Run A:** the registered experiment re-run unchanged. It must reproduce the registered result
  before Run B is trusted.
- **Run B:** the same run with a purge and embargo of **199 bars** applied at every train/test
  boundary, using `purge_embargo_train` in `src/research/psd/purged_wf.py` (imported, not
  reimplemented). The length is the longest lookback plus the longest holding period in the search
  grid. That is deliberately more conservative than the module's default rule, so the test leans
  toward finding an effect if one exists.
- **Material if any of:**
  - |ΔSharpe| > **0.6566**, the half-width of the registered Sharpe confidence interval;
  - |ΔCalmar| > **0.2680**, the half-width of the Calmar interval;
  - a registered gate flips.

## Result

The runs were committed in `85867f6` (2026-07-16).

| | Run A (no embargo) | Run B (199-bar purge/embargo) |
|---|---|---|
| Pooled Sharpe | 0.065326 | −0.123824 |
| Sharpe 95% CI | [−0.616, 0.697] | [−0.797, 0.551] |
| Trades | 136 | 166 |
| Primary / secondary gate | FAIL / FAIL | FAIL / FAIL |

- **ΔSharpe −0.189** against a bound of 0.6566.
- **ΔCalmar −0.053** against a bound of 0.2680.
- No gate flipped.
- **Verdict: NOISE.** The leak did not change the rejection.

The change was negative: removing the boundary-adjacent training bars led to different parameter
choices and a worse out-of-sample result. So any leakage had mildly *flattered* the original run, and
the rejection stands. The same test on a two-instrument leg (GLD and USO) was also NOISE.

## The errata

On 2026-07-17 an append-only errata (`57bc570`) corrected the record:
- Run A matched the registered result at **summary-statistic precision**: every reported value agreed
  to the shown precision.
- It was **not** a byte-for-byte reproduction, and the raw run outputs were never committed.
- A separate hostile verification upheld the NOISE verdict with that caveat.
- Since then the repository requires diagnostic reports to commit their raw run outputs.

## What it taught

- A suspected flaw is cheapest to settle with a rule written before the rerun. Without one,
  "−0.19" invites an argument about whether it matters.
- A reproduction gate (Run A) has to pass before the comparison run means anything.
- Reproduction claims are only as strong as the artifacts kept to back them. This one was downgraded,
  on the record, from "byte-identical" to "summary-statistic".

*Sources: the private sensitivity audit and its hostile verification; commits `9f5afaf`, `85867f6`,
`57bc570`.*
