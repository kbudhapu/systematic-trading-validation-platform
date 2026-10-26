"""PSD trial-ledger tests: append-only, cumulative arithmetic, concurrent safety."""
from __future__ import annotations

import threading

import pytest

from src.research.psd.trial_ledger import (
    TrialLedgerEntry, append_trial_sync, cumulative_n_trials, entry_count,
)
from src.persistence.trial_ledger_store import persist_trial_rows, read_cumulative_n_trials


def test_objective_variants_must_be_one() -> None:
    with pytest.raises(ValueError):
        TrialLedgerEntry("qqq", "q1", 100, 1, objective_variants=2)


def test_append_only_two_appends_two_rows_cumulative_sums(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    append_trial_sync(TrialLedgerEntry("qqq", "q1", grid_points_evaluated=100, timeframes_evaluated=1), db)
    append_trial_sync(TrialLedgerEntry("qqq", "q2", grid_points_evaluated=50, timeframes_evaluated=2), db)
    # two distinct rows (append-only, not overwrite); cumulative = 100*1 + 50*2 = 200
    assert entry_count("qqq", db) == 2
    assert cumulative_n_trials("qqq", db) == 200
    # a second leg is isolated
    append_trial_sync(TrialLedgerEntry("spy", "q1", grid_points_evaluated=30, timeframes_evaluated=1), db)
    assert cumulative_n_trials("spy", db) == 30
    assert cumulative_n_trials("qqq", db) == 200


def test_no_update_path_reappending_same_queue_appends(tmp_path) -> None:
    """Re-appending for the same (leg, queue) must ADD a row, never overwrite --
    proving append-only (there is no update API)."""
    db = tmp_path / "ledger.db"
    e = TrialLedgerEntry("qqq", "q1", grid_points_evaluated=64, timeframes_evaluated=1)
    append_trial_sync(e, db)
    append_trial_sync(e, db)
    assert entry_count("qqq", db) == 2
    assert cumulative_n_trials("qqq", db) == 128


def test_concurrent_writes_all_land(tmp_path) -> None:
    """Concurrent appends through the persistence path (SQLite serialization) must
    all land with no lost/corrupt rows."""
    db = tmp_path / "ledger.db"
    n = 40

    def worker(i: int) -> None:
        persist_trial_rows(
            [{"leg_id": "qqq", "queue_id": f"c{i}", "grid_points_evaluated": 10,
              "timeframes_evaluated": 1, "objective_variants": 1}], db)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert entry_count("qqq", db) == n
    assert read_cumulative_n_trials("qqq", db) == n * 10


def test_writer_dispatch_flush_appends(tmp_path) -> None:
    """The AsyncDBWriter's TRIAL_LEDGER flush handler appends via the store."""
    from src.persistence.db_queue import AsyncDBWriter, QueuedWrite, WriteKind
    db = str(tmp_path / "ledger.db")
    w = AsyncDBWriter()
    items = [QueuedWrite(kind=WriteKind.TRIAL_LEDGER,
                         payload={"leg_id": "qqq", "queue_id": "q1",
                                  "grid_points_evaluated": 100, "timeframes_evaluated": 2,
                                  "objective_variants": 1}, db_path=db)]
    from pathlib import Path
    assert w._flush_trial_ledger_batch(items, Path(db)) is True
    assert cumulative_n_trials("qqq", db) == 200
