"""VTD DiagnosticReport + hypothesis-quarantine -- research-facing API
(doctrine section 4, the one-way ratchet).

Every Stage 1-5 run emits a structured DiagnosticReport (append-only SQLite via
the existing AsyncDBWriter). Attribution findings are not acted on directly; they
are written as QUARANTINED hypotheses, testable only via a NEW pre-registered
experiment on data the finding has not touched (legal channel a). Each quarantined
hypothesis carries its generation count within the emitting report, so a
quarantine-born experiment's multiplicity N can include its siblings from the same
report (adversarial audit #3 -- quarantine laundering mitigation).

Budget keys are (instrument, period-window) and TIMEFRAME-AGNOSTIC (doctrine
section 5.5): rerunning on 30m bars of the same period is not new data, so 15m
and 30m over the same window map to the SAME budget key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.persistence.db_queue import (
    enqueue_diagnostic_report, enqueue_quarantined_hypothesis,
)
from src.persistence.diagnostic_report_store import (
    insert_diagnostic_report, insert_quarantined_hypothesis,
    read_budget_confirmatory_count, read_report_count, read_sibling_count,
)

VALID_STAGES = frozenset({"1", "2", "3", "4", "5"})

# The emission SCHEMA version (P1). Every persisted report carries it so the
# dashboard can adapt to producers, not vice versa: additive changes keep "v1";
# a breaking change must bump to "v2". Documented in docs/methodology/DIAGNOSTIC_EMISSION_SCHEMA.md.
DIAGNOSTIC_SCHEMA_VERSION = "v1"

# Verdict-time-only emission (SFD 4.5). A DiagnosticReport is EVIDENCE and may be
# emitted ONLY once a terminal verdict exists — never from a Phase-A / blind
# context (where the verdict is not yet known). These sentinels name a non-terminal
# context; emit_report / emit_report_async refuse them so a blind run cannot leak a
# report into the append-only evidence store.
_BLIND_VERDICTS = frozenset({"", "PHASE_A", "PHASE-A", "BLIND", "PENDING", "NONE"})


class PhaseAEmissionError(RuntimeError):
    """Raised when emit is called from a Phase-A/blind context (non-terminal verdict).
    Emission is verdict-time only (SFD 4.5): evidence is produced when a verdict is,
    never during the blind phase."""


def _assert_verdict_time(report: "DiagnosticReport") -> None:
    v = str(report.verdict).strip().upper()
    if v in _BLIND_VERDICTS:
        raise PhaseAEmissionError(
            f"emit refused: verdict {report.verdict!r} is a Phase-A/blind context — "
            "DiagnosticReports are emitted at VERDICT time only (SFD 4.5), never blind."
        )


def budget_key(instrument: str, period_start: str, period_end: str) -> str:
    """Canonical (instrument, period-window) budget key, timeframe-agnostic.

    Deliberately excludes timeframe: 15m and 30m bars over the same
    (instrument, start, end) window share one budget key, so resampling cannot
    be used to circumvent the per-window trial budget (adversarial audit #5)."""
    return f"{instrument.strip().upper()}|{str(period_start).strip()}|{str(period_end).strip()}"


@dataclass
class DiagnosticReport:
    exp_id: str
    leg_id: str
    stage: str
    verdict: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    psd_flags: list | dict = field(default_factory=list)
    cost_observations: list | dict = field(default_factory=dict)
    budget_key: str | None = None
    budget_state: dict[str, Any] = field(default_factory=dict)
    # SCHEMA v1 additions (P1 + full replayability): the version stamp, the seeds that
    # generated every stochastic figure in `diagnostics` (MCPT/bootstrap/PBO), and the
    # content hashes of the source artifacts, so any report can be replayed byte-for-byte.
    schema_version: str = DIAGNOSTIC_SCHEMA_VERSION
    seeds: dict[str, Any] = field(default_factory=dict)
    artifact_hashes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if str(self.stage) not in VALID_STAGES:
            raise ValueError(f"stage must be one of {sorted(VALID_STAGES)}, got {self.stage!r}")

    def as_payload(self) -> dict:
        return {
            "exp_id": self.exp_id, "leg_id": self.leg_id, "stage": str(self.stage),
            "verdict": self.verdict, "diagnostics": self.diagnostics,
            "psd_flags": self.psd_flags, "cost_observations": self.cost_observations,
            "budget_key": self.budget_key, "budget_state": self.budget_state,
            "schema_version": self.schema_version, "seeds": self.seeds,
            "artifact_hashes": self.artifact_hashes,
        }


def emit_report(report: DiagnosticReport, db_path: str | Path) -> int:
    """Append a report synchronously, returning its report id (needed to attach
    quarantined hypotheses). The append-only store never overwrites.

    Verdict-time only (SFD 4.5): refuses a Phase-A/blind verdict — evidence is emitted
    when a verdict exists, never during the blind phase."""
    _assert_verdict_time(report)
    return insert_diagnostic_report(report.as_payload(), db_path)


def emit_report_async(report: DiagnosticReport, *, db_path: str | None = None) -> None:
    """Production path: enqueue the report through the AsyncDBWriter (no id
    returned, fire-and-forget, append-only).

    Verdict-time only (SFD 4.5): refuses a Phase-A/blind verdict."""
    _assert_verdict_time(report)
    enqueue_diagnostic_report(report.as_payload(), db_path=db_path)


def hash_artifacts(**named_objs: Any) -> dict[str, str]:
    """Stable content hashes of a report's SOURCE artifacts (returns series, grid, config).
    A reader can detect whether the underlying data changed since the report was emitted —
    the replay substrate SCHEMA v1 promises. Deterministic: sorted-keys JSON, sha256[:16]."""
    import hashlib
    import json
    out: dict[str, str] = {}
    for name, obj in named_objs.items():
        blob = json.dumps(obj, sort_keys=True, default=str).encode()
        out[name] = "sha256:" + hashlib.sha256(blob).hexdigest()[:16]
    return out


def build_verdict_report(
    *, exp_id: str, leg_id: str, stage: str, verdict: str,
    diagnostics: dict[str, Any], seeds: dict[str, Any], artifact_hashes: dict[str, Any],
    psd_flags: list | dict | None = None, cost_observations: list | dict | None = None,
    budget_key: str | None = None, budget_state: dict[str, Any] | None = None,
) -> DiagnosticReport:
    """Construct a SCHEMA-v1 DiagnosticReport at a runner's VERDICT site from the statistics
    it already computed inline (M1-W). `seeds` + `artifact_hashes` are MANDATORY (replayability).
    The verdict-time guard fires in emit_report*; a blind/Phase-A verdict raises there."""
    if not seeds:
        raise ValueError("build_verdict_report: seeds are mandatory (SCHEMA v1 replayability)")
    if not artifact_hashes:
        raise ValueError("build_verdict_report: artifact_hashes are mandatory (SCHEMA v1 replayability)")
    return DiagnosticReport(
        exp_id=exp_id, leg_id=leg_id, stage=str(stage), verdict=verdict,
        diagnostics=diagnostics, psd_flags=psd_flags or [],
        cost_observations=cost_observations or {}, budget_key=budget_key,
        budget_state=budget_state or {}, seeds=seeds, artifact_hashes=artifact_hashes,
    )


def quarantine_hypothesis(
    source_exp_id: str,
    source_report_id: int,
    description: str,
    db_path: str | Path,
    *,
    status: str = "QUARANTINED",
) -> str:
    """Write an attribution finding as a QUARANTINED hypothesis (legal channel a).
    generation_count_within_report is assigned automatically (continuing the
    report's existing family). Returns the hypothesis_id."""
    return insert_quarantined_hypothesis(
        {
            "source_exp_id": source_exp_id, "source_report_id": int(source_report_id),
            "description": description, "status": status,
        },
        db_path,
    )


def quarantine_hypothesis_async(
    source_exp_id: str,
    source_report_id: int,
    description: str,
    generation_count_within_report: int,
    *,
    status: str = "QUARANTINED",
    db_path: str | None = None,
) -> None:
    """Production async path: the generation count must be supplied (the writer
    drain does not read back state to assign it)."""
    enqueue_quarantined_hypothesis(
        {
            "source_exp_id": source_exp_id, "source_report_id": int(source_report_id),
            "description": description, "status": status,
            "generation_count_within_report": int(generation_count_within_report),
        },
        db_path=db_path,
    )


def sibling_count(report_id: int, db_path: str | Path) -> int:
    """Sibling-family size for a report (all quarantined hypotheses born from it).
    Multiplicity N for a quarantine-born experiment includes this count."""
    return read_sibling_count(report_id, db_path)


def report_count(db_path: str | Path, *, exp_id: str | None = None) -> int:
    return read_report_count(db_path, exp_id=exp_id)


def budget_confirmatory_count(instrument: str, period_start: str, period_end: str,
                              db_path: str | Path) -> int:
    """How many confirmatory reports have been spent on a (instrument, window)
    budget, timeframe-agnostic (channel d alpha-spending)."""
    return read_budget_confirmatory_count(
        budget_key(instrument, period_start, period_end), db_path)
