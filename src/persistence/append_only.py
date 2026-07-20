"""AppendOnlyStore base (E3/R2, F8).

The common shape of the append-only clone stores: a fixed DDL, a schema-ensure,
an INSERT-only append of an ordered column set, and a `permanent` flag the
retention policy honors (permanent stores are never pruned). The pure-INSERT
stores (heartbeat, alerts, reconciliation) compose this base so the pattern is
STRUCTURE, not copy-paste discipline. Stores with derived columns (trial ledger's
n_trials, quarantine's generation counts, the diagnostic-report json shaping) keep
their custom persist logic and adopt this base incrementally.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path


class AppendOnlyStore:
    """A table that is only ever INSERTed into (never UPDATE/DELETE here)."""

    def __init__(self, *, table: str, ddl: str, insert_columns: Sequence[str],
                 permanent: bool = True, timestamp_column: str | None = "created_utc") -> None:
        self.table = table
        self._ddl = ddl
        self._columns = list(insert_columns)
        self.permanent = permanent
        self._ts_col = timestamp_column

    def ensure_schema(self, db_path: str | Path) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(p) as conn:
            conn.executescript(self._ddl)

    def append(self, rows: Sequence[dict], db_path: str | Path) -> None:
        """INSERT the given rows (each a dict keyed by insert column). A
        timestamp_column absent from a row is filled with now(UTC)."""
        if not rows:
            return
        self.ensure_schema(db_path)
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in self._columns)
        values = [
            tuple(
                (r.get(c, now) if c == self._ts_col else r.get(c))
                for c in self._columns
            )
            for r in rows
        ]
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                f"INSERT INTO {self.table} ({','.join(self._columns)}) VALUES ({placeholders})",
                values,
            )

    def count(self, db_path: str | Path) -> int:
        self.ensure_schema(db_path)
        with sqlite3.connect(db_path) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0])
