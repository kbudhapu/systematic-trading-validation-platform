"""
Regression test: SQLite readonly-database error handling.

Covers the 2026-06-26 production incident where research_vault.db was owned
by root while the service ran as a different user. Every cycle call to any
persistence write function raised:

    sqlite3.OperationalError: attempt to write a readonly database

The existing exception handler (bare `except Exception`) treated this
identically to a transient network error, incrementing the same consecutive-
failure counter until SOFT_DEGRADE was applied — with no dedicated alert or
permanent-error classification. The bot retried indefinitely, never recovering,
while the dashboard showed $0 because no snapshots could be written.

Note on test environment: the CI container runs as root, so filesystem chmod
restrictions are not enforced. Tests that need to confirm OperationalError
propagation must inject the error via monkeypatching rather than relying on
OS-level file permissions.

FINDING (see end of module): the current handler does NOT distinguish a
permanent configuration error (wrong file ownership) from a transient error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.persistence.cycle_metrics_store as cms_module
from src.persistence.cycle_metrics_store import (
    ensure_cycle_metrics_schema,
    persist_cycle_metrics_row,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_READONLY_ERROR = sqlite3.OperationalError("attempt to write a readonly database")
_real_connect = sqlite3.connect  # captured before any patching


def _injecting_connect(db_path: Path):
    """
    sqlite3.connect replacement that allows schema creation (schema DDL only
    uses CREATE TABLE IF NOT EXISTS which are no-ops on a pre-existing schema)
    but raises the production readonly error on the first INSERT or BEGIN
    IMMEDIATE write transaction.
    """
    real_conn = _real_connect(db_path)

    class _ReadonlyWriteConn:
        """Proxy that passes schema reads through but blocks write transactions."""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            real_conn.close()

        def executescript(self, sql: str) -> None:
            real_conn.executescript(sql)

        def execute(self, sql: str, params=()) -> MagicMock:
            stripped = sql.strip().upper()
            if stripped.startswith("BEGIN") or stripped.startswith("INSERT"):
                raise _READONLY_ERROR
            return real_conn.execute(sql, params)

        def close(self):
            real_conn.close()

    return _ReadonlyWriteConn()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_persist_raises_operational_error_on_readonly_db(tmp_path: Path) -> None:
    """
    A read-only DB raises sqlite3.OperationalError('attempt to write a
    readonly database').  The error must propagate out of persist_cycle_metrics_row
    unchanged — not swallowed, not wrapped in a different exception type.
    """
    db_path = tmp_path / "trading.db"
    ensure_cycle_metrics_schema(db_path)

    with patch.object(cms_module.sqlite3, "connect", side_effect=_injecting_connect):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            persist_cycle_metrics_row(
                phase_a_ms=10.0,
                phase_b_ms=8.0,
                phase_c_ms=12.0,
                total_cycle_ms=30.0,
                sieve_backlog_qty=0,
                db_path=db_path,
            )


def test_readonly_error_is_operational_not_generic(tmp_path: Path) -> None:
    """
    Confirms the exception is specifically sqlite3.OperationalError (subclass
    of sqlite3.DatabaseError), NOT a generic Exception or OSError.  This
    matters because the production handler uses bare `except Exception` and
    cannot currently distinguish this permanent error from a transient one.
    """
    db_path = tmp_path / "trading.db"
    ensure_cycle_metrics_schema(db_path)

    exc_raised: BaseException | None = None
    with patch.object(cms_module.sqlite3, "connect", side_effect=_injecting_connect):
        try:
            persist_cycle_metrics_row(
                phase_a_ms=1.0,
                phase_b_ms=1.0,
                phase_c_ms=1.0,
                total_cycle_ms=3.0,
                sieve_backlog_qty=0,
                db_path=db_path,
            )
        except BaseException as e:
            exc_raised = e

    assert exc_raised is not None, "expected an exception but none was raised"
    assert isinstance(exc_raised, sqlite3.OperationalError), (
        f"expected sqlite3.OperationalError, got {type(exc_raised).__name__}: {exc_raised}"
    )
    assert "readonly" in str(exc_raised).lower()


# ---------------------------------------------------------------------------
# FINDING — permanent vs transient error classification
#
# Confirmed in src/engine/orchestrator.py (lines ~3264 and ~4489):
#
#   _run_leg_cycle:
#       except Exception as e:
#           log.error("leg_cycle_failed", leg=..., error=str(e))
#           self.sync.log_system_event("leg_error", ...)
#
#   run() main loop:
#       except Exception as e:
#           self._consecutive_cycle_failures += 1
#           log.error("cycle_failed", error=str(e), streak=...)
#           if self._consecutive_cycle_failures >= 3:
#               dispatch_critical_page(IncidentType.BOOT_BLOCKING_ERROR, ...)
#
# A sqlite3.OperationalError("attempt to write a readonly database") flows
# through both handlers identically to a transient RuntimeError from a
# network blip.  Consequences in the production incident:
#
#   1. The bot retried every TICK_SECONDS (5 s) indefinitely.  The error
#      never self-resolved because it required a manual `chown` on the server.
#   2. After 3 failures, BOOT_BLOCKING_ERROR fired, but the text was the raw
#      error string.  No structured incident type routes to a distinct runbook.
#   3. _consecutive_cycle_failures kept incrementing unboundedly across hours.
#
# Recommended follow-up (not implemented here — requires design work):
#   Catch sqlite3.OperationalError specifically in the persistence call sites
#   and/or in _run_leg_cycle. Classify it as a STORAGE_MISCONFIGURATION
#   incident with explicit remediation text ("Check file ownership: chown
#   trading:trading /path/to/db"). Transition to HARD_CRITICAL (not
#   SOFT_DEGRADE) since the error is permanent until a human acts.
# ---------------------------------------------------------------------------
