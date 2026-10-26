# Case Study: Insider Cluster Buying, a Significant Signal With a Negative Level

## Question

When several company insiders buy their own company's stock at around the same time, does the stock
then outperform the market enough to trade, net of costs?

## Pre-registered criteria

The pre-registration was committed in `bfe334d` (2026-07-09 10:43 −07:00). The statistics code and the
verdict followed in `7a3b366` (2026-07-09 19:19 −07:00).

- **Primary statistic:** mean SPY-adjusted net log return per event over a fixed holding period.
- **Test:** a placebo-date permutation test (1,000 draws, one-sided, p < 0.05; a single trial). Each
  event keeps its company and holding period but is moved to a random eligible date, which isolates
  the timing claim: do post-cluster windows beat the same companies at arbitrary times?
- **Pass condition:** the result must be significant **and** on the hypothesised side of zero.
- **Binding amendment at the coverage checkpoint:** a rejection is final, because the available sample
  can only flatter the result.

## Data

- **Source:** an EDGAR point-in-time store of **2,017,093** insider-ownership filings (Forms 3, 4 and
  5, including 1,805,180 Form 4s) filed 2016 to 2025, with daily prices.
- **Events:** 11,308 insider-buying clusters from 2017-01-03 to 2025-11-21.
  - **6,497 were tradeable** after liquidity and data filters, across **2,654** tickers. These are the
    primary sample.
  - 3,694 fell below the liquidity floor and are a diagnostic only.
  - 1,117 had missing data and enter only through a worst-case bound.

## Result

| Measure | Value |
|---|---|
| Mean SPY-adjusted net return per event | **−1.97%** (gross −1.55%) |
| Bootstrap 95% CI | [−2.61%, −1.37%], entirely below zero |
| At 2× / 4× costs | −2.39% / −3.24% |
| Worst-case survivorship bound | −4.62% per event |
| Placebo-date null | centred at −2.87% per event |
| Permutation p (vs that null) | 0.001 |

The decisive reading is in the null.
- The companies insiders buy into lose about 2.9% against the market over *any* random window of the
  same length.
- Post-cluster windows lose less (−1.97%), so the timing signal is real and significant: clusters do
  mark relative bottoms.
- But it only softens the companies' underperformance; it does not overcome it. The level is negative
  in 8 of 9 years.

## Decision

**Rejected at triage, final.** The positive-drift hypothesis fails because the result sits on the wrong
side of zero. No inverse ("short the cluster") hypothesis was drafted: shorting post-cluster windows
would do *worse* than shorting the same companies at random times.

## What it taught

- "Statistically significant" and "tradeable" are different questions. Here the timing effect is
  significant while the tradeable long loses money.
- A placebo null built from the same companies separates a timing effect from a property of the
  companies themselves. A plain market benchmark would have hidden that.
- Locking "a rejection is final" in advance prevented a costly data purchase. Better survivorship
  coverage could only have made this result worse.

*Sources: the private verdict record, commits `bfe334d` → `7a3b366`, and the private research vault.*
