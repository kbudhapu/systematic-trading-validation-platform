"""FIX-5 (RA-POLLUTION): quarantine the phantom (test-written, qty=0) trade rows.

The `trades` table held only pre-R2.5 test pollution (61 rows, all qty=0, no backing order) that
every consumer read as if real (RA-0). This establishes the first clean phantom-vs-real boundary.

Design (confirmed): ADDITIVE-ONLY. A separate `trade_quarantine` table references the phantom
trade ids; the `trades` rows are NEVER modified (they remain byte-identical RA-0 evidence).
Idempotent (`INSERT OR IGNORE`) and reversible (drop the tag rows).

Tagging is by RA-0 two-source SIGNATURE, never by id range: a row is phantom iff `qty = 0` AND it
has no correlated FILLED order (broker echo). A real fill (`qty > 0` AND a filled order) fails the
signature, so this migration STRUCTURALLY CANNOT quarantine a real fill, whenever it runs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS trade_quarantine (
    trade_id    INTEGER PRIMARY KEY,   -- references trades.id; additive, never modifies trades
    reason      TEXT NOT NULL,
    tagged_utc  TEXT NOT NULL
);
"""

# RA-0 two-source phantom signature (t = the trades row). qty=0 AND no correlated broker fill.
_PHANTOM_SIGNATURE = """
    t.qty = 0
    AND NOT EXISTS (
        SELECT 1 FROM orders o
        WHERE o.symbol = t.symbol
          AND o.strategy_id = t.strategy_id
          AND o.filled_price IS NOT NULL
    )
"""

# Exclusion clause for readers that must not count phantom rows as real.
QUARANTINE_EXCLUSION = "id NOT IN (SELECT trade_id FROM trade_quarantine)"


def ensure_trade_quarantine_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def count_phantom_signature(db_path: str | Path) -> int:
    """Read-only: how many trades match the phantom signature right now."""
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM trades t WHERE {_PHANTOM_SIGNATURE}").fetchone()[0])


def quarantine_phantom_trades(db_path: str | Path) -> dict:
    """Tag every signature-matched phantom trade. Additive, idempotent. Returns a summary and
    ASSERTS it never modified a trades row (fingerprint before/after must match)."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        ensure_trade_quarantine_schema(conn)
        before_fp = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(qty),0), COALESCE(SUM(COALESCE(pnl,0)),0) FROM trades"
        ).fetchone()
        matched = [r[0] for r in conn.execute(
            f"SELECT t.id FROM trades t WHERE {_PHANTOM_SIGNATURE}").fetchall()]
        conn.executemany(
            "INSERT OR IGNORE INTO trade_quarantine (trade_id, reason, tagged_utc) VALUES (?,?,?)",
            [(tid, "RA-POLLUTION: qty=0 phantom, no filled order (RA-0 two-source signature)", now)
             for tid in matched],
        )
        after_fp = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(qty),0), COALESCE(SUM(COALESCE(pnl,0)),0) FROM trades"
        ).fetchone()
        tagged_total = int(conn.execute("SELECT COUNT(*) FROM trade_quarantine").fetchone()[0])
        real_remaining = int(conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE {QUARANTINE_EXCLUSION}").fetchone()[0])
    # HARD invariant: the migration must not have touched any trades row.
    assert before_fp == after_fp, f"trades table was modified: {before_fp} -> {after_fp}"
    return {
        "signature_matched": len(matched),
        "quarantine_total": tagged_total,
        "real_trades_remaining": real_remaining,
        "trades_fingerprint": {"before": before_fp, "after": after_fp, "unchanged": True},
    }
