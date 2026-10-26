"""Research evidence artifact store (M3/M4) — APPEND-ONLY, evidence_class-tagged.

Holds the diagnostic-emission artifacts that carry a full payload the dashboard renders:
MCPT null arrays and per-experiment equity/drawdown curves. Every row carries
`evidence_class` ('original' | 'replication'), SCHEMA v1 fields (schema_version, seed,
artifact_hash), and — per R2 — is NEVER updated or deleted; a replication is ADDED
alongside its original, never a replacement. Chart metadata (overlays: stored-p vs gate,
observed-vs-null) travels with the artifact so the display cannot mislabel it.

Local research vault only. Display-side readable; no generator imports it to READ (the
2.5 firewall) — generators only WRITE their own verdict-time evidence.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "v1"
_VALID_CLASS = frozenset({"original", "replication"})
_VALID_TYPE = frozenset({"mcpt_null", "equity_curve"})

_DDL = """
CREATE TABLE IF NOT EXISTS research_evidence_artifacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    trial_key      TEXT NOT NULL,
    artifact_type  TEXT NOT NULL,          -- 'mcpt_null' | 'equity_curve'
    evidence_class TEXT NOT NULL,          -- 'original' | 'replication'
    schema_version TEXT NOT NULL DEFAULT 'v1',
    seed           INTEGER,
    n_perm         INTEGER,
    stored_p       REAL,                   -- overlay: the verdict's stored p (for the chart)
    artifact_hash  TEXT NOT NULL,          -- sha256[:16] of payload_json
    payload_json   TEXT NOT NULL,          -- the null array / the curve series
    chart_meta_json TEXT NOT NULL DEFAULT '{}',   -- overlays / axis / gate lines
    created_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_exp ON research_evidence_artifacts(experiment_id, trial_key);
CREATE INDEX IF NOT EXISTS ix_evidence_class ON research_evidence_artifacts(evidence_class);
"""


def ensure_evidence_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_DDL)


def _hash(payload_json: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(payload_json.encode()).hexdigest()[:16]


def persist_evidence_artifact(
    *, experiment_id: str, trial_key: str, artifact_type: str, evidence_class: str,
    payload, seed: int | None = None, n_perm: int | None = None,
    stored_p: float | None = None, chart_meta: dict | None = None,
    db_path: str | Path,
) -> int:
    """Append one evidence artifact (never updates/deletes — R2). Returns its row id."""
    if evidence_class not in _VALID_CLASS:
        raise ValueError(f"evidence_class must be {_VALID_CLASS}, got {evidence_class!r}")
    if artifact_type not in _VALID_TYPE:
        raise ValueError(f"artifact_type must be {_VALID_TYPE}, got {artifact_type!r}")
    ensure_evidence_schema(db_path)
    payload_json = json.dumps(payload, default=str)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO research_evidence_artifacts
               (experiment_id, trial_key, artifact_type, evidence_class, schema_version,
                seed, n_perm, stored_p, artifact_hash, payload_json, chart_meta_json, created_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (experiment_id, trial_key, artifact_type, evidence_class, SCHEMA_VERSION,
             seed, n_perm, stored_p, _hash(payload_json), payload_json,
             json.dumps(chart_meta or {}), now),
        )
        return int(cur.lastrowid)


def evidence_counts(db_path: str | Path) -> dict:
    ensure_evidence_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT artifact_type, evidence_class, COUNT(*) FROM research_evidence_artifacts "
            "GROUP BY artifact_type, evidence_class").fetchall()
    return {f"{t}/{c}": n for t, c, n in rows}


def read_all_artifacts(db_path: str | Path) -> list[dict]:
    """Every evidence artifact as a dict (display-side read; the mirror's only artifact source).

    Ordered by id so the append-only mirror is deterministic. payload_json / chart_meta_json
    are returned as raw JSON strings — callers parse verbatim (no recompute in the store)."""
    ensure_evidence_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM research_evidence_artifacts ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
