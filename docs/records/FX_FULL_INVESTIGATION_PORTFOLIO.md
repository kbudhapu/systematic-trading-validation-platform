# FX — Full Investigation Record & Disposition (Portfolio Piece)
### Foreign exchange: mapping a market's edge structure to a disciplined decision not to trade it

**Status: MAPPED — STEPPED AWAY (not deployed).** This is a research/methodology record for a portfolio
repo. It is not a live or paper trading system and not investment advice. It documents a multi-phase
investigation of FX (spot/price and beyond-price) that concluded — consistent with the academic literature
— that retail-accessible FX edge is thin, risk-premium-dominated, or firm-gated, and that FX does not
merit further capital or data investment *for this operator's situation right now.*

**Timeline:** FX investigation concluded and disposition recorded 2026-09-10.

**The one-line story:** an exhaustive, harnessed search of FX price-based edge found real-but-thin signals
that don't clear costs; a survey of the broader literature confirmed the durable edges are either risk
premia (compensation for crash risk, not alpha) or structurally firm-gated (flow data, institutional
funding, or speed) — so the disciplined decision is to record the map and step away, not to keep digging
in a market whose edge structure is now well-understood.

---

## 0. Why this is a portfolio piece (read this first)

Killing one strategy (see the DDR record) shows you can catch a backtest lying. This record shows
something rarer and more senior: **the ability to map an entire market's edge structure, reach the same
conclusion the academic canon reached, understand *why* mechanistically, and make a disciplined
resource-allocation decision to step away.** Knowing when *not* to trade — and being able to say precisely
*why* — is most of risk management. The deliverable is not a strategy; it is a defensible map of where FX
edge does and does not live, and the judgment to act on it.

---

## PART I — WHAT WAS INVESTIGATED (the FX arc)

The investigation ran in phases across two broad classes.

### Price-based (spot OHLC/tick) — the bulk of the arc
- **Support/demand zones, levels, round numbers** — tested whether price "reacts" at structural levels.
  *Result: zones bounce like random lines* (a formal test against randomly-placed levels showed no
  excess reaction). No forcing function → no persistence.
- **Direction prediction** — can the sign of the next move be forecast from price? *Result: a real but
  tiny signal (a magnitude-isolated search found direction significance, z≈7.7, OOS-validated) that is
  sub-spread* — real, but smaller than the transaction cost, hence untradeable.
- **Magnitude / volatility** — the second moment was consistently *more* predictable than direction, but
  the predictable part was *priced* (the volatility risk premium had not narrowed) and decoupled from
  directional profit.
- **Mean-reversion / vol-reversion** — statistical patterns, spread-sized, net-negative as a book.
- **GA / RL / CNN searches** (direction × magnitude, amplifier objectives) — the most capable searches
  found the direction signal was real but sub-spread; a null-data baseline out-performed the real signal
  on the harder objective (i.e., the search was fitting noise once the thick part was exhausted).

**Price-based conclusion:** FX offers *martingales* (direction real-but-sub-spread, untradeable) and
*risk premia* (carry — decaying, uncappable tail, not alpha). No deployable alpha in spot price. The
bounded remaining miss is *non-price data*.

### Beyond-price — the positioning proxy (the one free structural test run)
- **CFTC Commitments of Traders (COT) positioning** — the free, retail-accessible proxy for the
  customer-flow segmentation research (informed leveraged funds vs forced/uninformed hedgers). Tested
  three ways: as a standalone slow positioning signal, as a context-filter on a faster price signal, and
  as a change-interaction (confirm/exhaust/diverge) with the live price move. *Result: null across all
  three, and the interaction failed a shuffled-context placebo* (a randomly-shuffled positioning context
  reproduced the result — no information). The weekly lag (Tuesday snapshot, Friday release) structurally
  blunts the "live" read. The *free* flow proxy is empty; the *rich* flow data (CLS) is a paid,
  untested investment.

---

## PART II — HOW THE LITERATURE CONFIRMS IT (the survey)

A structured survey of the FX research literature — organized by mechanism, honest replication status,
and "who pays" — independently confirmed the arc's conclusions. The framing null, which every claim must
clear: **Meese & Rogoff (1983)** — structural models can't beat a random walk for majors at short
horizons — and **Rossi (2013, *Journal of Economic Literature*)** — predictability remains episodic and
specification-dependent thirty years later.

The literature's edge structure, classified by "who pays":

**Risk premia (real, replicated, retail-accessible — but BETA, not alpha):**
- **Carry** (Lustig-Roussanov-Verdelhan 2011; Brunnermeier-Nagel-Pedersen 2008; Menkhoff et al. 2012) —
  the most replicated FX result, but it is compensation for being short crash/volatility risk ("up the
  stairs, down the elevator"); decayed in the 2010s zero-dispersion era. *Pennies in front of a
  steamroller.*
- **Momentum** (Menkhoff et al. 2012) — an anomaly protected by limits-to-arbitrage; profits concentrate
  in high-cost minor currencies where the costs that protect it also eat the retail trader. Largely dead
  in G10 majors net of cost post-2010.
- **Value** (Asness-Moskowitz-Pedersen 2013) — slow, low-Sharpe, a diversifier not a standalone.
- **Volatility risk premium** (Della Corte-Ramadorai-Sarno 2016) — sell options, earn the premium; same
  short-crash epitaph.

**Structural / informational edges (real, well-documented — but FIRM-GATED):**
- **Order flow** (Evans-Lyons 2002) explains 40-60% of daily moves — but the flow visibility is the asset,
  and banks own it (*data-gated*; the rich aggregated version, CLS, is purchasable but expensive and
  untested).
- **CIP / cross-currency basis quarter-end** (Du-Tepper-Verdelhan 2018) — a persistent, regulation-driven
  arbitrage failure — but harvesting it needs institutional balance sheet (*capital/funding-gated*).
- **The WM/R 4pm fix / benchmark flows** — forced flow with public timing, but the easy version was
  criminalized and is policed/crowded, and the edge needs size.
- **Triangular / LOB microstructure** — *speed-gated* (HFT, colocation).

**Tools — honest read (genuinely useful vs rigor-theater):**
- **Genuinely useful (on estimation problems, never on the mean of price):** covariance cleaning
  (Ledoit-Wolf shrinkage / random-matrix filtering), the dollar+carry two-factor risk decomposition,
  Kalman/state-space adaptive relationship estimation, regime classification *as conditioning*, intraday
  seasonality, rough-volatility / HAR volatility forecasting, execution-cost modeling.
- **Rigor-theater when aimed at price direction:** wavelets, spectral/Fourier cycles, path signatures,
  Hurst/fractal, transfer entropy, and deep nets on raw OHLC. The recurring fraud mechanism in the
  "decompose-then-predict" genre is *decomposition leakage* (transforming the full series before the
  train/test split leaks the future into training).

**The survey's portable synthesis:** *the mean of FX is nearly unforecastable, but the second moments,
the relationships, the calendars, and the compelled participants are not.* Every durable result lives on
one of those four — and the entire price-based arc had been attacking *the mean* (the one dead axis).

---

## PART III — WHY STEP AWAY (the disciplined decision)

The decision to shelve FX is a resource-allocation judgment, made explicit:

1. **The risk premia are not worth it for this situation.** Carry / vol-selling are real returns but are
   *compensation for bearing an uncappable crash tail* — "pennies in front of a steamroller." For a
   capital-constrained operator with a hard-drawdown objective, harvesting a modest premium with a
   catastrophic tail is a poor trade.

2. **The rich data is an investment that does not pencil out right now.** The one genuinely-alpha class
   (customer flow) is *data-gated but acquirable* — CLS aggregated flow data is a purchase, not a
   membership wall. But it is expensive (institutional pricing), and the *free* proxy for the same
   mechanism (COT) tested null, so buying the rich version is a speculative data bet not justified at
   this stage.

3. **The remaining structural edges are capital- or speed-gated** — CIP basis needs an institutional
   balance sheet; triangular/LOB needs HFT infrastructure. These are not acquirable for this operator by
   any amount of modeling.

4. **The one untested retail-reachable direction (the second-moment / volatility axis) is
   honest-but-modest.** Vol *is* forecastable (rough-vol/HAR), and the tools are genuinely useful there —
   but harvesting it is still *skillful risk-premium timing*, not alpha, and it does not escape the
   "durable FX = risk premia" conclusion. It is noted as a future option, not pursued now.

**Net:** FX's edge structure is now well-mapped and stable. The retail-accessible edge is thin and
risk-premium-dominated; the alpha is firm-gated (data, capital, or speed). Continuing to dig in a market
whose structure is understood — rather than reallocating to a domain that structurally fits a
capital-constrained modeler — would be motion without expected value. **The disciplined decision is to
record the map and step away.** FX is revivable if the situation changes (real capital, a firm seat with
flow data, or a specific edge in the second-moment/volatility axis) — the door is left open, the shovel
is put down.

---

## PART IV — WHAT THIS DEMONSTRATES (the methodology)

- **Harnessed, honest search of an entire edge class** — price-based FX tested with walk-forward,
  null-baseline, effective-N (for heavy date-clustering), deflation, and net-of-cost, reaching
  "real-but-sub-spread" cleanly rather than curve-fitting a positive.
- **The free-proxy-before-paid-data discipline** — testing the *free* structural proxy (COT) with a
  shuffled-context placebo *before* committing to the expensive rich data (CLS), and correctly declining
  the purchase when the free proxy was null.
- **Literature triangulation** — reaching a conclusion empirically and then confirming it against the
  academic canon (Meese-Rogoff, Rossi, and the mechanism papers), and — critically — being able to
  separate the genuinely-useful tools from the rigor-theater (covariance cleaning: real; wavelets-on-price:
  leakage-driven fraud).
- **The "who pays" discipline** — classifying every edge as risk premium (paid for risk) vs anomaly
  (mispricing that fades) vs structural/informational (someone compelled), which is the lens that turns
  "it didn't work" into "here is precisely where the money is and why I can't reach it."
- **Resource-allocation judgment** — the senior skill of deciding *not* to trade a mapped market and
  reallocating, with the reasoning made explicit and the door left open.

**Core takeaway:** FX is efficient enough that the retail-accessible, price-based edges are thin or
priced, and the durable alpha is structurally reserved for participants with flow data, institutional
funding, or speed. This is not a failure of effort — it is the correct, literature-confirmed map of the
market, and the disciplined response to that map is to step away rather than to keep digging.

---

## Appendix — companion documents in this repo
- `DDR_FULL_INVESTIGATION_PORTFOLIO.md` — the equities companion (a strategy killed on honest data).
- `ALPHA_GENERATION_ARCHITECTURE.md` — the domain-general methodology engine.

*Research record only. Not investment advice. Not a deployed or paper-trading system. No live trading was
conducted. Timeline: FX investigation concluded and disposition recorded 2026-09-10.*
