# Case Study: A Real Anomaly That Failed Its Recent-Period Test (Repurchase Drift)

## Question

After a company actually buys back its own shares, does its stock drift upward relative to similar
companies that did not? And if it once did, does it still?

## Pre-registered criteria

The criteria were committed in `e1ba738` (2026-07-22 23:10 −07:00) together with a written
pre-registration, before any return was computed. The verdict and the statistics code landed in
`cbdce00` (2026-07-23 03:37 −07:00), about four and a half hours later.

A window **passes** only if all of these hold:

- the mean net return per event is positive;
- a placebo-date permutation test (1,000 draws, one-sided) gives p < 0.025, which is Bonferroni 0.05/2
  for the two windows;
- the bootstrap 95% confidence interval excludes zero;
- the full-moment Deflated Sharpe Ratio exceeds 0.95 at a family size of N = 3;
- the confidence interval still excludes zero at 2× costs.

The effect is **promotable** only if **both** windows pass: a deep-history window (2000-01 to 2016-12)
and a primary, recent window (2017-01 to 2025-09).

## Data and method

- **Data:** US equities, using point-in-time fundamentals to identify completed buybacks, with daily
  prices. Only aggregate statistics are reported here; row-level data stays private under the data
  licence.
- **Method:** a matched-control event study. Each event's return is measured against the median of
  matched control companies that had no recent buyback, net of costs.
- **Null:** the placebo test moves each event to a random eligible date for the same company. That
  keeps the same companies and the same overlap structure, so it tests the timing claim like-for-like.

## Result

| | Deep window, 2000-2016 | Primary window, 2017-2025 |
|---|---|---|
| Events | 5,993 | 3,921 |
| Mean net return per event | **+1.12%** | +0.39% |
| Permutation p | **0.002** | 0.358 |
| Bootstrap 95% CI | [+0.68%, +1.55%] | [−0.18%, +0.90%] |
| Full-moment DSR | **0.99998** | 0.706 |
| Pass | **Yes (all criteria)** | No (fails p, CI, DSR and the 2× CI) |

The pre-registered by-decade view shows the decay directly: **+1.76%** per event in the 2000s and
**+0.46%** in the 2010s. In the deep window the effect was concentrated in smaller companies and grew
with the size of the buyback, which is what the mechanism predicts. The matched controls were roughly
flat, so the deep-window result was not produced by a depressed benchmark.

## Decision

**Rejected as a promotable edge.** The deep window passes every criterion, but the recent window does
not, and promotion requires both. The recent result is positive but not significant, rather than
negative, so no inverse ("short the signal") hypothesis was drafted.

## What it taught

- A real anomaly and a tradeable edge are different things. This effect was strong through 2016 and
  appears to have been arbitraged away since.
- Requiring a recent-period pass alongside a deep-history pass is what stopped a historically strong
  result from being promoted.
- The registration predicted the most likely outcome in advance (deep passes, recent is positive but
  insignificant, by-decade shrinkage), and that is what happened. Writing the prediction down first is
  what makes that comparison meaningful.

*Sources: the private verdict record and commits `e1ba738` → `cbdce00`.*
