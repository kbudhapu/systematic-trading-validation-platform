"""W-A (corrected 2026-07-11): knife-edge gate BAND vs estimator BIAS — decomposed.

Feeds the knife-edge verdict path (`psd_gates.py`). SFD 1.4: inside the replayable path -> seeded,
hash-pinned, bit-identically reproducible. Two DISTINCT error sources, never again conflated:

- **dsr_band = SOURCE A only** — the Gumbel/Euler-Mascheroni APPROXIMATION ERROR: E[max of N iid SR]
  closed form vs seeded Monte-Carlo, with sigma_SR HELD IDENTICAL on both arms. At the 0.95 gate z is
  pinned at Phi^-1(0.95)=1.645, so sigma_SR CANCELS and the band depends on n_trials ONLY (measured,
  confirmed n_obs-independent). A true two-sided tolerance band. Empirically ~1e-3..3e-3 -> below the
  registered 5e-3 floor at every N, so the operational band IS the floor.

- **dsr_bias = SOURCE B, a BIAS not a band** — the Gaussian sigma_SR misspecification (both repo copies
  use gamma3=0, gamma4=3; 0.C). Under real skew/kurtosis the reported DSR is ONE-DIRECTIONAL toward
  FALSE PASSES (DSR_full < DSR_gauss). A tolerance band CANNOT fix a systematic bias -> this is a PSD S9
  estimator-CORRECTION question and is emitted as a SEPARATE, SIGNED field that psd_gates does NOT
  consume as a band.

pbo_band keyed by (n_slices, n_configs): the CSCV PBO estimator's sampling spread (seeded MC).
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.stats import norm

EULER = 0.5772156649015328
DSR_BAND_FLOOR = 5e-3
PBO_BAND_FLOOR = 0.01
DSR_GATE = 0.95
PBO_GATE = 0.10

SEED = 20260711
N_MONTE_CARLO = 400            # PBO-spread MC reps
GUMBEL_MC_REPS = 20000         # E[max] MC reps for Source A
PLANTED_SKEW = -1.0            # for the SOURCE-B bias field (plausible 15-min-bar non-normality)
PLANTED_KURTOSIS = 10.0

DSR_N_TRIALS = (2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)   # band domain (1-D)
DSR_BIAS_N_OBS = (130, 260, 520, 1040, 2600, 5200)                   # bias field second axis
PBO_N_SLICES = (6, 8, 10, 12)
PBO_N_CONFIGS = (6, 12, 24, 48)

# --- SOURCE C (A1): corrected-estimator sampling-error band. Keyed (series_kind, n_obs, zero_frac,
# g3, g4). Covers the POST-T2 series the verdict's moments are actually computed on (per_event may
# carry residual zeros up to the 50% T2 boundary; trade_level/daily are dense). n_obs 50..3000. ---
DSR_C_N_OBS = (50, 130, 260, 520, 1040, 2080, 3000)
DSR_C_G3 = (0.0, -1.0)                 # skew: symmetric and the plausible 15-min-bar left skew
DSR_C_G4 = (3.0, 10.0)                 # kurtosis: Gaussian and heavy-tailed (matches dsr_bias planting)
DSR_C_KIND_ZERO = {                    # series_kind -> residual zero-fractions it realistically carries
    "per_event": (0.0, 0.25, 0.5),
    "trade_level": (0.0,),
    "daily": (0.0, 0.25),
}
DSR_ESTIMATOR_BAND_FLOOR = 5e-3        # Source-C FLOOR_FALLBACK (never zero); measured supersedes
DSR_C_MC_REPS = 300                    # resamples per grid cell
DSR_C_N_TRIALS_REF = 100               # operating point: representative multiple-testing depth

ARTIFACT_PATH = Path("data/research/knife_edge_bands.json")


# --------------------------------------------------------------------------- #
# SOURCE A — Gumbel/EM approximation band (sigma_SR cancels; n_trials-only).
# --------------------------------------------------------------------------- #
def _k_closed(n_trials: int) -> float:
    q1 = norm.ppf(1.0 - 1.0 / n_trials)
    q2 = norm.ppf(1.0 - math.exp(-1.0) / n_trials)
    return (1.0 - EULER) * q1 + EULER * q2


def gumbel_band(n_trials: int, rng: np.random.Generator, reps: int = GUMBEL_MC_REPS) -> float:
    """SOURCE A: |DSR_closed - DSR_MC| at the 0.95 gate with sigma_SR identical. z=1.645 is pinned, so
    sigma_SR cancels: band = |Phi(z) - Phi(z - (K_mc - K_closed))|, a pure function of n_trials."""
    z = norm.ppf(DSR_GATE)
    k_mc = float(rng.standard_normal((reps, n_trials)).max(axis=1).mean())
    return abs(DSR_GATE - float(norm.cdf(z - (k_mc - _k_closed(n_trials)))))


# --------------------------------------------------------------------------- #
# SOURCE B — Gaussian sigma_SR misspecification BIAS (signed; NOT a band).
# --------------------------------------------------------------------------- #
def sigma_sr_gaussian(sr_pbar: float, n_obs: int) -> float:
    return math.sqrt((1.0 + 0.5 * sr_pbar ** 2) / max(n_obs - 1, 1))


def sigma_sr_full(sr_pbar: float, n_obs: int, g3: float, g4: float) -> float:
    var = 1.0 - g3 * sr_pbar + ((g4 - 1.0) / 4.0) * sr_pbar ** 2
    return math.sqrt(max(var, 1e-12) / max(n_obs - 1, 1))


def _dsr_from(sr_pbar: float, n_trials: int, sigma_sr: float) -> float:
    return float(norm.cdf((sr_pbar - sigma_sr * _k_closed(n_trials)) / max(sigma_sr, 1e-12)))


def dsr_bias_signed(n_trials: int, n_obs: int, *, g3: float = PLANTED_SKEW,
                    g4: float = PLANTED_KURTOSIS) -> float:
    """SOURCE B: DSR_full - DSR_gauss at the gate (SIGNED; NEGATIVE = the Gaussian estimator
    over-certifies = toward FALSE PASS). Note the gate-clearing sr_pbar = sigma_SR*(z+K) ∝ 1/sqrt(n_obs)
    (sr_annual is fixed; the per-period Sharpe that clears the DEFLATED bar shrinks with more data), so
    the bias is genuinely n_obs-dependent -- NOT because SR was varied with n_obs by hand."""
    z = norm.ppf(DSR_GATE); a = _k_closed(n_trials)
    sg = math.sqrt(1.0 / max(n_obs - 1, 1)); srp = sg * (z + a)
    sg = sigma_sr_gaussian(srp, n_obs); srp = sg * (z + a)
    dsr_gauss = _dsr_from(srp, n_trials, sg)                       # == 0.95
    dsr_full = _dsr_from(srp, n_trials, sigma_sr_full(srp, n_obs, g3, g4))
    return dsr_full - dsr_gauss                                    # signed


# --------------------------------------------------------------------------- #
# PBO estimator spread (seeded MC).
# --------------------------------------------------------------------------- #
def pbo_band_measure(n_slices: int, n_configs: int, rng: np.random.Generator,
                     reps: int = N_MONTE_CARLO) -> float:
    from src.research.psd.cpcv_pbo import pbo_cscv
    vals = np.empty(reps)
    for i in range(reps):
        vals[i] = pbo_cscv(rng.normal(size=(n_slices, n_configs)))["pbo"]
    return float(vals.std(ddof=1))


# --------------------------------------------------------------------------- #
# SOURCE C — corrected-estimator sampling-error band (A1). Measures how much the
# full-moment DSR wobbles because gamma3/gamma4 are ESTIMATED from a finite series
# (kurtosis estimates are especially noisy under fat tails). Series with target
# (skew, kurtosis) are drawn by the Fleishman (1978) power method.
# --------------------------------------------------------------------------- #
def _fleishman_coeffs(skew: float, excess_kurt: float) -> tuple[float, float, float, float]:
    """Fleishman power-method coefficients (a,b,c,d) with a=-c, so X = a+bZ+cZ^2+dZ^3 (Z~N(0,1)) has
    mean 0, var 1, the target skew, and the target EXCESS kurtosis. Solved with fsolve."""
    import warnings

    from scipy.optimize import fsolve

    def eqs(p):
        b, c, d = p
        return [
            b * b + 6 * b * d + 2 * c * c + 15 * d * d - 1.0,
            2 * c * (b * b + 24 * b * d + 105 * d * d + 2.0) - skew,
            24 * (b * d + c * c * (1 + b * b + 28 * b * d)
                  + d * d * (12 + 48 * b * d + 141 * c * c + 225 * d * d)) - excess_kurt,
        ]
    with warnings.catch_warnings():          # the Gaussian cell starts AT the root -> benign
        warnings.simplefilter("ignore", RuntimeWarning)   # "not making good progress" false alarm
        b, c, d = fsolve(eqs, [1.0, 0.0, 0.0], full_output=False)
    return (-c, b, c, d)


def _fleishman_sample(n: int, coeffs: tuple[float, float, float, float],
                      rng: np.random.Generator) -> np.ndarray:
    a, b, c, d = coeffs
    z = rng.standard_normal(n)
    return a + b * z + c * z * z + d * z * z * z


def _gate_clearing_sr_pbar(n_obs: int, n_trials: int) -> float:
    """The per-period Sharpe that makes the GAUSSIAN DSR sit exactly at the 0.95 gate (the operating
    point at which the band matters). Mirrors dsr_bias_signed's fixed-point so C is measured where A
    and the bias were: sr_pbar = sigma_SR_gauss * (z + K), sigma depending on sr_pbar -> 2 iterations."""
    z = norm.ppf(DSR_GATE); a = _k_closed(n_trials)
    sg = math.sqrt(1.0 / max(n_obs - 1, 1)); srp = sg * (z + a)
    sg = sigma_sr_gaussian(srp, n_obs)
    return sg * (z + a)


def dsr_estimator_band_measure(n_obs: int, zero_frac: float, g3: float, g4: float,
                               rng: np.random.Generator, reps: int = DSR_C_MC_REPS,
                               n_trials: int = DSR_C_N_TRIALS_REF) -> float:
    """SOURCE C: seeded-MC sampling std of the full-moment DSR at the gate operating point, holding
    the true per-period Sharpe fixed and letting ONLY the estimated (gamma3, gamma4) vary across
    resamples of size n_obs (with `zero_frac` of the points structural zeros, as the estimator sees
    them). Returns the std of DSR across `reps` draws -- the estimator's own resolution."""
    from src.research.psd.cpcv_pbo import _series_moments, full_moment_sigma_sr

    coeffs = _fleishman_coeffs(g3, g4 - 3.0)
    srp = _gate_clearing_sr_pbar(n_obs, n_trials)
    a = _k_closed(n_trials)
    n_zero = int(round(n_obs * zero_frac))
    n_active = max(n_obs - n_zero, 4)
    dsrs = np.empty(reps)
    for i in range(reps):
        active = _fleishman_sample(n_active, coeffs, rng)
        series = np.concatenate([active, np.zeros(n_zero)]) if n_zero else active
        g3h, g4h, _, _ = _series_moments(series)
        sig, _, _ = full_moment_sigma_sr(srp, n_obs, g3h, g4h)
        dsrs[i] = float(norm.cdf((srp - sig * a) / max(sig, 1e-12)))
    return float(dsrs.std(ddof=1))


# --------------------------------------------------------------------------- #
# Generate the artifact (deterministic).
# --------------------------------------------------------------------------- #
def _content_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k not in ("content_sha256", "provenance")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def generate_bands(git_sha: str = "UNPINNED") -> dict:
    rng = np.random.default_rng(SEED)
    # SOURCE A band, 1-D on n_trials (n_obs-independent by construction). max(raw, floor).
    raw_a, dsr_table = {}, {}
    for nt in DSR_N_TRIALS:
        a = round(gumbel_band(nt, rng), 8)
        raw_a[str(nt)] = a
        dsr_table[str(nt)] = round(max(a, DSR_BAND_FLOOR), 8)
    # SOURCE B bias, 2-D (n_trials, n_obs), SIGNED. Emitted separately; NOT a band.
    bias_table = {f"{nt},{no}": round(dsr_bias_signed(nt, no), 6)
                  for nt in DSR_N_TRIALS for no in DSR_BIAS_N_OBS}
    # PBO band.
    pbo_table = {f"{ns},{nc}": round(max(pbo_band_measure(ns, nc, rng), PBO_BAND_FLOOR), 6)
                 for ns in PBO_N_SLICES for nc in PBO_N_CONFIGS}
    # SOURCE C — corrected-estimator sampling-error band (A1). Keyed kind,n_obs,zero_frac,g3,g4.
    c_table = {}
    for kind, zeros in DSR_C_KIND_ZERO.items():
        for no in DSR_C_N_OBS:
            for zf in zeros:
                for g3 in DSR_C_G3:
                    for g4 in DSR_C_G4:
                        band = dsr_estimator_band_measure(no, zf, g3, g4, rng)
                        c_table[f"{kind},{no},{zf},{g3},{g4}"] = round(max(band, DSR_ESTIMATOR_BAND_FLOOR), 6)

    payload = {
        "schema": "knife_edge_bands/3",
        "gates": {"dsr": DSR_GATE, "pbo": PBO_GATE},
        "floors": {"dsr": DSR_BAND_FLOOR, "pbo": PBO_BAND_FLOOR},
        "dsr_band": {
            "key": "n_trials", "domain": {"n_trials": list(DSR_N_TRIALS)}, "table": dsr_table,
            "raw_source_a": raw_a, "conservative_max": round(max(dsr_table.values()), 8),
            "method": "SOURCE A only: Gumbel/EM approximation error, sigma_SR identical both arms, "
                      "z pinned at 1.645 so sigma_SR cancels -> n_trials-only. Raw Source A < 5e-3 "
                      "floor at every N, so the operational band IS the floor.",
            "note_n_obs": "n_obs-INDEPENDENT by construction (measured + analytic); table is 1-D."},
        "dsr_bias": {
            "key": "n_trials,n_obs", "is_band": False,
            "sign": "negative = Gaussian over-certifies (toward FALSE PASS)",
            "domain": {"n_trials": list(DSR_N_TRIALS), "n_obs": list(DSR_BIAS_N_OBS)},
            "table": bias_table, "planted_non_normality": {"skew_g3": PLANTED_SKEW, "kurtosis_g4": PLANTED_KURTOSIS},
            "method": "SOURCE B: DSR_full - DSR_gauss at the gate (SIGNED). A systematic BIAS, NOT a "
                      "tolerance band -- psd_gates does NOT consume this. PSD S9 estimator-correction."},
        "pbo_band": {
            "key": "n_slices,n_configs", "domain": {"n_slices": list(PBO_N_SLICES),
                                                     "n_configs": list(PBO_N_CONFIGS)},
            "table": pbo_table, "conservative_max": round(max(pbo_table.values()), 6),
            "method": "seeded Monte-Carlo std of CSCV PBO under i.i.d. noise"},
        "dsr_estimator_band": {
            "key": "series_kind,n_obs,zero_frac,g3,g4", "is_band": True, "source": "C",
            "domain": {"series_kind": list(DSR_C_KIND_ZERO), "n_obs": list(DSR_C_N_OBS),
                       "zero_frac_by_kind": {k: list(v) for k, v in DSR_C_KIND_ZERO.items()},
                       "g3": list(DSR_C_G3), "g4": list(DSR_C_G4)},
            "table": c_table, "conservative_max": round(max(c_table.values()), 6),
            "floor": DSR_ESTIMATOR_BAND_FLOOR,
            "method": "SOURCE C (A1 full-moment DSR): seeded-MC std of the corrected DSR at the 0.95 "
                      "gate operating point with the TRUE per-period Sharpe held fixed and only the "
                      "ESTIMATED (gamma3,gamma4) varying across resamples of size n_obs (zero_frac of "
                      "them structural zeros). Fleishman power-method draws hit the target moments. "
                      "Covers the POST-T2 series the verdict's moments are computed on (per_event may "
                      "carry residual zeros to the 50% T2 boundary; trade_level/daily are dense). "
                      "CONSUMED as Source A + Source C (STRAIGHT SUM) -- independent error sources on "
                      "one verdict; max under-covers when both are material, RSS assumes an "
                      "undemonstrated independence structure, sum is conservative."},
        "provenance": {"seed": SEED, "gumbel_mc_reps": GUMBEL_MC_REPS, "n_monte_carlo": N_MONTE_CARLO,
                       "dsr_c_mc_reps": DSR_C_MC_REPS, "dsr_c_n_trials_ref": DSR_C_N_TRIALS_REF,
                       "generating_test_git_sha": git_sha},
    }
    payload["content_sha256"] = _content_hash(payload)
    return payload


def write_bands(path: Path = ARTIFACT_PATH, git_sha: str = "UNPINNED") -> dict:
    payload = generate_bands(git_sha=git_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


# --------------------------------------------------------------------------- #
# LOADER + the four contracts (consumed by psd_gates). dsr_band is 1-D on n_trials.
# --------------------------------------------------------------------------- #
class BandDomainError(RuntimeError):
    """CONTRACT (2): verdict N above the artifact domain -> HARD STOP, regenerate with an extended
    domain. NEVER conservative_max (it would UNDER-protect, and PSD S8 N only grows)."""


class BandRatchetError(RuntimeError):
    """CONTRACT (4): a re-measurement may only WIDEN a band; narrowing HARD STOPs (SFD 4.2)."""


def check_ratchet(old: dict, new: dict, tol: float = 1e-9) -> list[str]:
    violations = []
    for fam in ("dsr_band", "pbo_band", "dsr_estimator_band"):    # CONTRACT 4 covers Source C too (A1)
        ot, nt = old.get(fam, {}).get("table", {}), new.get(fam, {}).get("table", {})
        for key, ov in ot.items():
            nv = nt.get(key)
            if nv is not None and nv < ov - tol:
                violations.append(f"{fam}[{key}] narrowed {ov} -> {nv}")
    return violations


def load_bands(path: Path = ARTIFACT_PATH) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if payload.get("content_sha256") == _content_hash(payload) else None
    except (OSError, ValueError):
        return None


def _bucket_up(value: int, buckets: list[int]) -> int | None:
    for b in sorted(buckets):
        if b >= value:
            return b
    return None


def dsr_band_for(n_trials: int, payload: dict | None) -> dict:
    """SOURCE-A band, 1-D on n_trials (n_obs is NOT a band coordinate). FLOOR_FALLBACK when absent/corrupt;
    round-UP within domain; BandDomainError above domain."""
    if payload is None:
        return {"band": DSR_BAND_FLOOR, "band_source": "FLOOR_FALLBACK"}
    d = payload["dsr_band"]
    nt = _bucket_up(n_trials, d["domain"]["n_trials"])
    if nt is None:
        raise BandDomainError(
            f"n_trials={n_trials} exceeds dsr_band domain max {max(d['domain']['n_trials'])}; regenerate.")
    return {"band": max(d["table"][str(nt)], DSR_BAND_FLOOR), "band_source": "MEASURED"}


def pbo_band_for(n_slices: int, n_configs: int, payload: dict | None) -> dict:
    if payload is None:
        return {"band": PBO_BAND_FLOOR, "band_source": "FLOOR_FALLBACK"}
    d = payload["pbo_band"]
    ns = _bucket_up(n_slices, d["domain"]["n_slices"]); nc = _bucket_up(n_configs, d["domain"]["n_configs"])
    if ns is None or nc is None:
        raise BandDomainError(f"(n_slices={n_slices}, n_configs={n_configs}) exceeds pbo_band domain; regenerate.")
    return {"band": max(d["table"].get(f"{ns},{nc}", d["conservative_max"]), PBO_BAND_FLOOR),
            "band_source": "MEASURED"}


def _nearest_grid(value: float, grid) -> float:
    return min(grid, key=lambda g: abs(g - value))


def dsr_estimator_band_for(series_kind: str, n_obs: int, zero_frac: float, g3: float, g4: float,
                           payload: dict | None) -> dict:
    """SOURCE-C band lookup (A1). EXACT-KEY-OR-CONSERVATIVE-MAX, never interpolates. n_obs rounds UP
    (more data than a bucket -> the wider, safer smaller-n band never applies; use the bucket at/above);
    zero_frac/g3/g4 snap to the NEAREST grid node (conservative_max backs any miss). FLOOR_FALLBACK
    when the artifact is absent/corrupt (never zero). BandDomainError above the n_obs domain."""
    if payload is None or "dsr_estimator_band" not in payload:
        return {"band": DSR_ESTIMATOR_BAND_FLOOR, "band_source": "FLOOR_FALLBACK"}
    d = payload["dsr_estimator_band"]
    dom = d["domain"]
    kind = series_kind if series_kind in dom["series_kind"] else "per_event"
    no = _bucket_up(n_obs, dom["n_obs"])
    if no is None:
        raise BandDomainError(f"n_obs={n_obs} exceeds dsr_estimator_band domain max {max(dom['n_obs'])}; regenerate.")
    zf = _nearest_grid(zero_frac, dom["zero_frac_by_kind"].get(kind, [0.0]))
    gg3 = _nearest_grid(g3, dom["g3"]); gg4 = _nearest_grid(g4, dom["g4"])
    key = f"{kind},{no},{zf},{gg3},{gg4}"
    band = d["table"].get(key, d["conservative_max"])
    return {"band": max(band, d.get("floor", DSR_ESTIMATOR_BAND_FLOOR)),
            "band_source": "MEASURED" if key in d["table"] else "CONSERVATIVE_MAX", "key": key}


def full_moment_live(payload: dict | None) -> dict:
    """A1 retirement condition for the interim `DSR >= 0.95 + |dsr_bias|` posture. FAIL-CLOSED: the
    interim margin retires ONLY when BOTH hold, machine-checked (never hand-managed):

      (i)  the verdict path consumes deflated_sharpe_ratio_full EXCLUSIVELY -- the deprecated Gaussian
           deflated_sharpe_ratio is structurally unreachable from it (import-direction contract,
           asserted by the marker below); AND
      (ii) Source C is present and measured with floor + ratchet metadata in the artifact.

    A schema/band_source check ALONE is insufficient (ruling): retirement requires the fail-closed API
    condition (i) too. Returns {live, verdict_path_exclusive, source_c_ready, reason}."""
    from src.research.psd import verdict_dsr_contract as vc     # local import: the marker module
    exclusive = bool(getattr(vc, "VERDICT_PATH_FULL_MOMENT_EXCLUSIVE", False))
    c = (payload or {}).get("dsr_estimator_band") if payload else None
    source_c_ready = bool(c and c.get("table") and "floor" in c and c.get("source") == "C")
    live = exclusive and source_c_ready
    reason = ("live" if live else
              "; ".join(x for x in [
                  None if exclusive else "verdict path not yet full-moment-exclusive (contract (i))",
                  None if source_c_ready else "Source C band not present/measured (contract (ii))"] if x))
    return {"live": live, "verdict_path_exclusive": exclusive,
            "source_c_ready": source_c_ready, "reason": reason}


if __name__ == "__main__":
    import subprocess
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except Exception:
        sha = "UNPINNED"
    p = write_bands(git_sha=sha)
    print(f"wrote {ARTIFACT_PATH} sha={p['content_sha256'][:12]} dsr_band_max={p['dsr_band']['conservative_max']} "
          f"(Source A raw < floor -> band=floor) pbo_max={p['pbo_band']['conservative_max']}")
