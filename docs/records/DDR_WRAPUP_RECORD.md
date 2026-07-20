# DDR — Investigation Wrap-Up & Disposition

**Status: TESTED — NOT DEPLOYED.** Research-complete; disposition is *do not deploy as-is*.
This is a research/methodology record for a front-facing repo. It is **not** a live or paper deployment,
and it does **not** represent DDR as a viable trading strategy. The honest evidence is below.

**Disposition date:** 2026-09-10.

---

## 0. One-paragraph summary (the honest headline)

DDR-F1 is a long-only, oversold-bounce daily swing on liquid US small/mid-caps (RSI<30 arm → MACD-cross
confirm → next-open entry → ~30-session hold → episode-low stop). On the original sandbox it looked
viable (~6.5%/yr at a ~13% drawdown cap). **A survivorship-complete, crash-complete data rebuild
(2004–2022, incl. the 2008 GFC and the falling-knife/penny data the sandbox had silently dropped) revealed
the edge was largely a sandbox artifact: on honest data the strategy is ~break-even (≈+0.3%/yr) and
crash-fragile (a GFC-scale crash breaches the 13% drawdown budget ~4.6×).** The investigation's value is
the *method that caught this before any capital was risked*. Disposition: **not deployable as-is.**

---

## 1. What DDR is (the mechanism)

- **Family:** long-only oversold-bounce (mean-reversion) daily swing. Published-method origin ("SID
  method"), then pre-registered for forward validation.
- **Signal:** RSI-14 < 30 arms an episode (records the episode low); first MACD(12,26,9) bullish
  signal-cross while armed fires; disarm if RSI > 70 first.
- **Entry:** market-on-open, next session. **Exit:** flat at ~30th session open. **Stop:** arming
  episode's low.
- **Universe:** liquid US common ($5–100, median $vol ≥ $5M), ETFs excluded, one position/name/episode.
- **Sizing:** risk ÷ √(1+concurrent) inside a vol target; own compounding envelope; gross cap.
- **Frozen registration** (untouched throughout): the live vehicle was registered for a *forward paper
  grading*, never promoted on the backtest — the sandbox was always inadmissible for grading.

---

## 2. The headline finding: the sandbox flattered the strategy

The original sandbox (2015–2020) had three defects that all pointed the same way — **making crashes look
milder than they were:**

1. **2018 was entirely missing** (a whole year, incl. the 2018-Q4 selloff) — a processing gap, not a
   source gap.
2. **All rows with close < $1.00 were silently dropped** — i.e. the *falling knives* (names cratering in
   crashes) were removed. A survivorship-lite leak.
3. **The window contained no mega-crash** — the deepest event was the 2020 COVID V (~−18% at the book
   level), no 2008 GFC (−65%).

These were discovered via a **reproduction gate** (build the honest data, slice it to 2015–2020 under the
sandbox's own filters — it must reproduce the sandbox results). It reproduced *byte-identical on price*
and 98.9% on trades, and the residual exposed the two silent filters above. **The gate did exactly its
job: it proved the pipeline was consistent AND caught that the sandbox itself was hiding crash severity.**

---

## 3. The honest data rebuild (2004–2022)

Extended, **survivorship-complete** Sharadar rebuild to a separate store (sandbox left frozen):
- 29.1M rows, 16,099 names, **71% delisted names retained** with history to their last tradeable price.
- **5 separable crash regimes** (vs the sandbox's ~3): 2008 GFC (−66%), 2015–16, 2018-Q4, 2020 COVID,
  2022 — plus 2011.
- **Era-appropriate cost**, anchor-calibrated: a naive spread-from-range proxy overstated cost 2.78× (it
  picks up crash *volatility* as spread); calibrated against the trusted 2015–20 ~12 bps, quiet-period
  spread is roughly flat across eras. The *real* era-varying cost is crash slippage + halt/delisting gaps
  (from the delisting-exit model), not the quiet spread.
- **Delisting-exit modeled honestly:** bankruptcy/regulatory knives eat the loss to last-tradeable price;
  halted-then-delisted names eat the un-exitable gap; M&A exits at deal. 25% of delisted names died
  sub-$1 — the knife tail the sandbox had removed.
- **Point-in-time universe** built from live-at-T status (NOT the hindsight `isdelisted` flag — that back
  door would re-introduce survivorship bias).

---

## 4. The verdict on honest data (leave-one-crash-out CV)

**Entry (return-side), re-validated (not inherited):**
- No-MACD fast-crash-immediate (the "rebuild"): **−76 bps/trade, 34.8% win — LOSES.** The sandbox's
  "fast-crash helps / drop MACD" conclusion was an artifact of the dropped knives.
- **Original MACD-gated entry: +40 bps/trade, 41.7% win — marginally positive.** The MACD delay is
  protective *at the per-trade level* (enters after the worst of the fall). **→ Keep MACD; removing it
  was a mistake.**
- Both bleed every crash/bear year (2008 −17%, 2015, 2018, 2022 all negative at the book level) and earn
  only in recoveries (2009 +11%, 2020 +15%). The edge is **real in recovery, negative into crashes.**

**Drawdown (crash-survival), the decisive failure — leave-one-crash-out, sizing calibrated on the other
4 crashes, realized on the held-out crash:**

| held-out crash | realized DD (MACD-on) |
|---|---|
| **2008 GFC (the hard bar)** | **−68.9%** (budget breached ~4.6×) |
| 2020 COVID | −38.5% |
| 2022 | −37.5% |
| 2015–16 | −35.9% |
| 2018-Q4 (mildest) | −4.7% ✓ (only one that holds) |

**4 of 5 folds breach the 13% budget; the GFC fold is catastrophic.** A sizing rule calibrated without a
mega-crash cannot survive one — and mega-crash severity is not early-observable (shown separately). MACD
softens the GFC DD (−69% vs the no-MACD −99%) but **does not fix it** — and the "MACD avoids crashes"
premise is *refuted*: MACD-on fires 3× more trades and has *higher* crash concurrency (it participates
more, it just times entries slightly better).

**Return at the cap (honest):** MACD-on **≈ +0.35%/yr** at a 13% cap (+0.27%/yr at 10%) — vs the sandbox's
artifact 6.5%/yr, and an order of magnitude below any deploy bar (~5%/yr, Calmar ≥ 0.4).

**ML lever-combination search:** best combo indistinguishable from the noise-max of the trial set
(order-statistic z = 0.92 vs expected-max 2.78; fails deflated-Sharpe). **ML is not warranted — curve-fit
noise.**

---

## 5. Disposition

**DO NOT DEPLOY as-is.** On honest, crash-complete, survivorship-complete data the DDR oversold-bounce is
break-even (≈+0.3%/yr) and crash-fragile (breaches the drawdown budget 4.6× on a GFC-scale crash). The
apparent sandbox edge was an artifact of dropped falling-knife data, a dropped year, and a crash-poor
window.

**What is kept / true:**
- **Keep the MACD gate** (strictly better than the no-MACD rebuild — protective per-trade).
- The edge is **real in recovery** (post-trough oversold bounces earn +400 to +1000 bps) — but capturing
  it requires trough/recovery timing, which prior work showed is **not early-observable** (you can't
  causally tell a bottom from a mid-crash knife). So the recovery edge is un-timeable as-is.
- Frozen registration untouched; sandbox untouched.

**Open thread (a NEW hypothesis, not a rescue):** a recovery-regime-gated variant — trade the bounce only
after a trough — is the one place a real edge lives, but it faces the known "crash-timing is not
early-observable" wall and would need to be scoped fresh (on honest data, not by tuning DDR until it
passes).

---

## 6. Why this is a portfolio-worthy result (the methodology)

The deliverable is not a profitable strategy — it is a **demonstration of quant rigor that catches a
strategy fooling itself:**

- **Reproduction gate** — built the honest data, proved the pipeline reproduces the frozen sandbox
  before trusting any new result; the gate *caught two silent sandbox defects*.
- **Survivorship-completeness** — insisted on delisted names + falling-knife data (crashes kill stocks;
  a survivors-only backfill makes crashes look survivable when they aren't).
- **Leave-one-crash-out CV** — validated crash-survival by holding out each crash, flagging the
  GFC-held-out fold as the true bar (generalize to an unseen mega-crash).
- **Anchor-calibrated era cost** — refused to assume; calibrated against a trusted number and measured
  a 2.78× proxy bias.
- **Concrete deflation** — order-statistic + deflated-Sharpe on the ML search; rejected the "best" combo
  as noise.
- **Honest sequencing lesson (earned):** when crash-survival is the binding constraint, get
  crash-complete data *first* — crash analysis on crash-incomplete data produces confident, wrong
  survival numbers.

**Core takeaway:** the strategy would have looked deployable (~6.5%/yr) and blown a drawdown-capped
account on the first real crash. The method surfaced this on data, before capital. A negative result,
honestly earned, is the point.

---

*Research record only. Not investment advice. Not a deployed or paper-trading system. The frozen live
registration was never modified during this investigation.*
