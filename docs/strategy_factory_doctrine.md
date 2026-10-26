# STRATEGY FACTORY DOCTRINE (SFD) — v1.2 (audit-hardened, FROZEN)
### mbappe project · 2026-07-08 · status: FROZEN 2026-07-11
### v1.1: Art. 1.4 replayability line · 2.1(b) structural surveillance ·
### 6.4 two residence clocks · F7 recent-returns mining
### v1.2 (hostile audit): 2.2(b) observation-born multiplicity · 4.5
### blind-until-registered · 5.5 provisional cluster tags · 8/F3 act-based
### trial definition · 8/F8 world-vs-signal parameters
Companion to PSD/VTD/LLD/PAD. Those doctrines govern how a single
candidate is parameterized, validated, lifecycled, and admitted. SFD
governs the FACTORY: how candidates are generated continually, in what
order they are processed, what runs unattended, and where humans are
constitutionally required. Amendment rule identical to all doctrines:
evidence-backed, PRE-REGISTERED before any result it could affect;
result-motivated amendment is forbidden.

---

## ARTICLE 1 — THE UNIT OF PRODUCTION
1.1 The factory produces verdicts on MECHANISM-HYPOTHESES, never on
    stock-strategy pairings. A hypothesis names (a) the counterparty
    and why they cannot/will not arbitrage the effect away, (b) the
    universe the mechanism itself implies, (c) a falsifiable claim,
    (d) a cluster tag, (e) an honest prior.
1.2 Universes are DERIVED, not discovered: either a locked instrument
    list (trend sleeve, calendar instruments), an event-nominated set
    (filings, offerings, announcements), or a rule-selected daily set
    (Phase-0 in-play screen). "Scan instruments for whatever signal
    fits" is not a generation channel; it is the L0 graveyard's
    production process.
1.3 Instrument-dependence test: a signal that differs across
    structurally-equivalent instruments (e.g. two correlated index ETFs) without a
    mechanism-level reason is presumptively noise. A signal that
    differs across a structural boundary the mechanism predicts
    (capacity-constrained vs liquid) is a universe definition.
1.4 THE REPLAYABILITY LINE (governing principle for all intelligence
    in the factory): non-determinism is permitted exactly as far
    upstream as it stays out of the replayable path. An idea's ORIGIN
    may be non-deterministic (LLM synthesis, human hunch, agent
    intake) because origins are never re-executed. Everything from
    the registry entry downward — selection rules, entries, exits,
    sizing — must replay bit-identically over history, because
    replayability is what "validated" means. Corollary: any live,
    unfrozen model or agent at the decision boundary renders a
    strategy unvalidatable BY CONSTRUCTION (it cannot enter the
    gates), which is a stronger exclusion than "risky." The sole
    sanctioned intelligence inside the replayable path is a FROZEN,
    versioned, hash-pinned artifact (classifier/ranker) whose exact
    decisions replay, promoted and retrained only as pre-registered
    events under LLD. This line is falsifiable, not dogma: an
    LLM-derived component that meets the freeze standard (pinned
    prompts, model version, retrieval snapshots, seeds — full
    decision-set reproducibility) enters the gates like any other
    candidate. The bar never moves; anything that can clear it may
    pass.

## ARTICLE 2 — GENERATION CHANNELS (unlimited volume, four doors)
2.1 LITERATURE & SOURCE INTAKE (Family-9 rule): any paper, post,
    video, or practitioner claim → extract the codifiable mechanic →
    registry ID, evidence grade (A academic w/ replication .. D
    anecdote), cluster tag, prior. Claimed win-rates inadmissible.
    Published edges modeled at ≤50% of published Sharpe, pre-cost.
    (b) STRUCTURAL-CHANGE SURVEILLANCE: continuous monitoring of
    market-structure CONDITIONS — new instrument classes (ETF/ETP
    launches), regulatory changes, venue/mechanism changes (auction
    rules, tick regimes, 0DTE-class growth), borrow-cost regimes,
    participation shifts — as an intake channel. Structural facts
    are preconditions, not outcomes: observing them costs zero
    ledger N. Output is a drafted registry entry ("mechanism X's
    precondition now holds / has appeared in venue Y") for human
    confirmation. The complementary prohibition is F7: surveillance
    watches CONDITIONS, never mines recent RETURNS for "what is
    working now."
2.2 DIAGNOSTICS → QUARANTINE (VTD channel a): anomalies observed in
    experiment diagnostics become quarantined hypotheses carrying
    sibling multiplicity. Never promoted in the run that birthed them.
    (b) OBSERVATION-BORN HYPOTHESES: a hypothesis whose origin is
    looking at project data — dashboards, reports, charts of
    collected outcomes — is diagnostics-born regardless of who did
    the looking. It enters under the same quarantine-style
    multiplicity treatment, never as literature-grade intake.
    Dressing an observed pattern as an independent idea is the
    laundering route this clause closes.
2.3 DATA-DRIVEN MINING: Phase-0 feature mining and (post-threshold)
    ML extractors (H34/H35) generate candidate features/rankings ON
    validated-edge data. Extractors are never edge sources (H50
    doctrine). Architecture search is ledger-charged.
2.4 VARIATION & COMBINATION: variations within existing families are
    free to test; charged at the FAMILY level so cumulative N deflates
    marginal passes automatically. Exact-config repeats are the only
    forbidden waste: lookup-before-test against the registry is
    mandatory and surfaces prior verdicts.
2.5 New external sources of hypotheses (agents, scrapers, feeds) plug
    into 2.1 only. A generator may know WHAT has been tried (dedup);
    it must never be trained or selected on WHAT PASSED (generator
    overfitting to the gate battery is meta-overfitting one level up
    from where the ledger looks). Fresh external information licenses
    generation; gate outcomes do not.

## ARTICLE 3 — SEQUENCING (how the queue orders itself)
3.1 Rule: INFORMATION-PER-DOLLAR-PER-DAY. When a build slot or risk
    slot opens, take the registry candidate with the highest expected
    verdict-yield soonest for the least spend, whose dependencies
    (data, audits, triggers) are met. Sequencing is queue discipline,
    not judgment at trade time.
3.2 Three permanent lanes run in parallel:
    - FAST LANE: cache-based tests, verdicts in days, ~$0.
    - HARNESS LANE: forward-data-clocked hypotheses (Phase-0 → ORB →
      extractors); the clock is calendar sessions and cannot be
      compressed by compute.
    - BUILD LANE: ONE medium/large project at a time.
3.3 Spend is Sharpe drag. Data purchases are demand-triggered by a
    named hypothesis at a named gate, registered in INFRA_LEDGER,
    never speculative.
3.4 Prior negative results raise a family's bar; they never close a
    door (except L0 categories, reopened only by new structural
    information).

## ARTICLE 4 — AUTOMATION BOUNDARIES (the constitution)
4.1 AUTOMATED WITHOUT APPROVAL (the ~80% by volume):
    a. Data collection: Phase-0 daily capture, soak, Stage-5
       reconciliation accumulation, feed/quality monitors.
    b. Execution of PRE-REGISTERED experiments as queued jobs (MCPT
       triage batches, PSD sweeps, VTD batteries) on the research
       machine, every trial auto-charged to the ledger.
    c. Lifecycle monitoring: demotion monitors, decay checks,
       scheduled re-validation; a demotion auto-opens a slot and
       surfaces the next queue candidate.
    d. Candidate generation per Article 2, including automated
       source intake and pre-grading FOR HUMAN CONFIRMATION.
    e. Operator reporting, backups, telemetry.
4.2 AUTOMATION MUST NEVER: lower or reinterpret gates; self-approve
    promotions; run any test outside the ledger; modify configs,
    doctrine, or thresholds; close any loop from live PnL back into
    parameters (weights included — model weights are parameters); or
    touch the live order path.
4.3 THE TWO HUMAN JOINTS (constitutionally non-automatable):
    a. PRE-REGISTRATION — locking hypothesis, criteria, and grid
       before any run they govern. Minutes per experiment. This is
       where honesty enters the system.
    b. PROMOTION CONFIRMATION — gates decide, the operator confirms
       preconditions (merges landed, standing items closed) and
       executes the LLD promotion. This is where accountability
       enters the system.
    Removing either joint is how the system dies, not how it runs
    itself. "Autonomous" means everything else.
4.4 Any external agent (e.g. a fenced intake agent) operates under a
    WRITTEN fence spec authored BEFORE deployment: no test execution,
    no market-data+strategy-code co-access, no gate-outcome training,
    zero write access to configs/keys/production. The fence precedes
    the agent, always.
4.5 BLIND-UNTIL-REGISTERED (forward datasets): while a forward
    dataset is accumulating toward a pre-registered analysis (e.g.
    Phase-0 → Phase-1), routine reporting to the operator surfaces
    HEALTH AND COVERAGE ONLY — capture counts, field completeness,
    fidelity flags — never outcome statistics (return distributions,
    MFE/MAE summaries, continuation rates). Outcome statistics
    unblind only after that dataset's analysis criteria are locked.
    Rationale: human pre-registration contaminated by months of
    outcome-watching is fit-to-data wearing a lab coat; this clause
    is the human-side complement of F3.

## ARTICLE 5 — SLOTS, BUDGETS, GROWTH ORDER
5.1 Admission to the book happens ONLY through gates (VTD Stage-4 SPA
    + PAD admission + cluster budgets + blend-CI). Gates are the
    limit; there is no leg-count target.
5.2 Risk budgets are per-CLUSTER (PAD). Correlated hypotheses share
    one budget (e.g. SHORTVOL: H25/H46 + H26/H47 = one budget, never
    double-counted).
5.3 GROWTH ORDER (binding): (1) new decorrelated clusters first,
    (2) better legs replacing weaker legs within a cluster second,
    (3) capital expansion of capacity-constrained legs third.
    Breadth of independent drivers is the product; leg count is not.
5.4 Portfolio floor objective: ≥3 validated legs across distinct
    clusters with |ρ| < 0.3–0.4. Expected yield calibration: ~15
    hypotheses tested ⇒ ~1 false 95% pass; second-universe
    replication required before any leg touches paper.
5.5 Cluster tags are AUTHORED HYPOTHESES, provisional by nature.
    Empirical correlation measured at PAD admission (blend-CI)
    overrides the authored tag: two legs that measure correlated
    share one budget regardless of their labels. Tags route
    candidates; measurements allocate risk.

## ARTICLE 6 — THROUGHPUT DOCTRINE
6.1 The factory is DATA-BOUNDED, not compute-bounded. Fresh datasets
    (forward collection, new universes, new mechanisms, new event
    feeds) license new tests; recycling the same cache raises every
    survivor's bar via cumulative N. Compute purchases are evaluated
    against queue latency only, never against "more training = more
    edge" (false by construction under DSR).
6.2 Corollary: the highest-value recurring investment is anything
    that makes the Phase-0 collector richer (new causal fields,
    catalyst tags incl. newsletter_mention / social_velocity) —
    schema additions ship EARLY because forward data cannot be
    collected retroactively.
6.3 The holdout is one-shot per leg. Regime/period slices are
    diagnostics, never post-hoc gates.
6.4 TWO RESIDENCE CLOCKS. "Where a mechanism lives" changes on two
    clocks, handled by different machinery — never conflated:
    (a) INSTRUMENT-LEVEL (daily): which names host the mechanism
    today is answered by deterministic screeners/event feeds
    re-running FROZEN rules against fresh data (Phase-0 pre-open,
    EDGAR feeds). The rule is fixed; the world moves; the screener
    tracks the world. Already continuous by design.
    (b) MECHANISM-LEVEL (secular, years): structural migration of
    an edge (decay post-publication, capacity crowding, regime
    death) is handled by LLD demotion monitors on live legs,
    scheduled re-validation, and quarterly doctrine review — plus
    2.1(b) surveillance for newly-created residences. A 24/7
    scanner re-checking secular facts adds noise, not information.

## ARTICLE 7 — OPERATOR CADENCE (the human loop, sized honestly)
7.1 DAILY (~minutes, automatable to a report): Phase-0 captured>0
    check; alert review.
7.2 WEEKLY: review auto-generated candidate intake, confirm/deny
    registry entries; launch pre-registered queues for open slots.
7.3 MONTHLY: ledger review (family N, haircut trajectories);
    lifecycle review (decay monitors); INFRA_LEDGER spend audit.
7.4 QUARTERLY: doctrine review — pattern analysis across experiments;
    amendments proposed with evidence, pre-registered.

## ARTICLE 8 — FAILURE-MODE REGISTER (what kills factories)
Accepted residual risks, on the record (audited, no patch exists):
(i) the operator's own generator-gate coupling — the human knows
what passed and shapes intake; irreducible, mitigated by
family-level charging + the mechanism requirement; (ii) F6 is a
named temptation without an enforcement mechanism beyond F1's
amendment-deferral.

F1 META-OVERFITTING: changing rules in response to a leg you want to
   pass. Detection: any amendment proposed while a relevant result is
   pending is automatically deferred until after that verdict.
F2 GENERATOR-GATE COUPLING: candidate generators learning the gate
   battery's preferences (Art. 2.5). Detection: audit intake channels
   for gate-outcome inputs.
F3 MULTIPLICITY LEAK: tests executed outside the ledger. A TRIAL IS
   DEFINED BY THE ACT, NOT THE TOOL: any computation of
   strategy-performance statistics on market data — by a queue
   runner, a notebook, a spreadsheet, a Claude Code task
   "sanity-checking", a chat, an agent, or a human — is a trial and
   is ledger-charged or is an incident. Restricting research-DB
   access to ledger-integrated runners is the enforcement floor,
   not the definition: parquet caches and exports are equally in
   scope.
F4 EVIDENCE NON-PORTABILITY BLINDNESS: infrastructure swaps (broker,
   feed) that silently reset empirical calibrations. Rule: every swap
   proposal must state which calibration clocks it resets.
F5 TOOL CHURN: adopting platforms as displacement activity for
   verdict production. Rule: every adoption names the registry
   hypothesis it unblocks (see TOOLING_DECISION_RECORD).
F6 DEADLINE PRESSURE ON STATISTICS: "we need a leg by X" is not an
   input any gate accepts. The engine's survival property is that
   gates outrank wants — including the operator's.
F7 RECENT-RETURNS MINING: any automated proposer that scans recent
   price/return data for "gaps" or "what's working now" is, by
   construction, fitting the most autocorrelated, least
   out-of-sample slice of data available — it manufactures the
   candidates MOST likely to be regime noise, then spends family
   ledger N disproving them. Negative expected value per candidate.
   Condition surveillance (2.1(b)) is legal; outcome mining is not.
   Boundary clarification: ledger-charged, pre-registered analysis
   of data COLLECTED FOR THAT PURPOSE (Phase-0 feature mining under
   2.3) is not F7 — F7 targets unledgered proposers on market-wide
   outcomes. Detection: audit every intake channel's inputs for
   price/return series.
F8 PARAMETER-CLASS CONFUSION AT RECALIBRATION: live broker data
   feeding back into parameters is EITHER measurement or the
   forbidden loop, depending on parameter class.
   WORLD-PARAMETERS (spread capture, slippage, fill rates, borrow
   drag — properties of the market): calibrating these from broker
   truth via pre-registered recalibration events (Stage-5) is
   measurement, sanctioned, and typically makes gates HARDER.
   SIGNAL-PARAMETERS (thresholds, lookbacks, weights, model
   coefficients — the strategy's opinions): live data never touches
   these outside a full pre-registered re-validation. The Stage-5
   recalibration is CONDITION-GATED, not date-gated: it occurs when
   >=20 trading days of live_fill_costs rows exist in the
   reconciliation store — whenever that is. Any proposal blurring
   world-parameters and signal-parameters at recalibration remains
   F8 regardless of when the condition is met.

## ARTICLE 9 — RELATION TO EXISTING DOCTRINE
SFD adds no gate and relaxes no gate. Where SFD and PSD/VTD/LLD/PAD
could be read to conflict, the stricter reading governs. The pipeline
(registry → MCPT → build/parity → PSD → VTD decade battery → PBO →
LLD paper → soak → Stage-4 SPA + PAD admission → ACTIVE) is unchanged
and owned by its respective doctrines; SFD only governs what feeds it,
in what order, and unattended-vs-human boundaries.

--- END SFD v1.2 — FROZEN 2026-07-11 ---
