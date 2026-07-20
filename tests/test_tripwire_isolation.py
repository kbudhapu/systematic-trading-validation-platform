"""RA-TRIPWIRE fix — validate the two-layer test-DB isolation guard (conftest).

Layer 1 (path-assertion) is the always-on guarantee, immune to concurrent writers;
Layer 2 (content hash) strict-asserts only when no live writer is present, and is
loudly skipped when one is.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from tests.conftest import (
    _REAL_DATA_DIR,
    _evaluate_tripwire,
    _guard_db_path,
    _resolve_db_path,
)


# --------------------------------------------------------------------------- #
# (a) Layer 1 catches a simulated escape EVEN WHILE a live writer pounds a DB
# --------------------------------------------------------------------------- #
def test_path_assertion_catches_production_open_under_concurrent_writer(tmp_path) -> None:
    stop = threading.Event()
    writer_db = tmp_path / "fake_live_writer.db"

    def _pound() -> None:
        con = sqlite3.connect(writer_db)          # tmp path -> allowed by the guard
        con.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        con.commit()
        while not stop.is_set():
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
            time.sleep(0.001)
        con.close()

    writer = threading.Thread(target=_pound, daemon=True)
    writer.start()
    try:
        # The escape: opening a REAL production DB path must raise regardless of the
        # concurrent writer (Layer 1 is content-agnostic).
        for name in ("trading.db", "research_vault.db"):
            with pytest.raises(AssertionError, match="ISOLATION BREACH"):
                _guard_db_path(_REAL_DATA_DIR / name)
        # A tmp path (the redirected DATA_DIR) is allowed.
        _guard_db_path(tmp_path / "isolated.db")     # no raise
    finally:
        stop.set()
        writer.join(timeout=3)


def test_path_guard_allows_memory_and_ignores_nonpath() -> None:
    assert _resolve_db_path(":memory:") is None
    assert _resolve_db_path("file::memory:?cache=shared") is None
    assert _resolve_db_path(None) is None
    _guard_db_path(":memory:")          # no raise
    _guard_db_path(None)                # no raise


# --------------------------------------------------------------------------- #
# (b) legitimate concurrent writer + clean (tmp-only) tests -> no trip
# --------------------------------------------------------------------------- #
def test_clean_tmp_writes_do_not_trip_guard_under_concurrent_writer(tmp_path) -> None:
    stop = threading.Event()

    def _pound() -> None:
        con = sqlite3.connect(tmp_path / "writer.db")
        con.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        while not stop.is_set():
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
            time.sleep(0.001)
        con.close()

    writer = threading.Thread(target=_pound, daemon=True)
    writer.start()
    try:
        # A normal test doing isolated tmp work must not raise.
        con = sqlite3.connect(tmp_path / "clean.db")
        con.execute("CREATE TABLE IF NOT EXISTS ok (v INTEGER)")
        con.execute("INSERT INTO ok VALUES (42)")
        con.commit()
        assert con.execute("SELECT v FROM ok").fetchone()[0] == 42
        con.close()
    finally:
        stop.set()
        writer.join(timeout=3)


# --------------------------------------------------------------------------- #
# (c) Layer 2: no live writer + hash mismatch -> strict breach; live writer -> skip
# --------------------------------------------------------------------------- #
def test_hash_layer_strict_when_no_live_writer() -> None:
    verdict, changed = _evaluate_tripwire(
        {"trading.db": "aaa"}, {"trading.db": "bbb"}, live_writer=False
    )
    assert verdict == "breach"
    assert changed == ["trading.db"]


def test_hash_layer_skips_when_live_writer() -> None:
    verdict, changed = _evaluate_tripwire(
        {"trading.db": "aaa"}, {"trading.db": "bbb"}, live_writer=True
    )
    assert verdict == "skip"
    assert changed == ["trading.db"]


def test_hash_layer_ok_when_unchanged() -> None:
    verdict, changed = _evaluate_tripwire(
        {"trading.db": "aaa"}, {"trading.db": "aaa"}, live_writer=False
    )
    assert verdict == "ok"
    assert changed == []
