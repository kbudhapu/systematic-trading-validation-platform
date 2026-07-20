# PAD Sizing Seam Spec — wiring cluster_brain into live sizing at first promotion

**Status:** SPEC ONLY (PAD audit P5). Unbuilt. No behavior change. This is the design so that
promotion day is an implementation, not a design meeting. Every multiplier below was verified
against source at audit time (`risk_manager.py`, `portfolio_risk_governor.py`,
`portfolio_coordinator.py`, `orchestrator.py`). Adopting it, and especially **retiring** any live
multiplier in favor of PAD, is a separate pre-registered fix-queue item with its own before/after
behavioral test — not a stroke-of-the-pen deletion here.

## 1. Where the PAD leg budget enters ATR sizing
Live quantity sizing (`risk_manager.position_size`, `:617`; audited A3.2):
```
by_risk  = equity × max_risk_per_trade_pct × risk_budget_fraction / (atr × stop_multiplier)
shares   = min(by_risk, by_buying_power, by_concentration)   # by_concentration uses the cap stack (§2)
```
Today `risk_budget_fraction = (1/n_enabled) × portfolio_risk_governor.sizing_multipliers[leg]`
(`orchestrator.py:4335`, `:505-514`). **PAD wires in here:** replace `1/n_enabled` with the PAD
per-leg weight `AllocationResult.weights[leg]` (cluster-hierarchical, capped, cash-aware).
The governor clamps and the cap stack remain downstream (they compose, they are not replaced).

**Worked example:** equity $100k, `max_risk_per_trade_pct` 0.5%, PAD `weights[leg]=0.18`, ATR $2.00,
`stop_multiplier` 2.0 → `by_risk = 100000 × 0.005 × 0.18 / (2.00 × 2.0) = $90 / $4 = 22.5 shares`
of risk-budgeted size (before the min() against buying-power and the §2 concentration cap).

## 2. Composition order of ALL multipliers (total-ordered, single-authority target)
The live path stacks the following **across three functions with no single owner** — the seam's job
is to make ONE function own this list. Ordered as applied; each tagged **[Z]**=binary zero,
**[C]**=clamp (min against ceiling), **[M]**=compounding multiplier. Order matters where a zero, a
clamp, and a multiplier co-occur.

| # | Multiplier | Source | Type | PAD disposition |
|---|---|---|---|---|
| 0 | SAFE_MODE → 0.0 | `resolve_live_max_position_pct:1578` | **[Z]** | COMPOSE (LLD SAFE_MODE, freed→cash §3) |
| 1 | `base_cap` = `risk_config.max_position_pct` | `:1583` | base | COMPOSE |
| 2 | `× quality_multiplier` (`_quality_multiplier_from_score`) | `:1549,:385` | **[M]** | COMPOSE |
| 3 | `× parity_multiplier` = `relative_capacity_weight × active_count` | `:1546,:1550` | **[M]** | **RECONCILE** — parity capacity-weighting overlaps PAD's inverse-vol budgeting; decide replace-vs-compose |
| 4 | `× corr_multiplier` (`_correlation_shutter_multiplier`, 0.75 book-aggregate) | `:1551,:401` | **[M]** | **RETAIN — PAD gap** (book-aggregate scope absent from PAD; P1 #6) |
| 5 | `× portfolio_multiplier` (`PORTFOLIO_FACTOR_THROTTLE`=0.50 on net_beta/gross_imbalance) | `:1552,:1535,:68` | **[M]** | COMPOSE |
| 6 | clamp to `max_position_pct` | `:1554` | **[C]** | COMPOSE |
| 7 | `× vol_size_scalar` (`apply_vol_target_sizing`) | `:1602,:414` | **[M]** | **RECONCILE** — this IS the 12% vol-target scaling PAD's allocator omits (P2 obs); decide which layer owns vol-target |
| 8 | `× THIN_LIQUIDITY_PARTICIPATION_MULT` (if thin) | `:1606` | **[M]** | COMPOSE |
| — | **risk_budget_fraction** path (quantity, not cap): `1/n_enabled` | `orchestrator:4335` | **[M]** | **REPLACE** with PAD `weights[leg]` (§1) |
| 9 | governor correlation-clamp (`×0.6` when \|ρ\|≥`MAX_SAFE_CORRELATION`=0.85, raw closes ≥20 samples) | `portfolio_risk_governor.py:20-23` | **[C/M]** | COMPOSE (runtime clamp on active legs; P1 #3) |
| 10 | governor cold-start-clamp (`×0.6` when <20 bars) | `portfolio_risk_governor` | **[M]** | COMPOSE |
| 11 | governor drawdown exit-only (`STRATEGY_MAX_DRAWDOWN`=0.06 → exit_only) | `portfolio_risk_governor` | **[Z]** | COMPOSE |
| 12 | coord `resolve_portfolio_mode` leg-enablement (0.88/0.92) | `portfolio_coordinator:305` | **[Z]** leg on/off | COMPOSE (orthogonal; P1 #4) |
| 13 | coord `resolve_signal_conflicts` binary block (0.85 → `sizing_multiplier=0.0`) | `portfolio_coordinator:318` | **[Z]** | COMPOSE (P1 #5) |
| 14 | LLD `WATCH ×0.5` | LLD state (`_STATE_SIZING`) | **[M]** | COMPOSE (per doctrine §3: WATCH ×0.5 on top) |
| 15 | commissioning exemption | commissioning gate | override | COMPOSE (exempt legs bypass PAD entirely today) |
| 16 | breaker-adjacent (drawdown breaker `risk.max_drawdown_pct`) | governance/degradation | **[Z]** | COMPOSE |

**Ordering rule the single-authority function MUST encode:** apply all **[Z]** gates first (any zero
short-circuits to no-entry / flat — SAFE_MODE, exit-only, signal-conflict, leg-disable, breaker);
then compose all **[M]** multipliers; then apply **[C]** clamps last (a clamp after a zero is moot,
but a clamp before a multiply changes the result). A "single-authority composition function" is only
satisfied if its input list is this FULL set (#0–#16) — a subset is itself a FIX-BEFORE-PROMOTION
defect (per PB-1).

## 3. Mid-cycle events
- **Rebalance shrinks a leg's budget:** the **20% step cap governs** (`cluster_brain._apply_step_cap`);
  **no forced trimming of open positions** — the budget migrates over ≤20%/rebalance and new sizing
  reflects it; existing positions are not force-reduced (PAD §3, LLD §6.5). State explicitly in the
  impl: a budget cut does not emit sell orders; it lowers the ceiling for the next entry.
- **SAFE_MODE / WATCH frees budget → CASH, never siblings:** enforced in `cluster_brain.allocate`
  via per-leg state sizing (`_STATE_SIZING`: WATCH 0.5, SAFE_MODE 0) with the freed portion going to
  cash (`:152-160`), and `_cap_and_cash` sending capped residual to cash (never redistributed to a
  cluster sibling). The seam must route the LLD state into `LegInput.state` so this holds live.

## 4. Data dependency (tables/APIs) — with gaps
PAD needs, per leg: (a) **realized weekly return series** and (b) **registered OOS vol**, plus (c) a
**cluster tag**.
- (b) registered OOS vol — **EXISTS** (champion / hypothesis registry, research_vault).
- (a) realized WEEKLY returns per leg — **GAP**: per-trade PnL exists (`live_attribution_ledger`,
  `daily_pnl`), but there is **no stored per-leg weekly return series**; it must be aggregated
  (daily→weekly, per strategy_id) and persisted. Name the new store in the fix-queue item.
- (c) cluster tag — **GAP**: no `strategy_id → cluster` resolver exists in `src/` (P1.3); doctrine §7
  puts membership in the hypothesis registry — the resolver must be built.

## 5. Failure modes (fail-safe spec)
- **Allocator raises / returns empty:** sizing MUST fall back to the **last-good persisted
  `AllocationResult`** (add a durable last-good store); if none exists, fall back to the current live
  `allocate_risk_budgets` output (the incumbent path stays as the floor during migration).
- **Never emit an unsized order:** a PAD failure resolves to **no-entry (cash)**, never to an
  unconstrained or full-size order. Distinguish a *deliberate* zero (SAFE_MODE/exit-only) from an
  *error* zero (allocator crash) in telemetry — both block entry, but the error zero pages.
- **Floor at minimum:** an entry that survives all gates but computes a sub-viable size is dropped
  to no-entry, not rounded up (no min-notional inflation of an intended-tiny position).
