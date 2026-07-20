"""
Lightweight parameter-sweep guardrails for the adaptive tuner.

Kept separate from scripts/adaptive_parameter_tuner.py so unit tests do not
import Numba, Alpaca clients, or the full sweep pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

MAX_SWEEP_INNER_COMBINATIONS = int(os.getenv("TUNER_MAX_SWEEP_COMBINATIONS", "50000"))
TUNER_MAX_RUNTIME_SECONDS = int(os.getenv("TUNER_MAX_RUNTIME_SECONDS", "3600"))
TUNER_SUBPROCESS_TIMEOUT_SECONDS = TUNER_MAX_RUNTIME_SECONDS + 300


@dataclass(frozen=True)
class SearchGrid:
    regime: str
    sma_long_grid: list[int]
    sma_short_grid: list[int]
    long_grid: np.ndarray
    short_grid: np.ndarray
    exit_grid: np.ndarray
    max_bars_grid: np.ndarray

    @property
    def n_long(self) -> int:
        return len(self.long_grid)

    @property
    def n_short(self) -> int:
        return len(self.short_grid)

    @property
    def n_exit(self) -> int:
        return len(self.exit_grid)

    @property
    def n_max_bars(self) -> int:
        return len(self.max_bars_grid)

    @property
    def n_inner(self) -> int:
        return self.n_long * self.n_short * self.n_exit * self.n_max_bars


def enforce_sweep_grid_budget(search_grid: SearchGrid) -> None:
    if search_grid.n_inner > MAX_SWEEP_INNER_COMBINATIONS:
        raise RuntimeError(
            "parameter sweep grid too large: "
            f"{search_grid.n_inner} > {MAX_SWEEP_INNER_COMBINATIONS}"
        )
