# PORTFOLIO ASSEMBLY DOCTRINE (PAD v1.0)
### mbappe project · frozen methodology · companion to PSD/VTD/LLD

**Status:** PRE-REGISTERED AND FROZEN at commit. Same amendment protocol as the other doctrines. PAD implements the locked architecture decision: **risk-blend, not switch** — multiple decorrelated legs allocated by risk budget across return-driver clusters. Flat allocation over correlated legs is prohibited as actively dangerous, not merely suboptimal.

---

> ## Implementation status — read this first
>
> This document specifies the Portfolio Assembly Doctrine as **designed**. It is not a description
> of what the live system currently does, and the difference is deliberate.
>
> The system is in **single-leg commissioning**: one strategy, live. With a single leg, hierarchical
> cross-cluster allocation is mathematically degenerate — there is nothing to allocate across — so
> the live allocator runs a **static equal commissioning split**, and the correlation-aware
> machinery specified below is present in code but **dormant**: the §2 correlation admission gate,
> the §3 two-level cluster budgeting, and the §4 convergence watch are implemented, tested, and not
> wired to capital.
>
> Two deltas are tracked against this document and gate promotion out of commissioning:
>
> | id | delta |
> |---|---|
> | **PB-1** | Allocation authority is split: the doctrine describes one correlation-aware authority; the live path uses a static split with `cluster_brain` dormant. |
> | **PB-2a** | `vol_target_annual` is declared in §7 but not consumed by the live `allocate()` path. Registered and accepted for the commissioning phase. |
>
> Both are *not-yet-built*, not *decided-against*. This doctrine is the target the system is being
> commissioned toward, and the reconciliation record tracking these deltas is maintained internally.
>
> We publish this note because a doctrine document that silently describes unbuilt behaviour is
> worse than no document. If the specification and the running system disagree, the disagreement
> should be the first thing a reader sees.

---

## 0. PRINCIPLES

**P-A — The unit of diversification is the return driver, not the ticker.** Two legs on different instruments sharing one driver (e.g., two short-vol expressions) are ONE bet. Clusters are assigned from the hypothesis registry's driver tags at leg registration, by mechanism, before any correlation is measured — correlation confirms clustering, it does not define it.

**P-B — The portfolio is where the capital-tier CI is passed.** No single retail-feasible leg clears the capital bar alone; the blend does (reference math: 3 legs at Sharpe ≈ 0.8, pairwise ρ ≈ 0.2 → blend ≈ 1.17). Therefore admission quality (decorrelation) is a first-class gate, equal in rank to leg validation.

**P-C — Everything the allocator consumes must be causal.** Correlations, vols, and budgets are computed from realized returns available at allocation time. No full-sample estimates anywhere in the live path.

## 1. CLUSTERS

Registered driver clusters (the concrete registered set is omitted from this public copy; each cluster names a distinct, decorrelated economic return driver). New clusters require a registry amendment naming the distinct economic driver.

## 2. ADMISSION GATE (per leg, at promotion to ACTIVE)

1. |ρ| < 0.35 (registered default within the decided 0.3–0.4 band) between the candidate's OOS weekly returns and EVERY currently-active leg's realized weekly returns, computed on the overlapping window, minimum 52 overlapping weeks (shorter overlap → admission deferred, not waived).
2. Cluster budget available (§3 caps).
3. Blend-level check: adding the leg must not degrade the book's bootstrap Sharpe CI lower bound (computed on the combined realized/OOS series). A leg that individually passes but degrades the blend waits.

## 3. HIERARCHICAL RISK BUDGETING

- **Level 1 — across clusters:** total portfolio risk budget (vol-target basis) split across ACTIVE clusters by inverse realized cluster vol (equal-risk across clusters), causal rolling 26-week estimate, monthly rebalance, 20% max step per rebalance (no allocation whiplash).
- **Level 2 — within cluster:** cluster budget shared across its legs by inverse leg vol, same causal window. Legs in one cluster are presumed redundant expressions of one driver: adding a second leg to a cluster does NOT increase the cluster's budget.
- **Sizing flow:** leg risk budget → position sizing through the existing ATR-based unit sizing (risk stays constant per the original design); LLD WATCH state applies its ×0.5 on top.
- **Caps:** single cluster ≤ 40% of total risk budget; single leg ≤ 25%; SHORTVOL cluster additionally capped at 15% with its registered crash-scenario margin reserve (registered tail scenarios).

## 4. INTERACTIONS & EDGE RULES

- **Freed budget from WATCH/SAFE_MODE legs is held in cash** until the leg resolves — never auto-redistributed intra-cluster (LLD §6.5: concentrating a decaying driver is the failure mode).
- **Correlation regime spikes:** if realized pairwise ρ between two ACTIVE legs' 26-week returns exceeds 0.6 for 4 consecutive weeks (drivers converging under stress), the junior leg (later admission) drops to WATCH sizing and a DiagnosticReport is filed — clusters are hypotheses too, and this is their falsification signal.
- **Cold start:** with only 1–2 ACTIVE legs, Level-1 allocation is trivially concentrated; the caps still bind (a single leg never exceeds its 25% risk cap of the FULL target book — the remainder stays in cash until the book earns diversification). Under-deployment is the intended behavior of an under-diversified book.

## 5. VALIDATION TIE-INS

PAD admission is LLD promotion gate material (LLD §2). The blend itself is a tested object: VTD Stage-4 SPA runs on the deployed set; the blend bootstrap CI is re-computed at every admission and every retirement. PortfolioBrain allocations are logged per rebalance with their input correlation/vol matrices (attribution must be reconstructable).

## 6. ADVERSARIAL AUDIT

1. **Correlation on short overlap lies.** 52-week minimum is binding; deferral is the answer to impatience.
2. **Inverse-vol rewards stale vols.** A leg whose vol collapsed because it stopped trading gets over-allocated. Mitigation: vol floor = leg's registered OOS vol × 0.5; exposure-weighted vol estimation (flat weeks don't shrink risk).
3. **Cluster tags can be wrong.** The 0.6-spike rule (§4) is the empirical check on the theoretical clustering; repeated spikes force a registry re-clustering amendment.
4. **Caps create silent cash drag** that flatters Sharpe and hides under-deployment. Mitigation: cash share is a first-class reported metric on every rebalance log.
5. **Monthly rebalance + quarterly refit resonance:** a refit that changes a leg's vol profile mid-cycle is handled by the 20% step cap, not by emergency rebalances; only LLD state changes trigger off-cycle reallocation.

## 7. CONFIG (additive; guardrails untouched)

```yaml
portfolio:
  doctrine_version: "1.0"
  vol_target_annual: 0.12
  admission: {max_abs_corr: 0.35, min_overlap_weeks: 52}
  budgeting: {cluster_vol_window_weeks: 26, rebalance: "monthly",
              max_step: 0.20, vol_floor_factor: 0.5}
  caps: {max_cluster: 0.40, max_leg: 0.25, shortvol_cluster: 0.15}
  convergence_watch: {corr_threshold: 0.6, consecutive_weeks: 4}
```

*Frozen at commit. Cluster membership lives in the hypothesis/leg registry; this file holds only the mechanics.*

## 8. PRE-REGISTERED AMENDMENTS (post-freeze; each keyed to a registry STANDING entry)

The mechanics in §1–7 are frozen at commit. Corrections and upgrades are appended here, each pre-registered in `EXPERIMENT_REGISTRY.md` before landing.

- **U5 — vol-targeting is TAIL CONTROL, not a Sharpe enhancer** (registry `STANDING-VOLTARGETTAILCONTROL`). The "vol-target basis" of the Level-1 risk budget (§3) sets a LEFT-TAIL / drawdown-control target, NOT a Sharpe optimum. Robust literature (Harvey et al. 2018; Cederburg et al. 2020) finds vol-targeting reduces left-tail severity but does not reliably raise Sharpe; apparent Sharpe gains elsewhere lean on a look-ahead volatility estimate (Liu et al.), which PAD structurally avoids via its CAUSAL rolling 26-week estimate. Consequence: `vol_target_annual` (§7, 0.12) is a **TYPE-3 operator-owned risk knob** (a chosen tail-risk tolerance, per MAGIC_NUMBERS_2026-07.md), not a derived optimum — turn it to change the book's tail posture, not to chase Sharpe. Distinct from **PB-2a** (that finding is about the parameter being unwired; U5 corrects the stated EXPECTATION).

- **U4 — ERC is a trigger-registered future upgrade** (registry `STANDING-ERCFUTURETRIGGER`). Equal-risk-contribution (correlation-aware) budgeting supersedes inverse-vol (§3) ONLY once ≥104wk realized overlap exists across ≥3 active legs. Until then inverse-vol (correlation-free) is the robust choice — ERC on short-window correlations concentrates on estimation error. Registered, built nothing.
