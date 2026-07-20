"""
Pre-flight reconciliation retry scheduling and deferred maintenance recovery.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable

log = structlog.get_logger()

DEFERRED_PRE_OPEN_RECON_LATCH_KEY = "DEFERRED_PRE_OPEN_RECON"
RECON_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 60.0)

DEFERRED_RECON_DDL = """
CREATE TABLE IF NOT EXISTS control_plane_latches (
    latch_key TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT,
    updated_at TEXT NOT NULL
);
"""


def _ensure_latch_schema(db_path: Path) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DEFERRED_RECON_DDL)


def schedule_pre_open_full_recon(
    reason: str,
    *,
    metadata: dict[str, Any] | None = None,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> None:
    from datetime import datetime, timezone

    _ensure_latch_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO control_plane_latches (
                latch_key, active, reason, metadata_json, updated_at
            ) VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(latch_key) DO UPDATE SET
                active = 1,
                reason = excluded.reason,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                DEFERRED_PRE_OPEN_RECON_LATCH_KEY,
                reason,
                json.dumps(metadata or {}, separators=(",", ":")),
                now,
            ),
        )


def is_deferred_pre_open_recon_scheduled(
    db_path: Path = RESEARCH_VAULT_PATH,
) -> bool:
    if not db_path.exists():
        return False
    _ensure_latch_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT active FROM control_plane_latches
            WHERE latch_key = ?
            """,
            (DEFERRED_PRE_OPEN_RECON_LATCH_KEY,),
        ).fetchone()
    return row is not None and int(row[0]) == 1


def clear_deferred_pre_open_recon(db_path: Path = RESEARCH_VAULT_PATH) -> bool:
    if not db_path.exists():
        return False
    from datetime import datetime, timezone

    _ensure_latch_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT active FROM control_plane_latches
            WHERE latch_key = ?
            """,
            (DEFERRED_PRE_OPEN_RECON_LATCH_KEY,),
        ).fetchone()
        if row is None or int(row[0]) != 1:
            return False
        cur = conn.execute(
            """
            UPDATE control_plane_latches
            SET active = 0, updated_at = ?
            WHERE latch_key = ?
            """,
            (now, DEFERRED_PRE_OPEN_RECON_LATCH_KEY),
        )
        # R1 sibling hardening: never report a clear the UPDATE didn't apply.
        if int(cur.rowcount or 0) == 0:
            log.warning("deferred_pre_open_recon_clear_no_row",
                        latch_key=DEFERRED_PRE_OPEN_RECON_LATCH_KEY)
            return False
    return True
