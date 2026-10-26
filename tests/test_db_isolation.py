"""R2.5: test-DB isolation invariants.

The conftest sets MBAPPE_DATA_DIR to a per-session tmp dir BEFORE any src import,
so every store path (derived from the single env-aware src.config.DATA_DIR)
resolves under tmp -- no test can write production state. These tests pin the
invariant so it cannot silently regress (e.g. a future store hardcoding a path,
or DATA_DIR losing its env-awareness). The permanent hash tripwire in conftest
(_production_db_tripwire) is the session-level guarantee; these are the unit-level
assertions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import DATA_DIR, DB_PATH, PARQUET_DIR

_REPO_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_isolation_env_var_is_set() -> None:
    assert os.environ.get("MBAPPE_DATA_DIR"), (
        "MBAPPE_DATA_DIR must be set by conftest before any src import"
    )


def test_data_dir_is_redirected_not_production() -> None:
    """The active data root is the tmp session dir, NOT the repo's data/."""
    assert DATA_DIR == Path(os.environ["MBAPPE_DATA_DIR"])
    assert DATA_DIR.resolve() != _REPO_DATA_DIR.resolve()


def test_all_store_paths_derive_from_the_single_root() -> None:
    """Every store path must live under DATA_DIR -- the non-enumerated guarantee:
    a new store that derives from DATA_DIR is covered automatically."""
    from src.engine.governance import CIRCUIT_BREAKER_FILE
    from src.persistence.db import RESEARCH_VAULT_PATH

    for p in (DB_PATH, RESEARCH_VAULT_PATH, PARQUET_DIR, CIRCUIT_BREAKER_FILE):
        assert Path(p).resolve().is_relative_to(DATA_DIR.resolve()), (
            f"store path {p} is not under the redirected DATA_DIR {DATA_DIR} "
            "-- it would leak to production"
        )


def test_default_constructed_stores_write_under_isolation() -> None:
    """A store built with NO explicit db_path (the leak pattern that polluted
    production) must resolve to the isolated dir, not repo data/."""
    from src.engine.degradation_manager import DegradationManager
    from src.engine.governance import HumanOverrideRegistry

    assert Path(DegradationManager().db_path).resolve().is_relative_to(DATA_DIR.resolve())
    assert Path(HumanOverrideRegistry().db_path).resolve().is_relative_to(DATA_DIR.resolve())


def test_writing_kill_state_does_not_touch_repo_data_dir() -> None:
    """End-to-end: engage+release a kill switch and a hard degrade via default
    paths; the repo's real trading.db / research_vault.db must be untouched
    (this is the exact leak the isolation prevents; the session tripwire also
    guards it)."""
    from src.engine.degradation_manager import DegradationManager
    from src.engine.governance import HumanOverrideRegistry, KillLevel

    real_trading = _REPO_DATA_DIR / "trading.db"
    real_vault = _REPO_DATA_DIR / "research_vault.db"
    before = {p: (p.stat().st_mtime_ns if p.exists() else None)
              for p in (real_trading, real_vault)}

    reg = HumanOverrideRegistry()
    reg.engage(KillLevel.PORTFOLIO_HALT, operator="t", rationale="isolation test")
    reg.release(KillLevel.PORTFOLIO_HALT, operator="t", rationale="isolation test")
    DegradationManager().apply_hard_critical_degrade("isolation_test_bar_freshness")

    after = {p: (p.stat().st_mtime_ns if p.exists() else None)
             for p in (real_trading, real_vault)}
    assert before == after, "kill-state writes leaked into the repo's real data/ DBs"
