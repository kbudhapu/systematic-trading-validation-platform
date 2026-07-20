"""PortfolioBrain cluster layer (PAD v1.0).

Risk-blend, not switch: decorrelated legs allocated by risk budget across
return-driver CLUSTERS. Two-level hierarchical inverse-vol budgeting (Level 1
across clusters, Level 2 within a cluster), caps, an admission gate, a monthly
rebalance with a step cap, freed-budget-to-cash, and a correlation-convergence
watch. Everything the allocator consumes is CAUSAL (trailing realized returns).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from src.core.bootstrap import stationary_bootstrap_resample


@dataclass
class PortfolioConfig:
    # TYPE-3 operator-owned risk knob (U5 / MAGIC_NUMBERS_2026-07.md): a chosen LEFT-TAIL
    # control target, NOT a derived Sharpe optimum. Turn it to change the book's tail posture.
    vol_target_annual: float = 0.12
    # 0.35 = the field diversification "danger line" (~0.5, above which two legs stop meaningfully
    # diversifying) MINUS a measurement-noise buffer (~0.15, the 52wk correlation standard error).
    # PAD gates at 0.5 - 0.15 = 0.35 so a TRUE-0.5-correlated leg cannot sneak in by measuring below
    # 0.5 on noise. Stricter than the field's 0.5 because PAD's admission is AUTOMATED and TRUSTED
    # (no human re-check), but NOT permanent: admission is REVOCABLE via the convergence watch
    # (rising correlation -> WATCH -> demote -> freed budget to cash). FIX-4 replaced the point
    # estimate with a shrunk-CI gate; this anchor (danger-line-minus-noise) derives the 0.35 value.
    max_abs_corr: float = 0.35
    min_overlap_weeks: int = 52
    cluster_vol_window_weeks: int = 26
    max_step: float = 0.20
    vol_floor_factor: float = 0.5
    max_cluster: float = 0.40
    max_leg: float = 0.25
    shortvol_cluster_cap: float = 0.15
    convergence_corr_threshold: float = 0.6
    convergence_consecutive_weeks: int = 4
    # cluster_prior_rho: within-cluster shrinkage-target correlation. COLD-START
    # PLACEHOLDER, UNVALIDATED (TYPE-2, see docs/MAGIC_NUMBERS_2026-07.md). 0.4 chosen
    # conservative-leaning (>0 because same-cluster legs share a driver; ~at the 0.35
    # admission gate). The STANDARD Ledoit-Wolf constant-correlation target uses the
    # AVERAGE SAMPLE CORRELATION of the book, not a fixed prior -- but that needs a
    # populated cluster to average over, which cold-start lacks. REPLACEMENT TRIGGER
    # (registry STANDING-CLUSTERPRIORRHO): once a cluster has >=2 legs with real
    # overlapping history, shrink toward the MEASURED average within-cluster correlation
    # instead of this constant. Do NOT let 0.4 calcify -- it is a stand-in, registered
    # for replacement.
    cluster_prior_rho: float = 0.4
    # U3 cold-start tiered admission (registry STANDING-COLDSTARTBRIDGE). DORMANT: default OFF, so
    # admission keeps the strict 52wk floor (current behavior). When ON, a leg admits at REDUCED
    # budget from cold_start_tier_weeks and grows to full at min_overlap_weeks, budget shaped by
    # statistical significance ~ S*sqrt(t) (the derived "confidence accrues with sqrt(time)" rule:
    # confirming IR=1 at 2 SD takes ~4yr, so 52wk is already aggressive vs the field -> admit small,
    # grow as evidence firms). OPERATOR SUB-DECISION (0-26wk band): (a) ZERO budget below
    # cold_start_tier_weeks (current DEFER holds) -- IMPLEMENTED as the conservative default; (b) a
    # capped fraction from week 0 on shrunk OOS-pro-forma correlation -- NOT built, needs operator
    # opt-in + its own schedule. All three constants TYPE-2 UNVALIDATED (MAGIC_NUMBERS_2026-07.md);
    # replacement trigger: re-estimate the tier boundary + fraction from realized IR (S) once legs
    # have live track records. Honest caveat: OOS/early correlations UNDERSTATE crisis convergence
    # (Longin-Solnik) -- bounded by the tiered budget + caps + convergence watch.
    cold_start_tiered_admission: bool = False
    cold_start_tier_weeks: int = 26
    cold_start_tier_fraction: float = 0.5


@dataclass
class LegInput:
    leg_id: str
    cluster: str
    returns: np.ndarray            # weekly realized returns (chronological)
    oos_vol: float                 # registered OOS weekly vol (for the floor)
    state: str = "ACTIVE"          # ACTIVE | WATCH | SAFE_MODE | ...
    admission_order: int = 0       # lower == senior (admitted earlier)
    prior_weight: float = 0.0


@dataclass
class AllocationResult:
    weights: dict[str, float]
    cash_share: float
    cluster_budgets: dict[str, float]
    vols: dict[str, float]
    corr_matrix: dict[str, dict[str, float]]
    log: dict = field(default_factory=dict)


_STATE_SIZING = {"ACTIVE": 1.0, "WATCH": 0.5}   # SAFE_MODE / other -> 0 (to cash)


def causal_vol(returns: np.ndarray, oos_vol: float, cfg: PortfolioConfig) -> float:
    """Trailing exposure-weighted vol over the window, floored at
    vol_floor_factor x registered OOS vol (a leg that stopped trading cannot be
    made to look risk-free -- PAD adversarial audit #2)."""
    w = np.asarray(returns, dtype=np.float64)[-cfg.cluster_vol_window_weeks:]
    exposed = w[np.abs(w) > 0]                 # exposure-weighted: flat weeks excluded
    realized = float(np.std(exposed, ddof=1)) if len(exposed) > 1 else 0.0
    return max(realized, cfg.vol_floor_factor * abs(oos_vol))


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a2, b2 = np.asarray(a[-n:], float), np.asarray(b[-n:], float)
    if np.std(a2) == 0 or np.std(b2) == 0:
        return 0.0
    return float(np.corrcoef(a2, b2)[0, 1])


# --------------------------------------------------------------------------- #
# Shrinkage + block-bootstrap CI (registry STANDING-PADESTIMATOR; U1/U2 + PB-4a)
#
# delta is estimated via the STATIONARY BLOCK BOOTSTRAP, NOT the classical Ledoit-Wolf
# analytic closed form: the analytic delta assumes i.i.d. observations, which weekly
# returns violate (serial dependence), so the closed form under-shrinks silently. delta is
# recomputed INSIDE each outer resample (a fixed-delta plug-in understates the CI).
# --------------------------------------------------------------------------- #

def _cluster_prior(cluster_a: str, cluster_b: str, cfg: PortfolioConfig) -> float:
    """Shrinkage-target correlation: 0 ACROSS clusters, cluster_prior_rho WITHIN a cluster."""
    return cfg.cluster_prior_rho if cluster_a == cluster_b else 0.0


def _paired_resample_idx(n: int, rng: np.random.Generator, mbl: float) -> np.ndarray:
    """One stationary-block resample of INDICES (reuses the core resampler), so a pair of
    return series is resampled JOINTLY -- preserving the pairing the correlation depends on."""
    return stationary_bootstrap_resample(np.arange(n), mbl, rng).astype(np.int64)


def _shrink_delta(rho: float, boot_var: float, rho_prior: float) -> float:
    """Single-parameter shrinkage intensity: block-boot sampling variance of rho over the
    squared distance from the prior. High sampling noise (or rho near the prior) -> shrink more."""
    denom = boot_var + (rho - rho_prior) ** 2
    return 0.0 if denom <= 0.0 else min(1.0, max(0.0, boot_var / denom))


def _shrunk_corr(a: np.ndarray, b: np.ndarray, rho_prior: float, rng: np.random.Generator,
                 *, n_boot: int = 120, mbl: float = 5.0) -> tuple[float, float]:
    """Point shrunk correlation + delta (delta from the block-bootstrap variance of rho)."""
    a = np.asarray(a, float); b = np.asarray(b, float)
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    rho = _corr(a, b)
    if n < 4 or np.std(a) == 0 or np.std(b) == 0:
        return rho, 0.0
    reps = np.empty(n_boot)
    for i in range(n_boot):
        idx = _paired_resample_idx(n, rng, mbl)
        reps[i] = _corr(a[idx], b[idx])
    delta = _shrink_delta(rho, float(np.var(reps, ddof=1)), rho_prior)
    return delta * rho_prior + (1.0 - delta) * rho, delta


def _shrunk_corr_ci(a: np.ndarray, b: np.ndarray, rho_prior: float, rng: np.random.Generator,
                    *, n_outer: int = 200, n_inner: int = 40, alpha: float = 0.10,
                    mbl: float = 5.0) -> tuple[float, float]:
    """Block-bootstrap CI on the shrunk correlation, delta RECOMPUTED per outer resample."""
    a = np.asarray(a, float); b = np.asarray(b, float)
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n < 4 or np.std(a) == 0 or np.std(b) == 0:
        r = _corr(a, b)
        return r, r
    shrunk = np.empty(n_outer)
    for o in range(n_outer):
        oidx = _paired_resample_idx(n, rng, mbl)
        ao, bo = a[oidx], b[oidx]
        rho_o = _corr(ao, bo)
        inner = np.empty(n_inner)
        for j in range(n_inner):
            iidx = _paired_resample_idx(n, rng, mbl)
            inner[j] = _corr(ao[iidx], bo[iidx])
        delta_o = _shrink_delta(rho_o, float(np.var(inner, ddof=1)) if n_inner > 1 else 0.0, rho_prior)
        shrunk[o] = delta_o * rho_prior + (1.0 - delta_o) * rho_o
    return float(np.quantile(shrunk, alpha / 2.0)), float(np.quantile(shrunk, 1.0 - alpha / 2.0))


# --------------------------------------------------------------------------- #
# U6 -- lower-tail dependence lambda_L (registry STANDING-TAILDEPENDENCE)
#
# A SECOND correlation type: how often two legs are in their worst-q tail TOGETHER -- invisible
# to Pearson (which averages crash days into calm days). DORMANT + DIAGNOSTIC ONLY here: the
# estimator, its CI, and the sufficiency check are BUILT; wiring Tier-2/Tier-3 to convergence-
# watch / capital is a SEPARATE operator-acked step, NOT done in this task.
#
# Estimated on realized / clean-OOS returns only -- NEVER backtest-over-the-fitting-data. The
# sufficiency gate (below) refuses cold-start noise structurally. All thresholds are TYPE-2
# UNVALIDATED with replacement triggers (MAGIC_NUMBERS_2026-07.md).
# --------------------------------------------------------------------------- #

TAIL_Q = 0.10                       # worst-decile tail (TYPE-2, UNVALIDATED)
# MIN_EPISODE_SEPARATION_WEEKS = 13 (one quarter). DERIVED from the stress-
# episode literature: crisis event windows run ~6wk, and ">90 days (~13wk)
# separation" is the standard "fully independent events" bar (crypto systemic-
# risk dating; ECB stress-episode dating). 13wk is floored ABOVE the ~6wk
# event-window size so a single multi-week crisis with a mid-crisis bounce
# CANNOT split into two false episodes -- the exact 2008 "one crash counted as
# many" failure this guard exists to prevent. 4wk (the prior default) LEAKED
# that failure. Still TYPE-2: a future sharpening would add crisis-MECHANISM
# distinctness (not just time-separation), which needs crisis-type
# classification -> deferred.
MIN_EPISODE_SEPARATION_WEEKS = 13


def lower_tail_dependence(a: np.ndarray, b: np.ndarray, q: float = TAIL_Q) -> float:
    """Co-crash rate: of the observations where A is in its worst-q tail, the fraction where B is
    ALSO in its worst-q tail. Independence baseline ~ q; lower-tail-dependent pairs sit well above
    q. Pearson-invisible (two pairs with identical Pearson can have very different lambda_L)."""
    a = np.asarray(a, float); b = np.asarray(b, float)
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n < 10:
        return 0.0
    a_tail = a <= np.quantile(a, q)
    n_a = int(a_tail.sum())
    if n_a == 0:
        return 0.0
    both = int((a_tail & (b <= np.quantile(b, q))).sum())
    return both / n_a


def lower_tail_dependence_ci(a: np.ndarray, b: np.ndarray, rng: np.random.Generator,
                             q: float = TAIL_Q, *, n_boot: int = 200, alpha: float = 0.10,
                             mbl: float = 5.0) -> tuple[float, float, float]:
    """(point, ci_lower, ci_upper) for lambda_L via the paired stationary block bootstrap -- same
    machinery/discipline as the correlation estimator (reused, not forked; no i.i.d. closed form)."""
    a = np.asarray(a, float); b = np.asarray(b, float)
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    point = lower_tail_dependence(a, b, q)
    if n < 10:
        return point, 0.0, 1.0
    reps = np.empty(n_boot)
    for i in range(n_boot):
        idx = _paired_resample_idx(n, rng, mbl)
        reps[i] = lower_tail_dependence(a[idx], b[idx], q)
    return point, float(np.quantile(reps, alpha / 2.0)), float(np.quantile(reps, 1.0 - alpha / 2.0))


def _joint_tail_obs_and_episodes(a: np.ndarray, b: np.ndarray, q: float = TAIL_Q,
                                 sep: int = MIN_EPISODE_SEPARATION_WEEKS) -> tuple[int, int]:
    """(# joint-tail observations, # DISTINCT stress episodes). Episode diversity guards the 2008
    failure: 25 obs from one crash is one piece of evidence repeated, not 25. Two joint-tail obs
    are the same episode if within `sep` of each other."""
    a = np.asarray(a, float); b = np.asarray(b, float)
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n < 10:
        return 0, 0
    joint = np.where((a <= np.quantile(a, q)) & (b <= np.quantile(b, q)))[0]
    if len(joint) == 0:
        return 0, 0
    episodes = 1 + int(np.sum(np.diff(joint) > sep))
    return int(len(joint)), episodes


@dataclass
class TailSufficiency:
    """Which tier the lambda_L evidence currently supports. Reported; NOT wired to capital here."""
    joint_tail_obs: int
    stress_episodes: int
    ci_width: float
    tier: int          # 0 NONE, 1 DIAGNOSTIC, 2 CONVERGENCE_INPUT, 3 CAPITAL_MOVING
    tier_name: str


def tail_dependence_sufficiency(a: np.ndarray, b: np.ndarray, rng: np.random.Generator,
                                q: float = TAIL_Q, *, n_boot: int = 200) -> TailSufficiency:
    """Trust lambda_L only when ALL of: enough joint-tail obs, enough DISTINCT stress episodes,
    tight enough CI. Gates on CONFIDENCE in lambda_L, never its VALUE (circular). Operator-ruled
    starting thresholds (TYPE-2 UNVALIDATED). Episode diversity is the non-negotiable slow guard."""
    n_joint, n_epi = _joint_tail_obs_and_episodes(a, b, q)
    _, lo, hi = lower_tail_dependence_ci(a, b, rng, q, n_boot=n_boot)
    width = hi - lo
    if n_joint >= 40 and n_epi >= 3 and width < 0.20:
        tier, name = 3, "CAPITAL_MOVING"
    elif n_joint >= 25 and n_epi >= 2 and width < 0.25:
        tier, name = 2, "CONVERGENCE_INPUT"
    elif n_joint >= 25:
        tier, name = 1, "DIAGNOSTIC"
    else:
        tier, name = 0, "NONE"
    return TailSufficiency(n_joint, n_epi, width, tier, name)


def _cap_and_cash(weights: dict[str, float], clusters: dict[str, str],
                  cfg: PortfolioConfig) -> tuple[dict[str, float], float]:
    """Apply per-leg / per-cluster / SHORTVOL caps. Anything a cap removes becomes
    CASH (never redistributed) -- silent cash drag is a first-class reported number."""
    capped = {leg: min(w, cfg.max_leg) for leg, w in weights.items()}
    # cluster caps
    by_cluster: dict[str, list[str]] = {}
    for leg, cl in clusters.items():
        by_cluster.setdefault(cl, []).append(leg)
    for cl, legs in by_cluster.items():
        cap = cfg.shortvol_cluster_cap if cl == "SHORTVOL" else cfg.max_cluster
        total = sum(capped[l] for l in legs)
        if total > cap and total > 0:
            scale = cap / total
            for l in legs:
                capped[l] *= scale
    deployed = sum(capped.values())
    return capped, max(0.0, 1.0 - deployed)


def _apply_step_cap(target: dict[str, float], prior: dict[str, float],
                    cfg: PortfolioConfig) -> dict[str, float]:
    out = {}
    for leg, tw in target.items():
        pw = prior.get(leg, 0.0)
        delta = tw - pw
        if abs(delta) > cfg.max_step:
            tw = pw + cfg.max_step * (1.0 if delta > 0 else -1.0)
        out[leg] = max(0.0, tw)
    return out


def allocate(legs: list[LegInput], cfg: PortfolioConfig | None = None,
             *, apply_step_cap: bool = False) -> AllocationResult:
    """Hierarchical inverse-vol risk budgeting with caps and freed-budget-to-cash.

    Level 1 (across clusters): equal-risk via inverse cluster vol.
    Level 2 (within cluster): inverse leg vol. A non-ACTIVE leg keeps its budget
    slot but its share is scaled by its state factor (WATCH x0.5, SAFE_MODE x0),
    and the freed portion goes to CASH -- never to a cluster sibling (LLD 6.5)."""
    cfg = cfg or PortfolioConfig()
    if not legs:
        return AllocationResult({}, 1.0, {}, {}, {})

    vols = {leg.leg_id: causal_vol(leg.returns, leg.oos_vol, cfg) for leg in legs}
    clusters = {leg.leg_id: leg.cluster for leg in legs}

    # Level 1: cluster vol = vol of the cluster's inverse-vol-weighted return series
    cluster_legs: dict[str, list[LegInput]] = {}
    for leg in legs:
        cluster_legs.setdefault(leg.cluster, []).append(leg)
    cluster_vol: dict[str, float] = {}
    for cl, members in cluster_legs.items():
        inv = np.array([1.0 / vols[m.leg_id] for m in members])
        inv = inv / inv.sum()
        n = min(len(m.returns) for m in members)
        stacked = np.vstack([np.asarray(m.returns[-n:], float) for m in members])
        cluster_series = inv @ stacked
        cluster_vol[cl] = max(float(np.std(cluster_series, ddof=1)) if n > 1 else 0.0, 1e-9)

    inv_cluster = {cl: 1.0 / v for cl, v in cluster_vol.items()}
    total_inv = sum(inv_cluster.values())
    cluster_budget = {cl: inv_cluster[cl] / total_inv for cl in inv_cluster}

    # Level 2: inverse leg vol within a cluster, at FULL sizing (every leg keeps its
    # budget slot). Caps are applied to this full-sizing allocation, so a demoted
    # leg's cap headroom is NOT handed to a sibling.
    raw: dict[str, float] = {}
    for cl, members in cluster_legs.items():
        inv = {m.leg_id: 1.0 / vols[m.leg_id] for m in members}
        s = sum(inv.values())
        for m in members:
            raw[m.leg_id] = cluster_budget[cl] * inv[m.leg_id] / s

    capped_full, _ = _cap_and_cash(raw, clusters, cfg)
    # Apply state sizing per leg (WATCH x0.5, SAFE_MODE/other x0); the freed portion
    # goes to CASH, never to a cluster sibling (LLD 6.5 / PAD section 4).
    state_by_leg = {leg.leg_id: leg.state for leg in legs}
    capped = {leg: w * _STATE_SIZING.get(state_by_leg[leg], 0.0) for leg, w in capped_full.items()}
    if apply_step_cap:
        prior = {leg.leg_id: leg.prior_weight for leg in legs}
        capped = _apply_step_cap(capped, prior, cfg)
    cash = max(0.0, 1.0 - sum(capped.values()))

    corr_matrix = {a.leg_id: {b.leg_id: _corr(a.returns, b.returns) for b in legs} for a in legs}
    # PAD sec 5 reconstructable attribution: persist raw + shrunk correlation + shrinkage delta
    # side by side, so an auditor can tell "shrinkage stabilized noise" from "shrinkage hid a
    # regime shift". (Log only; does not affect weights/budgets.)
    _log_rng = np.random.default_rng(0)
    corr_shrunk: dict = {a.leg_id: {} for a in legs}
    shrink_delta: dict = {a.leg_id: {} for a in legs}
    # U6 DIAGNOSTIC ONLY (registry STANDING-TAILDEPENDENCE): lambda_L + sufficiency tier logged
    # per pair. Affects NO weight/budget/convergence decision here -- it accumulates the record so
    # the signal can later EARN trust on the real legs. Tier 2/3 activation is a separate acked step.
    tail_dep: dict = {a.leg_id: {} for a in legs}
    for a in legs:
        for b in legs:
            if a.leg_id == b.leg_id:
                corr_shrunk[a.leg_id][b.leg_id], shrink_delta[a.leg_id][b.leg_id] = 1.0, 0.0
                continue
            sh, d = _shrunk_corr(a.returns, b.returns,
                                 _cluster_prior(a.cluster, b.cluster, cfg), _log_rng, n_boot=80)
            corr_shrunk[a.leg_id][b.leg_id], shrink_delta[a.leg_id][b.leg_id] = sh, d
            lam = lower_tail_dependence(a.returns, b.returns)
            suf = tail_dependence_sufficiency(a.returns, b.returns, _log_rng, n_boot=100)
            tail_dep[a.leg_id][b.leg_id] = {
                "lambda_L": lam, "ci_width": suf.ci_width, "tier": suf.tier,
                "tier_name": suf.tier_name, "joint_tail_obs": suf.joint_tail_obs,
                "stress_episodes": suf.stress_episodes}
    return AllocationResult(
        weights=capped, cash_share=cash, cluster_budgets=cluster_budget,
        vols=vols, corr_matrix=corr_matrix,
        log={"cluster_vol": cluster_vol, "cluster_budget": cluster_budget,
             "vols": vols, "weights": capped, "cash_share": cash,
             "corr_matrix": corr_matrix, "corr_matrix_raw": corr_matrix,
             "corr_matrix_shrunk": corr_shrunk, "shrinkage_delta": shrink_delta,
             "tail_dependence": tail_dep})


# --------------------------------------------------------------------------- #
# Admission gate
# --------------------------------------------------------------------------- #

@dataclass
class AdmissionDecision:
    verdict: str                  # ADMIT | REJECT_CORR | DEFER_OVERLAP | DEFER_BLEND
    detail: dict = field(default_factory=dict)
    budget_multiplier: float = 1.0  # U3: <1.0 for a cold-start (26-52wk) tiered admission


def _cold_start_budget_multiplier(overlap_weeks: int, cfg: PortfolioConfig) -> float:
    """U3 tiered budget vs overlap: 0 below the tier floor (DEFER handled upstream);
    cold_start_tier_fraction at the tier floor; growing with statistical significance ~ S*sqrt(t)
    to 1.0 at the full overlap floor. Continuous sqrt(t) shape, not a step."""
    full, start = cfg.min_overlap_weeks, cfg.cold_start_tier_weeks
    if overlap_weeks >= full:
        return 1.0
    if overlap_weeks < start:
        return 0.0
    half = cfg.cold_start_tier_fraction
    frac = (np.sqrt(overlap_weeks) - np.sqrt(start)) / (np.sqrt(full) - np.sqrt(start))
    return float(half + (1.0 - half) * frac)


def _sharpe_ci_lower(returns: np.ndarray, *, n_boot: int = 500, seed: int = 0) -> float:
    r = np.asarray(returns, float)
    if len(r) < 4 or np.std(r) == 0:
        return 0.0
    rng = np.random.default_rng(seed)
    sr = []
    for _ in range(n_boot):
        s = stationary_bootstrap_resample(r, 5.0, rng)
        sd = np.std(s, ddof=1)
        sr.append(float(np.mean(s) / sd) if sd > 0 else 0.0)
    return float(np.quantile(sr, 0.05))


def admission_decision(
    candidate: LegInput, active: list[LegInput], cfg: PortfolioConfig | None = None,
) -> AdmissionDecision:
    """Admission gate (PAD section 2): |rho| < max_abs_corr vs EVERY active leg on
    the overlapping window (>= min_overlap_weeks), and the blend's bootstrap Sharpe
    CI lower bound must not degrade. Insufficient overlap -> DEFER (not waived)."""
    cfg = cfg or PortfolioConfig()
    # U3: when tiered admission is ON, DEFER only below the reduced tier floor (26wk); otherwise the
    # strict 52wk floor (default -- current behavior). admit-small still clears the shrunk-CI gate.
    overlap_floor = (
        cfg.cold_start_tier_weeks if cfg.cold_start_tiered_admission else cfg.min_overlap_weeks)
    overlaps = [min(len(candidate.returns), len(a.returns)) for a in active]
    for a in active:
        overlap = min(len(candidate.returns), len(a.returns))
        if overlap < overlap_floor:
            return AdmissionDecision("DEFER_OVERLAP", {"leg": a.leg_id, "overlap": overlap})
    rng = np.random.default_rng(0)  # deterministic block-bootstrap
    for a in active:
        prior = _cluster_prior(candidate.cluster, a.cluster, cfg)
        lo, hi = _shrunk_corr_ci(candidate.returns, a.returns, prior, rng)
        # U2: ADMIT only if the shrunk-correlation CI wholly clears +/- max_abs_corr.
        if hi >= cfg.max_abs_corr or lo <= -cfg.max_abs_corr:
            return AdmissionDecision(
                "REJECT_CORR",
                {"leg": a.leg_id, "ci_lower": round(lo, 4), "ci_upper": round(hi, 4),
                 "prior": prior})
    if active:
        n = min([len(candidate.returns)] + [len(a.returns) for a in active])
        book = np.mean(np.vstack([np.asarray(a.returns[-n:], float) for a in active]), axis=0)
        book_with = np.mean(
            np.vstack([np.asarray(candidate.returns[-n:], float)]
                      + [np.asarray(a.returns[-n:], float) for a in active]), axis=0)
        if _sharpe_ci_lower(book_with) < _sharpe_ci_lower(book) - 1e-9:
            return AdmissionDecision("DEFER_BLEND", {"reason": "blend_ci_degraded"})
    min_overlap = min(overlaps) if overlaps else len(candidate.returns)
    # First-leg cold-start (no active leg to overlap): the candidate's OWN history is its evidence.
    # DEFER below the tier floor rather than admit at zero budget (option (a) applied to week counts).
    if cfg.cold_start_tiered_admission and min_overlap < cfg.cold_start_tier_weeks:
        return AdmissionDecision("DEFER_OVERLAP", {"overlap": min_overlap})
    budget_multiplier = (
        _cold_start_budget_multiplier(min_overlap, cfg)
        if cfg.cold_start_tiered_admission else 1.0)
    return AdmissionDecision(
        "ADMIT", {"overlap": min_overlap, "budget_multiplier": round(budget_multiplier, 4)},
        budget_multiplier=budget_multiplier)


# --------------------------------------------------------------------------- #
# Correlation-convergence watch
# --------------------------------------------------------------------------- #

@dataclass
class ConvergenceAlert:
    junior_leg: str
    senior_leg: str
    consecutive_weeks: int


def convergence_watch(
    a: LegInput, b: LegInput, cfg: PortfolioConfig | None = None,
) -> ConvergenceAlert | None:
    """If the trailing 26wk pairwise correlation exceeds the threshold for
    `consecutive_weeks` consecutive weeks, the JUNIOR (later-admitted) leg drops to
    WATCH sizing and a report is filed (PAD section 4)."""
    cfg = cfg or PortfolioConfig()
    ra, rb = np.asarray(a.returns, float), np.asarray(b.returns, float)
    n = min(len(ra), len(rb))
    w = cfg.cluster_vol_window_weeks
    prior = _cluster_prior(a.cluster, b.cluster, cfg)
    rng = np.random.default_rng(0)
    run = 0
    for end in range(w, n + 1):
        rho_shrunk, _ = _shrunk_corr(ra[end - w:end], rb[end - w:end], prior, rng, n_boot=60)
        if rho_shrunk > cfg.convergence_corr_threshold:
            run += 1
            if run >= cfg.convergence_consecutive_weeks:
                # U2: fire only when the CI LOWER bound also clears the threshold (confident,
                # not a noisy point spike).
                lo, _ = _shrunk_corr_ci(ra[end - w:end], rb[end - w:end], prior, rng)
                if lo > cfg.convergence_corr_threshold:
                    junior, senior = (a, b) if a.admission_order >= b.admission_order else (b, a)
                    return ConvergenceAlert(junior.leg_id, senior.leg_id, run)
        else:
            run = 0
    return None


def blend_sharpe(legs: list[LegInput], weights: dict[str, float]) -> float:
    """Weekly Sharpe of the weighted blend over the common window (for reporting)."""
    n = min(len(leg.returns) for leg in legs)
    stacked = np.vstack([np.asarray(leg.returns[-n:], float) for leg in legs])
    w = np.array([weights.get(leg.leg_id, 0.0) for leg in legs])
    if w.sum() == 0:
        return 0.0
    series = (w / w.sum()) @ stacked
    sd = np.std(series, ddof=1)
    return float(np.mean(series) / sd) if sd > 0 else 0.0
