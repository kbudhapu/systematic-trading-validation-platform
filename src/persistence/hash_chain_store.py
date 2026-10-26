"""SQLite persistence for the LLD config-hash chain (LLD section 3).

Append-only lineage of a leg's configuration leaves. Each refit produces a new
LEAF hash whose parent_hash is the prior leaf, so the chain is a verifiable
lineage. A parameter change arriving OUTSIDE the refit runner leaves no chain
entry and is detectable as out-of-band. Rows are only ever INSERTed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS config_hash_chain (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id             TEXT NOT NULL,
    epoch              INTEGER NOT NULL,
    parent_hash        TEXT,
    leaf_hash          TEXT NOT NULL,
    refit_evidence_ref TEXT,
    created_utc        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_hash_chain_leg ON config_hash_chain(leg_id);
"""


def ensure_hash_chain_schema(db_path: str | Path) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(_DDL)


def persist_chain_entries(payloads: Sequence[dict], db_path: str | Path) -> None:
    """Append-only INSERT of one or more chain entries."""
    if not payloads:
        return
    ensure_hash_chain_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (str(p["leg_id"]), int(p["epoch"]),
         (str(p["parent_hash"]) if p.get("parent_hash") is not None else None),
         str(p["leaf_hash"]),
         (str(p["refit_evidence_ref"]) if p.get("refit_evidence_ref") is not None else None),
         now)
        for p in payloads
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO config_hash_chain
               (leg_id, epoch, parent_hash, leaf_hash, refit_evidence_ref, created_utc)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )


def read_chain(leg_id: str, db_path: str | Path) -> list[dict]:
    """All chain entries for a leg, oldest first."""
    ensure_hash_chain_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT leg_id, epoch, parent_hash, leaf_hash, refit_evidence_ref, created_utc
               FROM config_hash_chain WHERE leg_id = ? ORDER BY id ASC""",
            (str(leg_id),),
        ).fetchall()
    return [
        {"leg_id": r[0], "epoch": r[1], "parent_hash": r[2], "leaf_hash": r[3],
         "refit_evidence_ref": r[4], "created_utc": r[5]}
        for r in rows
    ]


def read_latest_leaf(leg_id: str, db_path: str | Path) -> str | None:
    ensure_hash_chain_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT leaf_hash FROM config_hash_chain WHERE leg_id = ? ORDER BY id DESC LIMIT 1",
            (str(leg_id),),
        ).fetchone()
    return row[0] if row else None
