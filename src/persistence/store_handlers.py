"""Clone-store write-handler registrations (E3/R2).

Each append-only clone store's flush handler is registered here so the
AsyncDBWriter dispatch is a generic registry lookup (no per-kind if/elif). Adding
a NEW telemetry kind = add its persist import + one register_write_handler line
here (plus the WriteKind enum member and a thin enqueue helper) -- ZERO edits to
the AsyncDBWriter dispatch/flush code. db_queue imports this module at load so the
handlers are registered before the writer thread drains.
"""

from __future__ import annotations

from pathlib import Path

from src.persistence.db_queue import WriteKind, register_write_handler
from src.persistence.diagnostic_report_store import (
    persist_diagnostic_reports, persist_quarantined_hypotheses,
)
from src.persistence.hash_chain_store import persist_chain_entries
from src.persistence.heartbeat_store import persist_heartbeats
from src.persistence.reconciliation_store import persist_reconciliation_rows
from src.persistence.trial_ledger_store import persist_trial_rows


def _append_handler(persist_fn):
    """Wrap an append-only persist(payloads, db_path) as a (items, path)->bool handler."""
    def handler(items: list, sqlite_path: Path) -> bool:
        persist_fn([item.payload for item in items], sqlite_path)
        return True
    return handler


register_write_handler(WriteKind.TRIAL_LEDGER, _append_handler(persist_trial_rows))
register_write_handler(WriteKind.DIAGNOSTIC_REPORT, _append_handler(persist_diagnostic_reports))
register_write_handler(WriteKind.QUARANTINED_HYPOTHESIS, _append_handler(persist_quarantined_hypotheses))
register_write_handler(WriteKind.HEARTBEAT, _append_handler(persist_heartbeats))
register_write_handler(WriteKind.CONFIG_HASH_CHAIN, _append_handler(persist_chain_entries))
register_write_handler(WriteKind.RECONCILIATION, _append_handler(persist_reconciliation_rows))
