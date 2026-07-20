# PAD Amendment Drafts — 2026-07 (FIX-7)

**Status: DRAFTS for operator decision.** Parts A and C4 are pre-registration *drafts* (nothing
adopted, no capital moved). Part B records an operator ruling already made. The U6 safe layers
(estimator + diagnostic logging + sufficiency check) are **built dormant**; Tier-2/Tier-3 activation
is **gated** and requires a separate operator ack to wire to live decisions. All U-x priors/thresholds
ship **UNVALIDATED (TYPE-2)** with replacement triggers per `MAGIC_NUMBERS_2026-07.md`.

---

## PART A — PB-3 + U3 cold-start amendment (DRAFT; registry `STANDING-COLDSTARTBRIDGE`)

**The problem (PB-3):** admission needs ≥52wk overlap and the correlation gate is conservative, so
a fresh book takes ~2 years to assemble. Early admission is **doubly hard**: the *overlap floor* AND
the *conservative gate* both bite in the earliest window.

**Quantified costs (attach to every option):**
| Cost | Value | Source |
|---|---|---|
| Admission false-reject @26wk | **7.79%** | P4 |
| Admission false-reject @52wk | **1.14%** | P4 |
| False-admit of a true ρ=0.5 leg @52wk (pre-FIX-4) | **~8.5%** | P4 / PB-4 |
| … same, post-FIX-4 (shrinkage + CI) | **~1.3%** | FIX-4 P4-benchmark |
| FIX-4 Ruling-1 gate-conservatism false-reject @52wk | **~18.7%** | FIX-4 characterization |
| … decay of that cost | 4.0% @78wk · 2.7% @104wk · **0.0% @156wk** | FIX-4 characterization |

**Three options (operator decides):**
- **(a) OOS-vs-OOS pro-forma correlation** on the common historical window at admission, migrating to
  realized-vs-realized as live overlap accrues. Lets a leg admit before 52wk of *live* overlap using
  the OOS series it was validated on.
- **(b) Tiered admission:** reduced budget (e.g. half) at ≥26wk overlap, full at 52 — "admit small,
  grow as data firms." An allocation-based bridge rather than a binary gate.
- **(c) Accept the ~2yr timeline as-is.** Under-deployment is the honest state of an under-diversified
  book (doctrine §4 cold-start).

**U3 — literature-graded RECOMMENDED shape (a recommendation, NOT an adopted decision):** the **(a)+(b)
hybrid** — tiered admission on **shrunk OOS correlation** (composes directly with FIX-4's estimator:
run the shrinkage + block-bootstrap-CI gate on the OOS-vs-OOS series, admit at reduced budget from
≥26wk, full at 52). **Honest cost:** OOS correlations *understate crisis convergence* (Longin–Solnik:
correlations rise in the left tail), so a pro-forma-admitted leg can be more correlated in a crash than
its OOS ρ shows — **bounded** by the tiered budget (small initial exposure), the existing caps, the
convergence watch, and (prospectively) U6 tail-dependence.

**Ruling-1 fold-in (numbered cost):** the FIX-4 52wk gate-conservatism false-reject (~18.7%, decaying
to 0 by 156wk) is a *quantified* second defense that makes early admission hard. Option (b)'s **tiered
budget is the operator's allocation-based softening** of exactly that: admit the leg small, control the
downside via allocation while the correlation estimate firms — the two-defense structure (the gate
refuses to *fully* trust a thin estimate; the tiered budget lets a *little* capital flow anyway).

**OPERATOR SUB-DECISION to resolve (flagged, not resolved here):** the **0–26wk budget question** —
zero budget below 26wk overlap (hard floor) vs a capped fraction from week 0 (soft ramp). This trades
cold-start speed against exposure to an essentially unmeasured correlation.

---

## PART B — FIX-4 Ruling 2 pre-registration (registry `STANDING-TWOSIDEDGATE`; ACCEPTED)

**PAD gates two-sided on |ρ|, rejecting strong-negative correlation (ρ=−0.5 rejected 98.7%) as
basis/inverse risk — a DELIBERATE divergence from the lit review's one-sided U2.** Rationale (verbatim
operator intent): *"ρ alone cannot distinguish a genuine independent-source anti-correlated diversifier
(good) from a structural basis-risk trap (two expressions of one underlying, dangerous, can flip
together in the tail). When a good thing and a dangerous thing are indistinguishable by the available
measure, PAD rejects both."* **Quantified cost:** ~6pp of the 52wk false-reject is lower-tail rejection
(genuine anti-correlated diversifiers rejected as the price of catching basis traps).

**Future item (REGISTER, do NOT build):** if admitting genuine anti-correlated diversifiers ever
becomes needed, the fix is a **structural-vs-independent anti-correlation discriminator** (U6-adjacent —
tail dependence tells a co-crashing basis pair from a genuinely independent hedge), NOT reverting to
one-sided admission.

---

## PART C4 — U6 tail-dependence amendment (DRAFT; registry `STANDING-TAILDEPENDENCE`)

**What it is:** the SECOND correlation type — **lower-tail dependence λ_L**: how often two legs are in
their worst-q tail *together*. Invisible to Pearson (which averages crash days into calm days). It feeds
the EXISTING convergence watch a second, sharper input.

**Built now (dormant, decision-affecting-nothing):**
- **Estimator** `lower_tail_dependence` + **paired block-bootstrap CI** `lower_tail_dependence_ci`
  (reuses `src/core/bootstrap`, same discipline as FIX-4 — no i.i.d. closed form). Synthetic
  ground-truth tested (lower-tail pair detected; independent ≈ baseline; same-Pearson-different-tail
  distinguished).
- **Diagnostic logging** in `allocate()`'s log (`tail_dependence`: λ_L, CI width, sufficiency tier,
  joint-tail obs, stress episodes) alongside the correlation attribution. Flags/decides nothing.
- **Sufficiency check** `tail_dependence_sufficiency` — reports which tier the data currently supports.

**The three-tier sufficiency-gated staged auto-activation** (gate on CONFIDENCE in λ_L, **never its
value** — trusting a crash-detector's output to authorize trusting the crash-detector is circular, and
early λ is noise). Three components — trust λ_L only when ALL hold:
1. **joint-tail observation count** (raw sample behind λ_L);
2. **episode diversity** — distinct stress episodes (drawdown + ≥`MIN_EPISODE_SEPARATION_WEEKS`=4
   time-separation). Guards the 2008 failure: 25 obs from one crash is one piece of evidence repeated,
   not 25. **THE NON-NEGOTIABLE SLOW GUARD** — never tuned down for speed;
3. **CI tightness** (bootstrap CI width — catches technically-plentiful-but-messy data).

| Tier | Meaning | Operator-ruled thresholds (all TYPE-2 UNVALIDATED, replacement-triggered) | Wired? |
|---|---|---|---|
| 1 | DIAGNOSTIC (log only) | ≥25 joint-tail obs | **built + wired to logging** |
| 2 | CONVERGENCE-WATCH INPUT (may contribute to a WATCH flag, not capital) | ≥25 obs AND ≥2 episodes AND CI width < 0.25 | **spec'd + reported; NOT wired** |
| 3 | CAPITAL-MOVING (may demote/resize a leg) | ≥40 obs AND ≥3 episodes AND CI width < 0.20 | **spec'd + reported; NOT wired** |

**Stated plainly:** estimator + logging + sufficiency-check are built (dormant, decision-affecting-
nothing); the check REPORTS which tier the data supports, so the escalation becomes *available* when the
data honestly earns each tier — but **wiring Tier-2/Tier-3 to real WATCH/demotion/sizing is a SEPARATE,
operator-acked step, not done here**. Data discipline: **strategy-level REALIZED or clean-OOS returns
only, NEVER backtest-over-the-fitting-data** (that measures the fit, not the future — the VTD overfit
trap). **Cold-start is structurally excluded** by the sufficiency gate: a short window with zero real
stress events yields a noise λ, which the gate refuses (Tier 0).

**Replacement triggers (TYPE-2):** `TAIL_Q`=0.10, `MIN_EPISODE_SEPARATION_WEEKS`=4, and all tier
thresholds are conservative starting values — re-estimate from the realized joint-tail distribution once
the diagnostic log has accumulated real stress episodes on the actual legs.
