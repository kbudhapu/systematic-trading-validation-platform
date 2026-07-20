# DDR — Full Investigation Record (Portfolio Piece)
### Daily Displacement Reversal: a complete quant strategy lifecycle, from hypothesis to honest kill

**Status: TESTED — NOT DEPLOYED.** This is a research/methodology record for a portfolio repo. It is not a
live or paper trading system, not investment advice, and does not represent DDR as a viable strategy. The
frozen live registration was never modified during this investigation.

**Timeline:** DDR-F1 registered 2026-08-10 · investigation concluded 2026-09-10.

**What this document is:** the complete lifecycle of one quantitative equity strategy — the mechanism, how
it was made to "work," every improvement avenue tested, the sandbox results, the expansion to honest data,
and the exact evidence on which it was killed. It is written to demonstrate *quant research methodology* —
specifically the discipline of catching a strategy that is fooling you before it costs capital.

**The one-line story:** a strategy that backtested at ~6.5%/yr was proven, through a survivorship-complete
and crash-complete data rebuild, to be a ~break-even, crash-fragile artifact of a flattering sandbox — and
was killed on the evidence, before any capital was risked.

---

# PART I — THE STRATEGY

## 1.1 Origin and hypothesis

DDR ("Daily Displacement Reversal," registered as **DDR-F1**) began from a published retail method (the
"SID method," *Six Figures From Scratch*, Ch. 9) and was formalized into a pre-registered, falsifiable
strategy.

**Economic hypothesis (Gate 0):** multi-day forced selling in liquid US small/mid-cap names pushes price
below fair value; entering at *confirmed exhaustion* via the opening auction and holding through the
unwind collects the concession. The named payer: the displacement's immediacy-demanders. **Family:**
LIQUIDITY-PROVISION (compensation for absorbing forced flow), assigned after a formal family-ruling review.

This is a **long-only, oversold-bounce (mean-reversion) daily swing.** It is *not* a momentum or
trend strategy — it bets that deeply oversold liquid names revert upward.

## 1.2 The mechanism (frozen spec)

| Component | Rule |
|---|---|
| **Universe** | US common stocks, liquid tier (median $vol ≥ $5M/day), price $5–100 at entry; ETFs excluded; SPY/QQQ/IWM quarantined |
| **Arm** | RSI-14 (Wilder) daily close < 30 arms an episode; records the episode low |
| **Fire** | first MACD(12,26,9) signal-line **bullish cross** at any later bar while armed |
| **Disarm** | an RSI > 70 episode disarms without firing |
| **Entry** | market-on-open (MOO), next session's auction |
| **Exit (primary)** | flat at the **30th** session's open; no profit target |
| **Stop** | below the arming episode's low; an open through the stop fills at that open |
| **Delay discount** | size multiplier m(d) = max(0.20, 1 − d/20), d = bars from episode end to the cross |
| **Sizing** | per-trade risk ÷ √(1 + concurrent positions), inside a 20%-annualized portfolio vol target; one position per name; re-arm needs a fresh episode |
| **Direction** | long only (the short/overbought arm had no validated direction trigger) |

**The MACD gate is central to the whole story** (see Part V): it is a *confirmation delay* — the strategy
does not fire the instant a name is oversold; it waits for a MACD bullish cross, i.e. for early evidence
the fall has stopped.

## 1.3 How it was pre-registered (the discipline, before any evidence)

DDR-F1 was frozen as a pre-registration *before* forward evidence existed — every sandbox-derived
parameter pinned as an exact value (not a formula referencing a sandbox object), with a provenance tag on
each:

- **Frozen constants** included: +25.0 bps/trade PASS floor, the m(d) delay discount (explicitly labeled
  *curve-fit*, no mechanism claimed), the $5–100 / $5M liquid tier, the 30-session exit, 400-trade power
  floor, 126-session grading clock.
- **Three verdict arms:** A1 = the vehicle; **A2 = a matched random-entry placebo** (same names, counts,
  holds, sizing — only the timing randomized: the forward falsifier — if A1 ≈ A2, the timing claim dies
  regardless of pooled P&L); A3 = closed shadow ledgers (never verdict-bearing).
- **Temporal-forward wall:** every threshold precedes every evidence datum, git-provably. Grading is one
  event; the sandbox is inadmissible for grading (generation only).

**Why this matters for the piece:** the strategy was built to be *falsifiable* from day one — a placebo
arm, a pre-committed pass floor, a power floor, and a hard wall between the sandbox (hypothesis
generation) and the forward test (evidence). This is the opposite of "tune until the backtest looks good."

---

# PART II — THE SANDBOX RESULTS (how it was made to "work")

## 2.1 The sandbox

The MLQ-WALL-001 sandbox: liquid US small/mid-caps, **2015–2020** (Sharadar-derived). Its explicit
purpose was *free exploratory mining* — outcomes included — with one rule: **sandbox findings are
hypotheses, never evidence.** (This distinction becomes the whole story: the sandbox was used correctly as
a generation surface, but it turned out to be *silently defective*, which no amount of "it's only a
hypothesis" discipline protects against — a poisoned generator produces poisoned hypotheses.)

## 2.2 The apparent edge

On the sandbox, the base mechanism showed a real placebo-beating signal: **real mean at the ~100th
percentile of 200 matched random-entry books, every price band** (random ≈ 0, real +127 to +355 bps). The
displacement-timing claim looked genuine — the placebo arm, the correct falsifier, was passed decisively.

The auction-execution model measured fair (print-location L ≈ 0.46–0.52 in the liquid tier — the
quoted-spread toll is dodged at the open). Costs were modeled but small (~5–7 bps/side).

**Sandbox headline:** the primary long showed positive mean expectancy, placebo-confirmed, with a
tail-heavy distribution — enough to register for a forward test, but explicitly flagged as *fragile,
tail/one-regime-driven, underpowered* (172 primary sandbox trades; expectancy concentrated in the 2020
crash rebound; median negative; the positive sign partly set by the stop-anchor choice).

**The honest weaknesses were named at registration:** generous execution model (charges 0 below mid, no
impact), one stress episode in-sample, the 30-session exit measured on a studied window. The forward test
existed precisely to resolve those.

---

# PART III — THE IMPROVEMENT AVENUES (every lever tested)

Before and around the forward test, a systematic search ran for improvements — an "avenue ledger."
**Every avenue below was tested on the sandbox with a full anti-overfit harness** (walk-forward,
null-baseline, effective-N-by-day for the heavy date-clustering, deflation, net-of-cost). Each is reported
with its honest verdict. This section is the bulk of the *work* — and, critically, almost all of it was
later shown to be optimizing against flattered data (Part IV).

### 3.1 Entry / signal

- **Ground-up entry rebuild (5 sequenced single-job GAs):** searched the whole entry definition — the
  oversold measure (RSI vs z-score vs %-below-MA vs drawdown vs descent-rate), the quality (capitulation
  volume, decline speed/shape, uptrend-vs-downtrend), market/sector/vol context, timing, and a
  backward-from-winners framing. **Finding:** the oversold *measure* barely matters (RSI ≈ best); the
  *quality* signal "fast-crash into oversold" appeared to help; the market-context (breadth) signal was
  the only real driver, AUC ≈ 0.60. **The MACD gate appeared to be a mild leak → the rebuild dropped it
  and entered immediately.** *(This conclusion later reversed on honest data — see Part V.)*

- **Is the MACD hurting? / missed trades:** the MACD-cross filter appeared to filter out good fast-bounce
  entries on the sandbox; raw RSI<30 at a no-stop exit gave ~67% win vs DDR's fired 48%. *(Again — a
  sandbox artifact; the "missed good trades" were the falling knives the sandbox had deleted.)*

- **At-entry winner/magnitude prediction (full-feature GBM):** the shallow-read "entry is weak" did **not**
  understate — a full-feature GBM at entry got AUC ≈ 0.60 ≈ the best single feature (market context/
  breadth). No hidden interaction. Magnitude *was* more predictable (top-quartile MFE AUC 0.70) but the
  *predictable* part of magnitude **decoupled** from winning (it was entry volatility, not edge) — so
  sizing on predicted magnitude would be the classic volatility trap.

### 3.2 Exit / holding

- **The give-back wall:** trade-behavior mapping showed winners peak ~session 14–20 and **~70% of trades
  that reach a >2% peak give it back.** A large structural inefficiency — but:
- **Causal exits all FAIL:** every trajectory-based exit (trailing stop, momentum-stall, give-back-onset,
  partial/scaled) *underperformed simply holding*, because at the decision bar a recoverable pullback and
  a peak-that-gives-back look identical (the give-back wall, confirmed on the exit side — you cannot tell
  the peak from a dip in real time). The give-back is real but **not causally harvestable.**
- **Holding period:** ~20 sessions modestly beat 30 (+~0.9%/yr, noisy) — the one small positive.

### 3.3 Stop

- **The stop is a wall (sandbox) → widen it:** the episode-low stop cut winners as fast as losers (the
  winner-kill vs loser-save exchange rate never favored a separating level). Sandbox conclusion: widen the
  stop. **This *reversed* under the prop drawdown cap** (Part III.7): a wide stop deepens the tail →
  forces smaller sizing under a hard cap → halves the return. Correct answer for a capped account:
  **keep the tighter stop.**

### 3.4 Position management — losers

- **Cutting losers:** a wall. Cutting realizes recoverable losses; every stop level reduced total P&L.
- **Loser-ID (can we flag the dead ones early?):** losers ≈ winners at entry (inseparable); mid-trade
  return-so-far separates them (AUC ~0.68) but only *after* most of the loss is locked in. Two walls —
  can't-avoid-at-entry and can't-cut-in-time.
- **Hold-losers-to-recovery (a real idea):** under a drawdown cap this collapses to cut-all (positions
  down >8% almost always classify as knives) and risks the cap — not a lever.
- **Net: the ONLY loser lever is sizing** (you cannot predict or cut individual losers usefully).

### 3.5 Position management — winners (the one real positive)

- **Causal size-up of revealed winners:** adding size to a position that has *already revealed* itself as
  running (return-so-far positive at session 3–5, causal, no lookahead) — the remaining run is positive
  net-of-cost at every checkpoint (+3.5% at T3). This *worked* (unlike the loser side): acting on
  *revealed strength* beats predicting weakness. Best config: early, low-threshold, ~1× size,
  drawdown-headroom-conditioned. Lift: ~+0.8%/yr (sandbox, best-of-swept — haircut for selection).

### 3.6 Universe

- **Segment-and-fit (price × liquidity × sector × vol):** the FX-Phase-45 pooling lesson applied — does
  the bounce work in wider bands under band-specific treatments? **Finding:** the bounce is broad across
  liquid names (widen the price cap, keep the liquidity floor); the real axis is **liquidity, not price**;
  sub-$1 needs limit orders (order-type saves ~36 bps on wide-spread names). **Crucially:** per-segment
  *bespoke treatments* added nothing — one treatment fits all. The "100 bps the fitting adds" was traced
  to a null-definition artifact (fitted-vs-shuffled-null, not fitted-vs-book); against the *right*
  baseline (book treatment) the fit adds only the ~2 bps order-type effect. A clean example of catching a
  metric measured against the wrong baseline.

### 3.7 Concurrency / correlation

- **Within-cluster allocation:** when a broad selloff arms many names at once (correlated fires — 62 of
  172 sandbox entries on 19 crash days), the 0.60 win-prob is *within-cluster degenerate* (it's a
  date-level regime signal — constant across the cluster). The real within-cluster discriminator is
  **co-movement** (idiosyncratic names bounce +77 bps more than crowd-movers); liquidity runs *backwards*
  within-cluster (illiquid bounce more). A **co-movement cap** modestly improved return-per-drawdown
  (ratio 1.02 → 1.11).

### 3.8 Sizing — the drawdown levers

- **The prop-cap reframe:** the deploy target is a prop account with a **hard drawdown cap** (≤15%, later
  ≤10%). Objective: maximize return *subject to* never breaching the cap — a constraint, not a ratio.
- **Vol-sizing (inverse-vol) is HARMFUL:** it halves the return (6.49 → 3.48%/yr) — because vol and edge
  are *positively coupled* here (high-vol oversold names are the better bounces: Q3 high-vol +793 bps/58%
  win vs Q0 low-vol +37 bps/38%). Sizing *down* high-vol throws away edge. **Keep the existing
  concurrency-based sizing; keep high-vol names.**
- **Vol-regime edge is real but already captured** (DDR trades all vol levels; it already harvests the
  hi-vol bounce). Confirmatory, not additive.

### 3.9 Fundamental / event (the one orthogonal axis)

- **Earnings-knife filter:** oversold *into/after an earnings filing* bounces ~100 bps *worse*
  (fundamental drop, reverts less) — **incremental over the fast-crash technical** (earnings-oversold are
  mostly not fast-crashes), so a genuine orthogonal (non-price) signal. Small (~+0.3%/yr). The first
  entry-side signal that wasn't redundant with the technicals.
- **Sector:** Energy/Utilities oversold are near-knives; Tech/Healthcare bounce hard — real but partly
  co-movement-redundant and regime-confounded.
- **Valuation (P/B): dead.** Short-interest: **absent from the data → acquisition-flagged** (the
  squeeze-bounce hypothesis untested).

### 3.10 Short side (dead both ways)

- **Standalone short (overbought-fade):** killed — drift (shorting a rising market bleeds ~300 bps) +
  borrow (8–15%/yr) + an unbounded short-squeeze left tail (p99 +54%, worst trades <−100%). Net −263 bps.
- **Long/short market-neutral (the reserved crash-hedge idea):** the pair *is* market-neutral (drift
  cancels, beta +0.016) — but the short leg is **anti-hedged**: it loses *more* in crashes (−135 bps) than
  calm (−54 bps), because overbought-fade shorts get squeezed by crash-driven bear rallies exactly when a
  hedge is needed. The pair **halves the return** (2.71 vs 6.49%/yr) and *deepens* the worst drawdown.
  **There is no prediction-free structural crash-hedge for this strategy.**

### 3.11 Name-quality / re-entry (a textbook overfit trap, correctly rejected)

- **"Some names bounce reliably → concentrate":** the killer test — do train-reliable bouncers stay
  reliable out-of-sample? **corr(train win-rate, test win-rate) = 0.06** (≈ zero); the "reliable" names
  had *lower* OOS return. Pure look-ahead selection on tiny per-name samples (median 4 events/name).
  Concentration also loses (fights the breadth edge). **Killed.** A clean demonstration of not falling for
  the most seductive overfit in equities.

### 3.12 Intra-day / multi-timeframe

- **Does finer resolution sharpen the weak daily signals?** Full-feature GBM on 1-min → hourly aggregates
  vs daily: **intraday-alone is random OOS (AUC 0.49); the combination overfits (train up, test down);
  daily-alone is the best generalizer.** The expensive minute cache buys nothing for entry-quality — a
  clean more-features-overfit result caught by walk-forward.

### 3.13 The combine (the integration)

Assembling the "validated" levers (selective entry + tight stop + hold-20 + size-up + concurrency/
co-movement caps + earnings-avoid) into one system: the sum of the isolated gains (12.08%/yr) collapsed to
**5.23%/yr combined — a 57% over-counting gap.** Why: the levers **anti-stack** — the selective entry
*already* solves the drawdown problem the DD-caps were built for, so the caps become redundant and only
cut return; and the over-selective entry draws down so little (−1.5%) it *under-uses* the drawdown budget,
under-earning. **Lesson: you can only reduce drawdown once; additional drawdown-reducers are pure cost.**
The original DDR (which *uses* its drawdown budget) remained competitive with the over-engineered rebuild.

### 3.14 ML lever-combination search

An exhaustive combinatorial search (384 combos) over the levers, maximizing return subject to the
drawdown cap. **In-sample it found a 7.8%/yr combo; the deflation harness proved it noise:** deflated to
~p90 ≈ 5.4%/yr; train-rank was *orthogonal* to test-rank (one "best" train combo *lost money* OOS); the
best combo was an order-statistic of the trial distribution. **Return-side levers (selective entry, short
hold, tight stop) were unanimously selected and robust; DD-side levers were coin-flip.** Confirmed: with
~3 sandbox crashes, the DD-side is *not learnable*, and the search only curve-fits.

---

# PART IV — THE EXPANSION (the honest data rebuild)

## 4.1 Why expand — the diagnosed binding constraint

Every DD/crash result failed for the *same* reason: **N = 3 crashes** in the sandbox. The DD-levers were
coin-flip, the crash characterization was underpowered (N=12 episodes), the walk-forward "test" was ~1.5
years / one crash. The sandbox was **exhausted as a training set for the drawdown/crash side** — it
structurally did not contain enough crashes to train crash-survival. The diagnosed fix: pull more
crash-inclusive history.

## 4.2 What the reproduction gate caught (two silent sandbox defects)

Before trusting any extended result, a **reproduction gate** was built: process the honest data, slice it
to 2015–2020 under the sandbox's own filters — it must reproduce the sandbox. It reproduced **byte-
identical on price** (6,086,478/6,086,478 rows, max relative error 0.0) and 98.9% on trades — and the
residual exposed **two undocumented filters the sandbox had silently applied:**

1. **All of 2018 was dropped** (a whole year, including the 2018-Q4 selloff).
2. **Every row with close < $1.00 was dropped** — i.e. **the falling knives** (names cratering in crashes)
   were silently removed. A survivorship-lite leak.

Both defects **removed crash data**, making crashes look milder than they were. This is the pivotal
finding: *the sandbox was flattering the strategy by hiding crash severity, and no "sandbox is only a
hypothesis generator" discipline protects against a generator that is systematically biased.*

## 4.3 The honest rebuild (2004–2022)

A survivorship-complete Sharadar rebuild to a **separate store** (sandbox left frozen), built to full-truth
standards:

- **29.1M rows, 16,099 names, 71% delisted names retained** with history to last tradeable price.
- **5 separable crash regimes** (vs the sandbox's ~3): 2008 GFC (−66%), 2015–16, 2018-Q4, 2020 COVID, 2022.
- **Survivorship guards:** point-in-time universe from *live-at-T* status (NOT the hindsight `isdelisted`
  flag — which would re-introduce survivorship bias); delisted names included; **delisting-exit modeled
  honestly** (bankruptcy/regulatory knives eat the loss to last tradeable price; halted-then-delisted
  names eat the un-exitable gap; M&A exits at deal) — 25% of delisted names died sub-$1, the knife tail
  the sandbox removed.
- **Era-cost anchor-calibrated:** a naive spread-from-range proxy overstated cost 2.78× (it reads crash
  *volatility* as spread); anchored to the trusted 2015–20 ~12 bps, quiet spread is roughly flat across
  eras — the *real* era-varying cost is crash slippage + halt/delisting gaps.
- **Corporate-action handling** (reverse-splits, ticker-changes) and **SF1 point-in-time** fundamental
  join (no look-ahead).

---

# PART V — WHY IT WAS KILLED (the honest verdict)

Re-run on honest data with **leave-one-crash-out cross-validation** (calibrate sizing on 4 crashes, realize
the drawdown on the held-out 5th — the honest successor to the uninformative 1.5-year walk-forward), both
the original MACD-gated strategy and the rebuilt no-MACD version:

## 5.1 The entry edge inverted — and MACD was vindicated

| Entry | Result on honest data |
|---|---|
| No-MACD, fast-crash-immediate (the "rebuild") | **−76 bps/trade, 34.8% win — LOSES** |
| Original MACD-gated | **+40 bps/trade, 41.7% win — marginally positive** |

The sandbox's "fast-crash helps, drop MACD" conclusion was an **artifact of the deleted knives** — on the
sandbox, fast-crashes looked like great bounces because the ones that cratered below $1 had been removed.
With the knives restored, entering fast into a crash is a *loser*. **The MACD confirmation delay is
protective** (it waits past the worst of the fall). *Removing MACD had been a mistake; the honest data
reverses it.* But note: MACD's help is per-trade *timing*, not crash-avoidance — MACD-on actually fires 3×
more trades with *higher* crash concurrency (it participates *more*, it just enters slightly better).

## 5.2 The drawdown side does not survive — the decisive failure

Leave-one-crash-out realized drawdown (sizing calibrated to a 13% budget on the other 4 crashes),
MACD-on:

| Held-out crash | Realized drawdown |
|---|---|
| **2008 GFC (the hard bar)** | **−68.9%** — budget breached ~4.6× |
| 2020 COVID | −38.5% |
| 2022 | −37.5% |
| 2015–16 | −35.9% |
| 2018-Q4 (mildest) | −4.7% ✓ (the only fold that holds) |

**4 of 5 folds breach the drawdown budget; the GFC fold is catastrophic.** A sizing rule calibrated
without a mega-crash *cannot* survive one — and mega-crash severity is *not early-observable* (a separate
crash-characterization test showed crash outcomes are driven by timing-within-the-event and are not
predictable from early features). MACD softens the GFC drawdown (−69% vs the no-MACD −99%) but does **not**
fix it. There is no prediction-free crash-defense (the long/short hedge was anti-hedged, §3.10).

## 5.3 The honest return is ~break-even

Return at a 13% drawdown cap on honest data: **MACD-on ≈ +0.35%/yr** (+0.27%/yr at a 10% cap) — versus the
sandbox's artifact **6.5%/yr**, and an order of magnitude below any deploy bar (~5%/yr, Calmar ≥ 0.4). The
edge is **real in recovery** (post-trough oversold bounces earn +400 to +1000 bps in 2009, 2020) and
**negative into crashes** — untimed, these roughly cancel to zero, and the trough-timing needed to
separate them is not causally available.

## 5.4 The ML search is noise (confirmed on honest data)

Leave-one-crash-out over the lever combinations: best combo **indistinguishable from the noise-max** of the
trial set (order-statistic z = 0.92 vs expected-max 2.78; fails deflated-Sharpe). Even with 5 crashes, the
DD-side combinatorial search curve-fits.

## 5.5 Disposition

**DO NOT DEPLOY as-is.** On honest, crash-complete, survivorship-complete data, DDR's oversold-bounce is
**break-even (≈+0.3%/yr) and crash-fragile** (breaches the drawdown budget ~4.6× on a GFC-scale crash). The
apparent sandbox edge was an artifact of dropped falling-knife data, a dropped year, and a crash-poor
window. **Keep the MACD gate** (strictly better than the no-MACD rebuild). The frozen registration is
untouched; nothing is deployed.

**The one open thread (a NEW hypothesis, not a rescue):** a recovery-regime-gated variant — trade the
bounce only after a trough — is the single place a real edge lives, but it requires trough timing that
prior work showed is not early-observable, and would need to be scoped fresh (on honest data, never by
tuning DDR until it passes).

---

# PART VI — WHAT THIS DEMONSTRATES (the methodology)

The deliverable is **not a profitable strategy** — it is a demonstration of the discipline that separates a
quant researcher from a curve-fitter: *the ability to catch a strategy that is fooling you, before it
costs capital.*

The techniques that did the work:

1. **Pre-registration with a placebo arm** — a matched random-entry control as the forward falsifier; the
   strategy was falsifiable from day one, with pinned constants committed before any evidence.
2. **The reproduction gate** — build the honest data, prove it reproduces the frozen sandbox *before*
   trusting any new result. It caught two silent sandbox defects that had biased every prior result.
3. **Survivorship-completeness** — insisting on delisted names and falling-knife data, because crashes
   *kill stocks*; a survivors-only backfill makes crashes look survivable when they aren't. Point-in-time
   universe from live-at-T status, not the hindsight delisted flag.
4. **Leave-one-crash-out CV** — validating crash-survival by holding out each crash, with the GFC-held-out
   fold as the real bar (generalize to an *unseen* mega-crash).
5. **Anchor-calibrated era cost** — refusing to assume; calibrating the cost proxy against a trusted number
   and measuring its 2.78× bias.
6. **Concrete deflation** — order-statistic and deflated-Sharpe checks on the combinatorial search;
   rejecting the "best" combo as an order-statistic of noise.
7. **Baseline discipline** — measuring "improvements" against the *right* baseline (fitted-vs-book, not
   fitted-vs-shuffled-null), which turned an apparent +100 bps into a real ~2 bps.
8. **Right-tool matching** — GA/decision-tree for interpretable rule search, not a data-hungry CNN;
   ML only where the problem is genuinely ML-shaped, and deflated when it isn't.

**The hardest-won lesson (a real sequencing error, honestly recorded):** when crash-survival is the binding
constraint, get crash-complete data **first** — crash analysis on crash-incomplete data produces
*confident, wrong* survival numbers. Much of Part III optimized against a sandbox that was hiding the
crashes; the honest move was to pull complete data the moment crash-survival became the constraint, not
after 40 tests. The investigation still reached the correct terminal answer — but the ordering is a lesson
carried forward.

**Core takeaway:** DDR would have looked deployable (~6.5%/yr) and blown a drawdown-capped account on the
first real crash (a −69% held-out GFC drawdown against a 13% budget). The method surfaced this *on data,
before capital.* A negative result, honestly earned and fully documented, is the point — and it is a
stronger signal of quantitative rigor than a backtest one cannot tell is overfit.

---

## Appendix — companion documents in this repo
- `DDR_WRAPUP_RECORD.md` — the concise disposition summary.
- `ALPHA_GENERATION_ARCHITECTURE.md` — the domain-general methodology (the reusable engine: map → mechanism-
  hypothesis → validate-the-conditioning-event → match-tool → anti-overfit harness → mirage-guards → read
  honestly → dispose), with each rule traced to a specific investigation mistake and correction.

*Research record only. Not investment advice. Not a deployed or paper-trading system. The frozen live
registration was never modified during this investigation.*
