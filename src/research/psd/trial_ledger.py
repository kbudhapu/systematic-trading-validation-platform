"""PSD trial-accounting ledger -- research-facing API (doctrine S8).

An append-only per-leg ledger recording each selection attempt's trial count
(grid_points x timeframes x objective_variants), cumulative across queues, so
DSR calls can charge the true N_trials. Writes route through the existing
AsyncDBWriter (`enqueue_trial_ledger`) -- no new writer thread; the persistence
layer (`src/persistence/trial_ledger_store.py`) only ever INSERTs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.persistence.db_queue import enqueue_trial_ledger
from src.persistence.trial_ledger_store import (
    persist_trial_rows, read_cumulative_n_trials, read_entry_count,
)


@dataclass(frozen=True)
class TrialLedgerEntry:
    leg_id: str
    queue_id: str
    grid_points_evaluated: int
    timeframes_evaluated: int
    objective_variants: int = 1   # doctrine S8: MUST be 1 (no objective shopping)

    def __post_init__(self) -> None:
        if self.objective_variants != 1:
            raise ValueError("doctrine S8: objective_variants must be 1 (no objective-function shopping)")
        if self.grid_points_evaluated <= 0 or self.timeframes_evaluated <= 0:
            raise ValueError("grid_points_evaluated and timeframes_evaluated must be positive")

    def as_payload(self) -> dict:
        return {
            "leg_id": self.leg_id, "queue_id": self.queue_id,
            "grid_points_evaluated": self.grid_points_evaluated,
            "timeframes_evaluated": self.timeframes_evaluated,
            "objective_variants": self.objective_variants,
        }


def append_trial(entry: TrialLedgerEntry, *, db_path: str | None = None) -> None:
    """Enqueue an append through the AsyncDBWriter (production path)."""
    enqueue_trial_ledger(entry.as_payload(), db_path=db_path)


def append_trial_sync(entry: TrialLedgerEntry, db_path: str | Path) -> None:
    """Synchronous append (test/offline path) -- same append-only INSERT the
    writer's drain uses, without the background writer loop."""
    persist_trial_rows([entry.as_payload()], db_path)


def cumulative_n_trials(leg_id: str, db_path: str | Path) -> int:
    """Cumulative N_trials for a leg, for the DSR call (doctrine S8)."""
    return read_cumulative_n_trials(leg_id, db_path)


def entry_count(leg_id: str, db_path: str | Path) -> int:
    return read_entry_count(leg_id, db_path)
