"""Robust-design preflight (v6) — structured A1 gate + burn/family-multiplicity ledgers.

Doctrine (verbatim, ratified): docs/research/ROBUST_DESIGN_PREFLIGHT.md. This module WIRES it; it does
not reinterpret it. The implementation honours the doctrine's own three-way honesty split exactly:

  STRUCTURALLY ENFORCED (code blocks A1) — COMPLETENESS + internal consistency of the gate structures
    (all confound axes covered, no UNPAID parameter, Gate 7 knowable<=used ordering, both Gate 4 floors
    below the plausible effect, decay-cutoff conditions evaluated, …) AND the two ledgers' MECHANICAL
    logic (lineage-burn rebuttable presumption with git-checkable pre-dating; family multiplicity keyed
    by the declared refuting fact with the gerrymander guard on identical refuters).
  SURFACED FOR HUMAN REVIEW (present-but-unjudged; NOT gated) — QUALITY: is the mechanism real, is the
    falsifiability observation genuine, is a confound-separation convincing, is a differently-worded
    refuter SEMANTICALLY entailed by an existing one, is an external-derivation cite truthful. Code
    checks PRESENCE, never truth.
  NAMED-BUT-IRREDUCIBLE (the honor-hole; code MUST NOT pretend to close it) — out-of-repo exploration is
    undetectable; the research-environment declaration + git-archaeology spot-check MITIGATE, not close.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CONFOUND_AXES = ("horizon", "universe", "calendar")
PAID_CATEGORIES = ("DERIVED", "WALLED-OFF", "DECLARED-DEFLATED")
DECAY_CUTOFF_CONDS = ("modern_window_full_battery", "trend_extrapolated_above_zero", "modern_2x_cost_clear")
# Gate 2 TRAJECTORY CRITERION (amendment 2026-07-26) — the CLOSED shape set (C2). A trajectory the
# mechanism predicts but that is none of these is a DOCTRINE-ESCALATION event, not a registrant choice.
VALID_SHAPES = ("FLAT", "MONOTONE-SCALED", "UNIMODAL")


# --------------------------------------------------------------------------- #
# Gate 2 trajectory helpers — shape-vs-observed analysis (the failure-mode teeth).
# --------------------------------------------------------------------------- #
def _is_monotone(seq) -> bool:
    inc = all(y >= x for x, y in zip(seq, seq[1:]))
    dec = all(y <= x for x, y in zip(seq, seq[1:]))
    return inc or dec


def _rel_variation(seq) -> float:
    """(max-min) normalized by the magnitude scale — for the FLAT ≤50% bound."""
    if not seq:
        return 0.0
    scale = max((abs(x) for x in seq), default=0.0) or 1.0
    return (max(seq) - min(seq)) / scale


def _count_humps(seq) -> int:
    """Number of rise-then-fall transitions (interior local maxima) in a single-signed sequence.
    monotone -> 0, unimodal rise-then-decay -> 1, noise/multi-modal -> >=2."""
    humps, prev = 0, 0
    for x, y in zip(seq, seq[1:]):
        d = y - x
        s = 1 if d > 0 else (-1 if d < 0 else 0)
        if s != 0:
            if prev == 1 and s == -1:
                humps += 1
            prev = s
    return humps


def _trajectory_failure_mode(shape: str, observed: list) -> str | None:
    """Return a failure-mode string if the OBSERVED trajectory contradicts the declared SHAPE, else None.
    `observed` is a list of {'param','effect','significant'} points (verdict-time / calibration readings;
    absent at a prospective A1, where only the declaration completeness — C1/C2/C3 — is checked)."""
    pts = sorted(observed, key=lambda r: r.get("param", 0))
    eff = [float(r.get("effect", 0.0)) for r in pts]
    sig = [bool(r.get("significant")) for r in pts]
    n = len(eff)
    # SINGLE-HORIZON SPIKE — significant at exactly one point, dead on both immediate neighbors.
    sig_idx = [i for i, v in enumerate(sig) if v]
    if len(sig_idx) == 1:
        i = sig_idx[0]
        left_dead = i - 1 < 0 or not sig[i - 1]
        right_dead = i + 1 >= n or not sig[i + 1]
        if left_dead and right_dead:
            return ("SINGLE-HORIZON-SPIKE — significant at exactly one point, dead on both immediate "
                    "neighbors -> FAIL (the peak-masquerading-as-mechanism the gate exists to catch)")
    humps = _count_humps(eff)
    if humps >= 2:
        return "MULTI-MODAL — two or more humps in the observed trajectory -> FAIL (noise or a searched artifact)"
    if shape == "FLAT" and _rel_variation(eff) > 0.5:
        return "SHAPE-MISMATCH — declared FLAT, observed a trend / >50% variation across the band -> FAIL"
    if shape == "MONOTONE-SCALED" and not _is_monotone(eff):
        return "SHAPE-MISMATCH — declared MONOTONE-SCALED, observed non-monotone -> FAIL"
    if shape == "UNIMODAL" and (_is_monotone(eff) or humps != 1):
        return "SHAPE-MISMATCH — declared UNIMODAL, observed no single interior peak (monotone/flat) -> FAIL"
    return None


# --------------------------------------------------------------------------- #
# STRUCTURALLY ENFORCED — gate-structure completeness + internal consistency.
# --------------------------------------------------------------------------- #
def verify_robust_design(pf: dict) -> tuple[bool, list[str], list[str]]:
    """Check the filled preflight STRUCTURE for completeness + internal consistency.
    Returns (ok, blocking_issues, surfaced_notes). Blocking issues fail A1. Surfaced notes are
    present-but-unjudged QUALITY items for human review (never gated here)."""
    b: list[str] = []          # blocking (completeness / consistency)
    s: list[str] = []          # surfaced for human review (quality — presence only)

    # --- Gate 0: falsifiable, confound-distinguished mechanism ---
    g0 = pf.get("gate0", {})
    for k in ("mechanism_sentence", "falsifiability_observation", "price_shape"):
        v = str(g0.get(k, "")).strip()
        if not v:
            b.append(f"GATE0.{k}: required-nonempty")
        elif k in ("mechanism_sentence", "falsifiability_observation"):
            s.append(f"GATE0.{k}: present — QUALITY not machine-adjudicable, surfaced for operator review")
    axes_seen = {str(r.get("axis", "")).lower() for r in g0.get("confound_table", [])}
    for ax in CONFOUND_AXES:
        if ax not in axes_seen:
            b.append(f"GATE0.confound_table: axis '{ax}' unaddressed (all three axes required)")
    for r in g0.get("confound_table", []):
        if not str(r.get("named_alternative", "")).strip() or not str(r.get("how_separated", "")).strip():
            b.append(f"GATE0.confound_table: axis '{r.get('axis')}' missing named_alternative/how_separated")
        else:
            s.append(f"GATE0.confound[{r.get('axis')}]: separation present — is it CONVINCING? human review")

    # --- Gate 1: every parameter paid ---
    ledger = pf.get("gate1", {}).get("parameter_ledger", [])
    if not ledger:
        b.append("GATE1.parameter_ledger: empty — list every number")
    for p in ledger:
        cat = p.get("category")
        if cat not in PAID_CATEGORIES:
            b.append(f"GATE1: parameter '{p.get('name')}' category={cat!r} is UNPAID/invalid "
                     f"(must be one of {PAID_CATEGORIES})")
        if not str(p.get("artifact_ref", "")).strip():
            b.append(f"GATE1: parameter '{p.get('name')}' missing artifact_ref")
        if not str(p.get("form_vs_coefficient_note", "")).strip():
            b.append(f"GATE1: parameter '{p.get('name')}' missing form_vs_coefficient_note")

    # --- Gate 1b: joint-search accounting ---
    g1b = pf.get("gate1b", {})
    if not isinstance(g1b.get("joint_search_space_size"), int):
        b.append("GATE1b.joint_search_space_size: required integer (number of COMBINATIONS)")
    if not g1b.get("git_reconstructable"):
        b.append("GATE1b.git_reconstructable: assertion required (all design exploration in-repo)")

    # --- Meta-rule 1: refuting fact (feeds family ledger) ---
    if not str(pf.get("refuting_fact", "")).strip():
        b.append("METARULE1.refuting_fact: required-nonempty (the single empirical truth; feeds family ledger)")

    # --- Gate 2: TRAJECTORY (neighborhood + granularity + time-periods + trajectory-criterion) ---
    g2 = pf.get("gate2", {})
    if not g2.get("neighborhood"):
        b.append("GATE2.neighborhood: pre-declared neighborhood required")
    if not str(g2.get("granularity", "")).strip():
        b.append("GATE2.granularity: required (with mechanism-cadence rationale)")
    periods = g2.get("time_periods", [])
    if not periods:
        b.append("GATE2.time_periods: pre-declared periods required (all-must-pass)")
    for per in periods:
        if per.get("operative") is None:
            b.append(f"GATE2: period '{per.get('period')}' missing OPERATIVE/INOPERATIVE flag")
        elif per.get("operative") is False and not str(per.get("reason", "")).strip():
            b.append(f"GATE2: INOPERATIVE period '{per.get('period')}' needs a MECHANISM reason (not performance)")

    # TRAJECTORY CRITERION (amendment 2026-07-26). The code enforces C1 REFERENCE + C2 CLOSED-SET + C3
    # PINNED-PEAK structurally; whether the shape is GENUINELY entailed by the mechanism (C1's "a reviewer
    # can predict it") is a SURFACED human check, never adjudicated here. Failure routing per doctrine:
    # UNDERIVABLE-SHAPE fails at GATE 0 and never reaches the trajectory test; SHAPE-MISMATCH / MULTI-MODAL
    # / SINGLE-HORIZON-SPIKE fail at Gate 2.
    traj = g2.get("trajectory", {})
    shape = str(traj.get("shape", "")).strip().upper()
    mech_present = bool(str(g0.get("mechanism_sentence", "")).strip())
    derived_from = str(traj.get("derived_from_gate0", "")).strip()
    if not shape:
        b.append(f"GATE2.trajectory.shape: required — declare the mechanism-predicted shape ({'|'.join(VALID_SHAPES)})")
    elif not (derived_from and mech_present):
        # C1 — UNDERIVABLE-SHAPE routes to GATE 0; the registration fails there and never reaches Gate 2.
        b.append("GATE0.trajectory_derivability: UNDERIVABLE-SHAPE — the Gate-2 shape does not reference a "
                 "Gate-0 mechanism (C1); the mechanism is underspecified -> FAIL AT GATE 0, does not reach Gate 2")
    else:
        # C1 reference present; is the derivation CONVINCING? surfaced, not gated.
        s.append("GATE2.trajectory: shape references the Gate-0 mechanism — is it GENUINELY entailed? "
                 "(C1 is a human check; code enforces the reference, not its persuasiveness)")
        if shape not in VALID_SHAPES:
            # C2 — a bespoke shape is a doctrine-escalation event, not a registrant selection.
            b.append(f"GATE2.trajectory.shape: '{shape}' not in the closed set {VALID_SHAPES} — a bespoke shape "
                     "is a DOCTRINE-ESCALATION event (proposer!=ratifier), not a registrant selection (C2)")
        elif shape == "UNIMODAL":
            # C3 — pinned peak + minimum band-width (>=2 neighbors each side, peak in the interior).
            peak = traj.get("peak_location")
            bp = traj.get("band_points")
            pk_ok = isinstance(peak, (int, float)) or (
                isinstance(peak, (list, tuple)) and len(peak) == 2 and all(isinstance(x, (int, float)) for x in peak))
            if not pk_ok:
                b.append("GATE2.trajectory.peak_location: UNIMODAL requires a PINNED peak (a point or narrow "
                         "[lo,hi] interval), declared before the pull (C3)")
            elif not (isinstance(bp, (list, tuple)) and bp and all(isinstance(x, (int, float)) for x in bp)):
                b.append("GATE2.trajectory.band_points: UNIMODAL requires the numeric sweep band to test the peak (C3)")
            else:
                lo = peak[0] if isinstance(peak, (list, tuple)) else peak
                hi = peak[1] if isinstance(peak, (list, tuple)) else peak
                below, above = sum(1 for x in bp if x < lo), sum(1 for x in bp if x > hi)
                if below < 2 or above < 2:
                    b.append("GATE2.trajectory: PINNED-PEAK band inadequate — "
                             f"{below} point(s) below / {above} above the declared peak; C3 requires >=2 each side "
                             "with the peak in the band INTERIOR (a peak at/outside an edge, or a band too narrow to "
                             "show two-up-two-down, FAILS)")
        # Observed-trajectory match (verdict-time / calibration; absent at a prospective A1).
        if shape in VALID_SHAPES:
            observed = traj.get("observed_trajectory")
            if observed:
                fm = _trajectory_failure_mode(shape, observed)
                if fm:
                    b.append(f"GATE2.trajectory: {fm}")

    # --- Gate 4: two floors as numbers, plausible effect above both ---
    g4 = pf.get("gate4", {})
    nums = {k: g4.get(k) for k in ("mde_at_n", "economic_floor", "plausible_effect")}
    for k, v in nums.items():
        if not isinstance(v, (int, float)):
            b.append(f"GATE4.{k}: required number")
    if all(isinstance(v, (int, float)) for v in nums.values()):
        if not (nums["plausible_effect"] > nums["mde_at_n"] and nums["plausible_effect"] > nums["economic_floor"]):
            b.append("GATE4: plausible_effect must exceed BOTH floors (MDE and economic) — "
                     f"got effect={nums['plausible_effect']} vs mde={nums['mde_at_n']} econ={nums['economic_floor']}")

    # --- Gate 5: pre-committed outcome readings + decay cutoff if decay ---
    g5 = pf.get("gate5", {})
    readings = g5.get("outcome_readings", {})
    for k in ("pass", "reject", "quarantine", "underpowered_null"):
        if not str(readings.get(k, "")).strip():
            b.append(f"GATE5.outcome_readings.{k}: required-nonempty (pre-committed reading)")
    if g5.get("is_decay_case"):
        dc = g5.get("decay_cutoff", {})
        for c in DECAY_CUTOFF_CONDS:
            if dc.get(c) is None:
                b.append(f"GATE5.decay_cutoff.{c}: must be evaluated (bool) for a decay case")

    # --- Gate 6: population integrity assertions ---
    g6 = pf.get("gate6", {})
    for k in ("eligibility_outcome_blind", "leg_symmetry", "exclusion_reconciliation"):
        if not g6.get(k):
            b.append(f"GATE6.{k}: assertion required (True)")

    # --- Gate 7: field table with knowable<=used ordering ---
    ft = pf.get("gate7", {}).get("field_table", [])
    if not ft:
        b.append("GATE7.field_table: required (every consumed field)")
    for row in ft:
        field = str(row.get("field", "")).strip()
        kt, ut = row.get("knowable_ts"), row.get("used_ts")
        if not field or kt is None or ut is None:
            b.append(f"GATE7: field row incomplete: {row}")
        elif not (kt <= ut):
            b.append(f"GATE7: LOOKAHEAD — field '{field}' knowable_ts={kt} AFTER used_ts={ut}")

    # --- Honor-hole mitigation (surfaced, not gated for QUALITY) ---
    if not str(pf.get("research_environment", "")).strip():
        b.append("HONOR.research_environment: declaration required (mitigation field; presence gated, truth not)")
    else:
        s.append("HONOR.research_environment: declared — OUT-OF-REPO exploration is UNDETECTABLE (irreducible); "
                 "run git_archaeology_spotcheck() to look for undeclared exploration")

    return (len(b) == 0, b, s)


def assert_robust_design_complete(pf: dict) -> list[str]:
    """Raise on any blocking completeness/consistency failure; return the surfaced-for-review notes."""
    ok, blocking, surfaced = verify_robust_design(pf)
    if not ok:
        raise RuntimeError("A1 ROBUST-DESIGN PREFLIGHT FAILED (completeness) — "
                           + "; ".join(blocking[:12]) + (" …" if len(blocking) > 12 else ""))
    return surfaced


# --------------------------------------------------------------------------- #
# LEDGER A — BURN (lineage), rebuttable presumption. Keyed by MECHANISM FAMILY.
# --------------------------------------------------------------------------- #
_BURN_DDL = """
CREATE TABLE IF NOT EXISTS rdp_burn_ledger (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis       TEXT NOT NULL,
    reserved_window  TEXT NOT NULL,
    mechanism_family TEXT NOT NULL,
    grade_utc        TEXT NOT NULL,
    grade_event      TEXT
);
"""


def record_grade(db_path, hypothesis: str, reserved_window: str, mechanism_family: str,
                 grade_utc: str, grade_event: str = "") -> None:
    """Record that `hypothesis` (of `mechanism_family`) was GRADED on `reserved_window` at `grade_utc`.
    Called at decision time; the A1 gate READS this to compute the burn presumption."""
    with sqlite3.connect(db_path) as c:
        c.executescript(_BURN_DDL)
        c.execute("INSERT INTO rdp_burn_ledger (hypothesis, reserved_window, mechanism_family, grade_utc, "
                  "grade_event) VALUES (?,?,?,?,?)",
                  (hypothesis, reserved_window, mechanism_family, grade_utc, grade_event))


def _commit_predates(commit_hash: str, cutoff_utc: str) -> bool:
    """git-checkable: does `commit_hash`'s COMMITTER date pre-date `cutoff_utc`? Proves the design existed
    before the family's verdict could have taught it. Unknown/absent commit -> False (denies the claim)."""
    try:
        r = subprocess.run(["git", "show", "-s", "--format=%cI", commit_hash],
                           cwd=str(REPO_ROOT), capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return False
        return r.stdout.strip() < cutoff_utc
    except Exception:
        return False


def burn_status(db_path, mechanism_family: str, new_reg_utc: str, target_windows,
                rebuttals: dict | None = None) -> dict:
    """For each target reserved window: is the NEW hypothesis (family `mechanism_family`, registered
    `new_reg_utc`) PRESUMED BURNED — i.e. a same-family hypothesis was already graded on that window and
    the new one was registered AFTER that grade — and if so, is it rebutted?
    Returns {window: {presumed_burned, rebutted, rebuttal_kind, reason}}. rebuttal_kind 'commit' is
    git-verified; 'external' is RECORDED + SURFACED (not adjudicated for truth)."""
    rebuttals = rebuttals or {}
    out: dict = {}
    with sqlite3.connect(db_path) as c:
        c.executescript(_BURN_DDL)
        for w in target_windows:
            rows = c.execute("SELECT grade_utc FROM rdp_burn_ledger WHERE reserved_window=? AND "
                             "mechanism_family=? ORDER BY grade_utc ASC", (w, mechanism_family)).fetchall()
            grades = [r[0] for r in rows]
            presumed = any(new_reg_utc > g for g in grades)   # registered AFTER a same-family grade on w
            if not presumed:
                out[w] = {"presumed_burned": False, "rebutted": True, "rebuttal_kind": None,
                          "reason": "no same-family prior grade on this window"}
                continue
            earliest = grades[0]
            reb = rebuttals.get(w)
            if reb and reb.get("kind") == "commit" and _commit_predates(str(reb.get("value", "")), earliest):
                out[w] = {"presumed_burned": True, "rebutted": True, "rebuttal_kind": "commit",
                          "reason": f"commit {reb.get('value')} pre-dates family verdict {earliest} (git-verified)"}
            elif reb and reb.get("kind") == "external" and str(reb.get("value", "")).strip():
                out[w] = {"presumed_burned": True, "rebutted": True, "rebuttal_kind": "external",
                          "reason": "external-derivation cite RECORDED — SURFACED for human/audit review, not adjudicated"}
            else:
                out[w] = {"presumed_burned": True, "rebutted": False, "rebuttal_kind": None,
                          "reason": f"same-family grade on '{w}' at {earliest}; no valid rebuttal -> BLOCK (route to "
                                    "next untouched reserved window or forward accrual)"}
    return out


def assert_not_burned(db_path, mechanism_family: str, new_reg_utc: str, target_windows,
                      rebuttals: dict | None = None) -> dict:
    """Raise if any target window is presumed-burned WITHOUT a valid rebuttal. Returns the full status
    (so callers can surface the 'external' rebuttals for review)."""
    st = burn_status(db_path, mechanism_family, new_reg_utc, target_windows, rebuttals)
    blocked = [w for w, s in st.items() if not s["rebutted"]]
    if blocked:
        raise RuntimeError("A1 ROBUST-DESIGN PREFLIGHT FAILED (lineage-burn) — presumed burned on "
                           f"{blocked} with no valid rebuttal. " + "; ".join(st[w]["reason"] for w in blocked))
    return st


# --------------------------------------------------------------------------- #
# LEDGER B — FAMILY MULTIPLICITY, keyed by REFUTING FACT. Gerrymander guard.
# --------------------------------------------------------------------------- #
_FAMILY_DDL = """
CREATE TABLE IF NOT EXISTS rdp_family_ledger (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis       TEXT NOT NULL,
    refuting_fact    TEXT NOT NULL,
    refuter_key      TEXT NOT NULL,
    registered_utc   TEXT,
    distinguishing_observation TEXT
);
"""


def _refuter_key(refuting_fact: str) -> str:
    """Normalize a refuting fact to a family key: lowercase, collapse whitespace, strip punctuation.
    IDENTICAL refuters (however differently the MECHANISM is named) map to the same key -> JOINT family.
    This is the structurally-enforceable half of the same-family test. SEMANTIC ENTAILMENT of a
    differently-worded refuter is NOT machine-checkable and is surfaced for review, never auto-joined."""
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", refuting_fact.strip().lower())).strip()


def family_multiplicity(db_path, refuting_fact: str) -> int:
    """Number of DISTINCT hypotheses already sharing this refuter (family size, this member excluded)."""
    with sqlite3.connect(db_path) as c:
        c.executescript(_FAMILY_DDL)
        rows = c.execute("SELECT DISTINCT hypothesis FROM rdp_family_ledger WHERE refuter_key=?",
                         (_refuter_key(refuting_fact),)).fetchall()
    return len(rows)


def register_family_member(db_path, hypothesis: str, refuting_fact: str, registered_utc: str = "",
                           distinguishing_observation: str = "") -> None:
    with sqlite3.connect(db_path) as c:
        c.executescript(_FAMILY_DDL)
        c.execute("INSERT INTO rdp_family_ledger (hypothesis, refuting_fact, refuter_key, registered_utc, "
                  "distinguishing_observation) VALUES (?,?,?,?,?)",
                  (hypothesis, refuting_fact, _refuter_key(refuting_fact), registered_utc,
                   distinguishing_observation))


def family_bar_check(db_path, hypothesis: str, refuting_fact: str, declared_family_bar: int,
                     claims_new_family: bool = False, distinguishing_observation: str = "") -> dict:
    """The gerrymander guard + multiplicity accounting. Returns
    {family_size_including_this, joint (bool), declared_ok (bool), surfaced (list)}.

    - If the declared refuter matches an existing key -> the family is JOINT: the Nth member's declared bar
      MUST be >= the family size including this member. A 'new family' claim on an IDENTICAL refuter is
      COSMETIC and the count stays joint (enforced) regardless of a different mechanism NAME.
    - If the refuter is genuinely NEW (distinct key) -> a new family (size 1). Whether the distinctness is
      REAL (kills-A-spares-B) vs a cosmetic reword / semantic entailment is SURFACED for review, never
      adjudicated by code (the distinguishing_observation is recorded)."""
    surfaced: list[str] = []
    prior = family_multiplicity(db_path, refuting_fact)      # existing members sharing this exact refuter
    size = prior + 1                                          # including this member
    if prior > 0:
        joint = True
        declared_ok = declared_family_bar >= size
        if claims_new_family:
            surfaced.append("GERRYMANDER: 'new family' claimed but the declared refuter is IDENTICAL to an "
                            "existing family's — split is COSMETIC, count stays JOINT (enforced).")
        if not declared_ok:
            surfaced.append(f"MULTIPLICITY: declared family bar {declared_family_bar} < family size {size}; "
                            "the Nth member's bar must rise with the count.")
    else:
        joint = False
        declared_ok = declared_family_bar >= 1
        surfaced.append("GERRYMANDER: refuter is NEW (distinct key) -> new family (size 1). Is the distinctness "
                        "REAL (an observation that kills an existing member while sparing this one) or a "
                        "reword / SEMANTIC ENTAILMENT? Not machine-checkable — SURFACED for review. "
                        f"claimed distinguishing observation: {distinguishing_observation or '(none supplied)'}")
    return {"family_size_including_this": size, "joint": joint, "declared_ok": declared_ok, "surfaced": surfaced}


# --------------------------------------------------------------------------- #
# NAMED-BUT-IRREDUCIBLE — honor-hole mitigation (surfaced, never auto-blocking).
# --------------------------------------------------------------------------- #
def git_archaeology_spotcheck() -> dict:
    """Surface potential UNDECLARED out-of-repo exploration: orphaned branches, dangling commits, and
    notebook checkpoints. This MITIGATES the irreducible honor-hole; it does NOT close it and NEVER
    auto-blocks. The operator or an audit runs it and eyeballs the result."""
    def _run(args):
        try:
            return subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True).stdout
        except Exception:
            return ""
    dangling = [ln.split()[-1] for ln in _run(["git", "fsck", "--no-reflogs", "--dangling"]).splitlines()
                if "dangling commit" in ln]
    branches = [ln.strip("* ").strip() for ln in _run(["git", "branch", "-a"]).splitlines() if ln.strip()]
    checkpoints = [str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.rglob(".ipynb_checkpoints")
                   if ".git" not in p.parts][:50]
    return {"dangling_commits": dangling[:50], "branch_count": len(branches),
            "notebook_checkpoints": checkpoints,
            "note": "SURFACED, not enforcing — out-of-repo exploration is UNDETECTABLE and IRREDUCIBLE per "
                    "doctrine; this only flags in-repo traces for human/audit review."}


# --------------------------------------------------------------------------- #
# TOP-LEVEL A1 GATE — composes completeness (enforced) + burn (enforced) + family (enforced), and
# collects the surfaced-for-review + named-irreducible items. Raises on any blocking failure.
# --------------------------------------------------------------------------- #
def robust_design_a1_gate(db_path, rd: dict) -> dict:
    """The A1 robust-design gate. `rd` carries the filled preflight + ledger inputs:
      rd['preflight']            — the structured gate dict (see verify_robust_design)
      rd['hypothesis'], rd['mechanism_family'], rd['registered_utc']
      rd['target_windows']       — reserved windows this hypothesis wants to grade on (for burn)
      rd['rebuttals']            — {window: {'kind': 'commit'|'external', 'value': ...}}
      rd['declared_family_bar']  — the declared multiplicity bar (must be >= family size)
      rd['claims_new_family'], rd['distinguishing_observation']
    Raises RuntimeError on any BLOCKING failure (completeness, lineage-burn, multiplicity). Returns a
    report classifying findings into enforced / surfaced-for-review / named-irreducible."""
    pf = rd["preflight"]
    surfaced = assert_robust_design_complete(pf)                      # (1) completeness ENFORCED; quality SURFACED
    burn = assert_not_burned(db_path, rd["mechanism_family"], rd["registered_utc"],   # (2) lineage-burn ENFORCED
                             rd.get("target_windows", []), rd.get("rebuttals"))
    for w, st in burn.items():
        if st.get("rebuttal_kind") == "external":
            surfaced.append(f"BURN[{w}]: rebutted by EXTERNAL-DERIVATION cite — SURFACED, not adjudicated for truth")
    fam = family_bar_check(db_path, rd["hypothesis"], pf["refuting_fact"],            # (3) multiplicity ENFORCED
                           rd.get("declared_family_bar", 1), rd.get("claims_new_family", False),
                           rd.get("distinguishing_observation", ""))
    if not fam["declared_ok"]:
        raise RuntimeError("A1 ROBUST-DESIGN PREFLIGHT FAILED (family multiplicity) — declared bar "
                           f"{rd.get('declared_family_bar')} < family size {fam['family_size_including_this']} "
                           "(the Nth member's bar rises with the count; a cosmetic split does not reset it).")
    surfaced += fam["surfaced"]
    return {"enforced_ok": True, "surfaced_for_review": surfaced,
            "named_irreducible": ["out-of-repo exploration is UNDETECTABLE (the honor-hole) — mitigated by the "
                                  "research_environment declaration + git_archaeology_spotcheck(), NOT closed"],
            "burn": burn, "family": fam}
