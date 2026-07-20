# Worked Example: Killing a Mean-Reversion Hypothesis

> **What this is.** A single hypothesis carried end-to-end through the validation
> pipeline — from idea, to parameterization, to purged walk-forward, to a
> statistical verdict — where the verdict is **reject**. It is included precisely
> *because* it fails: a rejected hypothesis discloses no edge, so it can be shown in
> full while live strategies are held back. It is the honest demonstration of the
> machine that decides what this desk is allowed to trade.
>
> **On the numbers.** Every parameter and statistic below is **illustrative** —
> round demonstration values (see `config/strategies/mean_reversion_example.yaml`),
> not tuned figures and not the values of any live or paper strategy. The pipeline
> reaches the same conclusion — *this is noise* — across the plausible region; the
> teaching content is the procedure, not the figures.
>
> **Companion case studies.** Two rejections drawn from the real experiment registry
> extend this one: [`CASE_STUDY_momentum_stage2_rejection.md`](CASE_STUDY_momentum_stage2_rejection.md)
> (a momentum rule that cleared triage, then failed five of six Stage-2 gates) and
> [`CASE_STUDY_lead_lag_audit_correction.md`](CASE_STUDY_lead_lag_audit_correction.md)
> (a rejection audited and re-grounded three days later).

## 1. Hypothesis (pre-registered)

> *Liquid equity-index prices mean-revert intraday: when price is far below a
> trailing moving average in z-score terms, it tends to recover; symmetrically for
> the short side.*

Pre-registration is the load-bearing discipline here. Under the **Validation &
Testing Doctrine (VTD)** the hypothesis, the universe, the parameter menu, the
walk-forward scheme, and the pass/fail thresholds are all committed **before**
any result is seen (`docs/validation_testing_doctrine.md`). Criteria are locked at
the registration commit and are not revised after the fact. This is what prevents
the single most common way backtests lie to their authors.

## 2. Parameterization (bounded, doctrine-limited)

The **Parameter Selection Doctrine (PSD)** caps the search: a small number of free
parameters, a fixed timeframe menu, and an *a priori* selection rule
(`docs/parameter_selection_doctrine.md`). For this example:

| Parameter | Illustrative value | Meaning |
|---|---|---|
| `sma_period_long` | 100 | horizon for the long-side z-score |
| `sma_period_short` | 20 | horizon for the short-side z-score |
| `long_threshold_sigma` | 2.0 | enter long when z below −2.0 |
| `short_threshold_sigma` | 2.0 | enter short when z above +2.0 |
| `exit_sigma` | 0.5 | exit as z returns toward the mean |
| `max_bars_in_trade` | 60 | time-stop |

The point of bounding the search is to keep the number of effective trials small,
so the multiple-testing correction in step 4 is honest rather than cosmetic.

## 3. Purged walk-forward (no leakage)

Evaluation uses **combinatorial purged cross-validation**: training and test folds
are separated by a **purge** (drop samples whose labels overlap the test window)
and an **embargo** (a gap after each test fold), so no information leaks across the
boundary. The engine that does this — bar alignment, session awareness, fold
construction, cost model, fill model — is the code in `src/backtest/` and
`src/research/{psd,vtd}/`. Costs are stressed at 1×, 2×, and 4× modeled slippage;
an edge that only survives at zero cost is not an edge.

## 4. Statistical verdict (DSR + PBO)

Two gates decide it, and this hypothesis fails both:

- **Deflated Sharpe Ratio (DSR).** The raw in-sample Sharpe is deflated for the
  number of trials, the non-normality of returns, and the sample length. After
  deflation the probability that the true Sharpe exceeds zero is **not**
  distinguishable from chance. A nominally "positive" backtest Sharpe collapses
  once the search that produced it is paid for. (Methodology:
  `docs/methodology/DSR_BAND_BIAS_DECOMPOSITION.md`.)

- **Probability of Backtest Overfitting (PBO).** Via CPCV, PBO estimates how often
  the configuration that looked best in-sample underperforms out-of-sample. Here
  PBO lands **above** the doctrine ceiling — the "best" parameters are best by luck,
  not by signal.

**Verdict: REJECT — indistinguishable from noise.** No promotion, no paper
allocation, no live capital. The hypothesis is recorded as rejected and does not
come back without a *new*, independently pre-registered reason.

## 5. Why this is the showcase

Anyone can show a backtest that made money. The harder and more valuable thing is
a process that reliably tells you when a backtest that *looks* like it made money
actually didn't — and then stops you from trading it. That process is the product:

- pre-registration that locks the goalposts (VTD),
- a bounded search so corrections are honest (PSD),
- leakage-free purged walk-forward with a real cost/fill model,
- a deflated, multiple-testing-aware verdict (DSR/PBO),
- and governance that treats "reject" as binding.

The strategies that *survive* this gauntlet are what the desk actually trades, and
they are deliberately **not** in this repository. What is here is the gauntlet.
