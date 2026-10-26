# Order management (OMS) — architecture as built

**Scope.** How this system takes an order intent, gets it to a broker, tracks what happened to it,
and keeps its own view of positions honest against the broker's. Order-handling mechanics only — no
strategy logic, signals, parameters or performance figures appear here.

**Status of this document.** Descriptive, not aspirational. It documents the order path *as wired
today*, including two places where the design on disk is better than the design in use. Line
references are to `main` at the time of writing and will drift; the file and symbol names are the
durable part.

---

## 1. The headline: order management is implemented more than once

There is no single component named "the OMS". Order management is **distributed across four
independent order paths**, three of which re-implement lifecycle, position tracking and
reconciliation from scratch:

| Path | Submits via | Purpose |
|---|---|---|
| **Live engine** | `IdempotentExecutionAdapter` → `AlpacaBroker` (`src/execution/idempotent_execution_adapter.py:41`) | the multi-leg trading engine |
| **DDR-F1** | `AlpacaPaperBroker.submit_moo` (`src/research/ddr_f1/broker_alpaca.py:93`) | a single daily-swing research strategy |
| **Cash-out forward** | `PaperBroker.place_paper_buy` (`src/research/cashout_forward/broker.py:54`) | an event-driven research arm |
| **DDR execution arms** | three submitters (`src/research/ddr_arms/submitters.py:103,124,147`) | execution-variant *apparatus*, not a strategy |

Each carries its own client-order-id scheme, its own position model, and its own idea of what
"terminal" means for an order.

And separately, there **is** a clean, broker-agnostic OMS core —
`src/execution/idempotent_execution.py` — with an order model, a position tracker, an idempotent
submitter and a boot reconciler. **Production imports two of its fourteen public names.** The rest
is written, unit-tested, and unused.

That combination — capability re-invented per subsystem while a unified implementation sits
partially unwired — is the central architectural fact about order handling here. It is recorded as
finding **F1** in [`docs/architecture_review.md`](../architecture_review.md) and is understood, not
accidental: the core was deliberately left unwired to avoid touching a live order path (see §6).

---

## 2. The five responsibilities

### 2.1 Order lifecycle / state machine — **AD-HOC**

**There is no explicit state machine anywhere.** No state enum, no transition table, no guarded
transitions. Order state is inferred at each call site by testing a broker-reported status string
against a locally-defined set — and **the sets disagree**:

| Defined in | Treated as terminal |
|---|---|
| `src/execution/idempotent_execution.py:57` `TERMINAL_STATUSES` | `filled`, `canceled`, `cancelled`, `expired`, `rejected` |
| `src/engine/broker_fill_sieve.py:26` `TERMINAL_ORDER_STATUSES` | `filled`, **`partially_filled`**, `canceled`, `cancelled`, `expired`, `rejected` |
| `src/research/ddr_f1/reconcile.py:24` `TERMINAL` | `filled`, `expired`, `canceled`, `cancelled`, **`done_for_day`**, `rejected`, **`replaced`** |
| cash-out | *no order-state concept at all* |

Two concrete divergences:

- **`partially_filled` is terminal in the fill sieve and non-terminal in the OMS core.** The core
  additionally defines `FILLABLE_STATUSES = {filled, partially_filled}`
  (`idempotent_execution.py:58`), i.e. it models a partial as *still live with a remainder*, while
  the sieve models it as *done*. Both readings are defensible; holding both simultaneously is the
  problem.
- **`done_for_day` and `replaced` are recognised only by DDR.** The other paths would classify
  either as non-terminal and wait for a resolution that never comes.

The closest thing to a real order model is `BrokerOrder`
(`idempotent_execution.py:90`), which carries `status`, `filled_qty`, `filled_avg_price` and derives
`is_terminal` and `remainder`. It is **not used in production.**

Where lifecycle *is* handled well, it is handled locally: the live engine's `PendingFillBundle`
(`broker_fill_sieve.py:62`) tracks a multi-slice order across `requested_qty` / `filled_qty` /
`is_terminal` / `needs_tracking`, and DDR models the pre-open submission honestly by returning a
deferred marker rather than inventing a fill (`broker_alpaca.py:93`, `deferred = True` at
`broker_alpaca.py:90`).

### 2.2 Position tracking — **AD-HOC (three parallel models)**

| Path | Model |
|---|---|
| Live engine | `models.Position` (`src/models.py:82`) is a **broker snapshot dataclass**, not a tracked book. Durable per-leg state is "leg memory", keyed on `bars_in_trade` (`src/engine/execution_adaptor.py:724-787`) |
| DDR-F1 | its own `Position` dataclass (`src/research/ddr_f1/harness.py:186`) plus an `open_positions` dict, rebuilt each session from broker fills by `net_ddr_holdings` (`src/research/ddr_f1/reconcile.py:80`) |
| Cash-out | `cf_positions` SQLite rows (`src/research/cashout_forward/store.py:24`); no in-memory model |
| DDR arms | `ArmBooks` / `Book` (`src/research/ddr_arms/books.py:58,108`), deliberately **two** parallel books per arm |

A unified model exists and is unused: `TrackedPosition` / `PositionTracker`
(`idempotent_execution.py:208,214`), whose `apply(order, fill_price)` folds a `BrokerOrder` into a
running position. **Zero production references.**

### 2.3 Reconciliation against the broker — **ROBUST (the strongest area)**

This is the part the architecture takes most seriously, and it is implemented three times *well*.

**Boot reconciliation** — `reconcile_boot` (`idempotent_execution.py:286`), wired into production at
`idempotent_execution_adapter.py:112`. Two properties worth naming:

- It **diffs** internal positions against broker positions plus open-order client-order-ids; on any
  mismatch it trips the block-new-entries safe state (`RiskEscalationLevel.ENTRY_GATE_HALT`) and
  emits a structured mismatch report (`ReconcileMismatch` / `ReconcileReport`, `:268,:277`).
- It **never silently adopts broker state.** A disagreement halts entries rather than overwriting
  the internal view. "No silent adoption" is stated as a design invariant in the module docstring
  and is the correct default: an unexplained divergence is a bug to surface, not a number to copy.

**Per-leg drift detection, live engine** — `src/engine/execution_adaptor.py`:
`reconcile_broker_primary_fill` (`:724`) compares requested qty against the fill result and the
broker position; `detect_leg_broker_memory_drift` (`:765`) flags a leg believing it is in a position
the broker does not show; `apply_broker_authority_to_leg_memory` (`:777`) resolves it with the
broker as authority.

**DDR's session reconciliation** — `Harness.reconcile_and_reconstruct`
(`src/research/ddr_f1/harness.py:664`) with the pure functions in `reconcile.py`. Its stated rule is
"Alpaca is the source of truth": each morning it reads its own tagged orders back from the broker,
classifies each terminal outcome (`reconciliation`, `:62`), and **rebuilds** open positions from net
filled holdings (`net_ddr_holdings`, `:80`) rather than trusting yesterday's memory. This exists
because the fresh-process-per-day model has no durable memory to trust — reconstruction *is* the
position store.

**Cash-out has no reconciliation.** It records a broker order id and later reads a fill price
(`broker.py:64`); nothing compares its `cf_positions` rows against broker state.

### 2.4 Idempotency / dedupe — **ROBUST by design, PATCHED-IN in practice**

The systematic mechanism is good and is **wired**:

- `deterministic_client_order_id` (`idempotent_execution.py:61`) =
  `sha256(leg_id | signal_ts | side | epoch)`, truncated to the broker's id limit. Same intent →
  same id, so a retry or a post-restart re-submit is a no-op rather than a duplicate.
- The id is **reserved before the network send**, which is what makes a mid-order interruption safe:
  restart plus reconcile finds the order by its id and adopts it exactly once.
- Query-before-submit plus per-id locking in `IdempotentSubmitter` (`:128`); the production adapter
  implements the same idea more narrowly with an in-process `_seen` set and a broker lookup
  (`idempotent_execution_adapter.py:49,99`).

The non-systematic part:

- **DDR uses a different scheme** — `DDRF1-<session>-<name>` (`broker_alpaca.py:79`), a
  human-readable tag chosen so its fills stay separable in an activity stream shared with the live
  engine. The DDR arms extend this to three prefixes (`ddr_arms/constants.py:132`). These are
  *deliberately* separate namespaces, but they are not the hash-based scheme, and nothing enforces
  a common format.
- **DDR's exit-path dedupe was added reactively.** The entry path had a per-order guard; the exit
  path did not, so a second exit submitted while a prior exit was still open was rejected by the
  broker and the uncaught exception aborted the whole session. The fix added a pre-submit check for
  symbols whose shares are already held by an open order (`pending_exit_symbols`,
  `broker_alpaca.py:131`) plus a matching guard on the exit submit. The asymmetry — a guard on one
  submit path and not its mirror — is the signature of per-site patching rather than a boundary
  layer that makes the guarantee once.
- **Cash-out has no idempotency**: a plain market submit with no client-order-id and no dedupe.

### 2.5 Fill handling, including partials — **ROBUST (engine) / AD-HOC (DDR) / MISSING (cash-out)**

- **Live engine — robust.** `src/engine/broker_fill_sieve.py` is real machinery:
  `ChildFillSlice` (`:53`) and `PendingFillBundle` (`:62`) model an order as a set of slices with
  `requested_qty`, `filled_qty`, `is_terminal` and `needs_tracking`; `BrokerFillReconciliationSieve`
  (`:140`) polls asynchronously after submission and tracks bundles across cycles, with
  `INCOMPLETE_CYCLE_CANCEL_THRESHOLD` (`:25`) bounding how long a bundle may stay unresolved.
- **DDR — ad-hoc but correct.** Partials are handled by comparing `filled_qty` against the intended
  qty inline during reconciliation, with `OrderRec.taken` (`reconcile.py:48`) distinguishing "put
  shares on the book" from "terminal but zero-fill". Whole-share flooring for auction orders and the
  per-order submit guard were both added after live breakage rather than designed in.
- **DDR arms — explicit and unusually careful.** `fills.py:85` `complete_fill` splits one intent
  into a real portion and a modelled remainder, every row tagged real or simulated
  (`FillRecord`, `fills.py:28`), and `assert_books_separable` (`books.py:170`) enforces that the two
  never merge. This is apparatus for an execution study, but it is the most explicit fill accounting
  in the repo.
- **Cash-out — missing.** `get_fill_price(order_id)` (`broker.py:64`) reads an average fill price.
  No partial handling, no remainder, no outstanding-quantity concept.

---

## 3. Summary table

| # | Responsibility | Rating | Strongest implementation |
|---|---|---|---|
| 1 | Lifecycle / state machine | **AD-HOC** | none — three disagreeing status vocabularies, no transitions |
| 2 | Position tracking | **AD-HOC** | `PositionTracker` exists but is unwired |
| 3 | Reconciliation vs broker | **ROBUST** | `reconcile_boot` + per-leg drift + DDR reconstruction |
| 4 | Idempotency / dedupe | **ROBUST by design** | `deterministic_client_order_id` (wired) |
| 5 | Fill handling / partials | **ROBUST** (engine only) | `broker_fill_sieve.py` |

---

## 4. The wired-vs-unwired split

`src/execution/idempotent_execution.py` is broker-agnostic by construction: everything in it runs
against a small `ExecutionBroker` protocol (`:109`), so it is exercised entirely with a mock and
never submits a live order. Its own docstring records the consequence — the live `AlpacaBroker`
*would* satisfy that protocol, but wiring it was left as an "integration point … live path left
untouched".

| Public name | In production? |
|---|---|
| `deterministic_client_order_id` (`:61`) | **yes** — `idempotent_execution_adapter.py:68` |
| `reconcile_boot` (`:286`) | **yes** — `idempotent_execution_adapter.py:112` |
| `IdempotentSubmitter` (`:128`) | no — referenced only in comments (`orchestrator.py:1163`, `entry_authority.py:9`) |
| `PositionTracker`, `TrackedPosition` (`:214,:208`) | no |
| `OrderIntent`, `BrokerOrder`, `ExecutionBroker`, `SubmitResult` (`:76,:90,:109,:123`) | no |
| `ReconcileMismatch`, `ReconcileReport` (`:268,:277`) | no (produced internally by `reconcile_boot`) |
| `TERMINAL_STATUSES`, `FILLABLE_STATUSES` (`:57,:58`) | no — each path defines its own |

So the two guarantees that generalise cheaply — deterministic ids and a halt-on-mismatch boot
diff — are live. The parts that would have *replaced* per-subsystem code (submitter, order model,
position model, status vocabulary) are the parts still on the shelf. That is the honest shape of it,
and it explains §1: the reinventions exist because the thing that would have prevented them was
never adopted at the boundary.

---

## 5. Reconciliation: the design, and its known limitation

**The design.** Broker state is authoritative for *what exists*; internal state is authoritative for
*what was intended*; a disagreement is escalated rather than silently resolved. `reconcile_boot`
halts new entries on mismatch; DDR rebuilds from broker fills because it has no durable memory;
the live engine resolves per-leg drift with the broker as authority.

**The limitation — identified, specified, and not yet built.** Reconciliation compares **positions**
(plus open-order ids). It does not compare local order/trade records against the broker's
*order-level* history. Any record-level error that nets to **zero position change** is therefore
structurally invisible to it: a fill record written with zero quantity, or a fabricated buy and sell
that cancel out, produce no position delta and so no mismatch.

This is not a hypothetical. It is why a batch of zero-quantity trade records persisted undetected
until found by direct inspection rather than by reconciliation.

**Path forward.** The gap is specified in
the internal spec `docs/research/ORDER_LEVEL_RECONCILIATION_F6.md` as finding **F6**
(that spec is repo-internal and not part of the public set, so it is referenced by name, not linked): extend reconciliation from position-level to order-level, diffing the local
`orders`/`trades` records against broker order history so a position-neutral record error is caught.
The containment in the meantime is that the position-level diff still catches anything that moves
exposure — which is the class that can lose money — while the invisible class is a bookkeeping
integrity problem. Stated plainly so the boundary of the guarantee is not overestimated.

---

## 6. Future direction

The improvement path is already identified rather than newly discovered here.

**F1 — adopt the unified core at the boundary.** `docs/architecture_review.md` records the
two-execution-paths finding as a P0. It is now **partially closed**: idempotency and boot
reconciliation are wired; the submitter, the unified position model and the order/broker abstraction
are not. Completing it would mean:

1. **One explicit state machine** — states plus legal transitions, replacing three string-set
   membership tests and resolving the `partially_filled` contradiction by construction rather than
   by convention.
2. **One position ledger** — adopt `PositionTracker` in place of leg memory, DDR's `open_positions`
   and `cf_positions` as three separate truths.
3. **Idempotency at the boundary, not per call site** — so an exit path cannot lack a guard its
   entry path has, and a new strategy inherits the property instead of re-deriving it.
4. **One broker contract** — `ExecutionBroker` (`:109`) already specifies it; the four order paths
   currently satisfy no common interface.
5. **Order-level reconciliation** — F6 above.

**What the current design does get right, and a rewrite should preserve:** no silent adoption of
broker state; deterministic ids reserved before the network send; reconstruction-from-broker where
there is no durable memory to trust; and asynchronous post-submit fill tracking rather than assuming
a submit is a fill.

---

## 7. File map

| Concern | File |
|---|---|
| OMS core (mostly unwired) | `src/execution/idempotent_execution.py` |
| Production idempotency + boot reconcile | `src/execution/idempotent_execution_adapter.py` |
| Live broker | `src/execution/alpaca.py` |
| Fill tracking, partials | `src/engine/broker_fill_sieve.py` |
| Per-leg broker-authority reconciliation | `src/engine/execution_adaptor.py` |
| Broker snapshot types | `src/models.py` |
| DDR order path + reconciliation | `src/research/ddr_f1/broker_alpaca.py`, `reconcile.py`, `harness.py` |
| DDR execution-variant apparatus | `src/research/ddr_arms/submitters.py`, `fills.py`, `books.py` |
| Cash-out order path | `src/research/cashout_forward/broker.py`, `store.py` |
| Architecture findings incl. F1 | `docs/architecture_review.md` |
| Order-level reconciliation spec (F6) | `docs/research/ORDER_LEVEL_RECONCILIATION_F6.md` *(repo-internal, not public)* |
