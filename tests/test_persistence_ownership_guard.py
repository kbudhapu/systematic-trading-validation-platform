"""Tests for the persistence ownership guard (Layer 1 fix)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.persistence.ownership_guard import (
    PersistenceOwnershipError,
    _is_exempt_from_root_create_check,
    ensure_db_writable,
)


# ---------------------------------------------------------------------------
# _is_exempt_from_root_create_check
# ---------------------------------------------------------------------------

def test_exempt_under_pytest(tmp_path: Path) -> None:
    # PYTEST_CURRENT_TEST is always set when this test actually runs under
    # pytest — no patching needed; just confirm the function returns True.
    assert "PYTEST_CURRENT_TEST" in os.environ
    assert _is_exempt_from_root_create_check(tmp_path / "db.sqlite") is True


def test_exempt_under_tempdir(tmp_path: Path) -> None:
    # Even with PYTEST_CURRENT_TEST stripped, a tmp_path is under tempdir.
    with patch.dict(os.environ, {}, clear=False):
        env_backup = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            assert _is_exempt_from_root_create_check(tmp_path / "db.sqlite") is True
        finally:
            if env_backup is not None:
                os.environ["PYTEST_CURRENT_TEST"] = env_backup


def test_not_exempt_for_production_path() -> None:
    production_path = Path("/home/user/mbappe/data/trading.db")
    with patch.dict(os.environ, {}, clear=False):
        env_backup = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            assert _is_exempt_from_root_create_check(production_path) is False
        finally:
            if env_backup is not None:
                os.environ["PYTEST_CURRENT_TEST"] = env_backup


# ---------------------------------------------------------------------------
# ensure_db_writable — happy paths
# ---------------------------------------------------------------------------

def test_writable_existing_file_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "ok.db"
    db.touch()
    with patch("os.access", return_value=True):
        ensure_db_writable(db)  # must not raise


@pytest.mark.skipif(os.name != "posix", reason="root/uid semantics are POSIX-only")
def test_absent_file_non_root_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "new.db"
    assert not db.exists()
    with patch("os.geteuid", return_value=1000):
        ensure_db_writable(db)  # must not raise


# ---------------------------------------------------------------------------
# False-positive fix: tmp_path as root must NOT raise (the 41-failure bug)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="root/uid semantics are POSIX-only")
def test_absent_tmppath_file_as_root_does_not_raise(tmp_path: Path) -> None:
    """Root creating a file under tmp_path is safe — guard must not fire."""
    db = tmp_path / "ephemeral.db"
    assert not db.exists()
    with patch("os.geteuid", return_value=0):
        ensure_db_writable(db)  # must not raise


@pytest.mark.skipif(os.name != "posix", reason="root/uid semantics are POSIX-only")
def test_absent_file_under_tempdir_as_root_does_not_raise() -> None:
    """Root creating a file anywhere under tempfile.gettempdir() is exempt."""
    db = Path(tempfile.gettempdir()) / "pytest-whatever" / "test_0" / "vault.db"
    with patch("os.geteuid", return_value=0):
        ensure_db_writable(db)  # must not raise


# ---------------------------------------------------------------------------
# Regression: real production path as root STILL raises (protection intact)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="root/uid semantics are POSIX-only")
def test_absent_production_path_as_root_still_raises() -> None:
    """Root creating a file under a real (non-temp) project path must still be rejected.

    Uses a path guaranteed not to exist and guaranteed not to be under
    tempfile.gettempdir(), so the test exercises the actual guard logic.
    """
    # /srv/trading/data/ is a typical production mount point — definitely not
    # under /tmp and guaranteed not to exist in any test environment.
    production_db = Path("/srv/trading/data/research_vault.db")
    assert not production_db.exists(), "test pre-condition: path must not exist"

    with (
        patch("os.geteuid", return_value=0),
        patch.dict(os.environ, {}, clear=False),
    ):
        env_backup = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            with pytest.raises(PersistenceOwnershipError) as exc_info:
                ensure_db_writable(production_db)
            msg = str(exc_info.value)
            assert str(production_db) in msg
            assert "root" in msg
            assert "trader" in msg
        finally:
            if env_backup is not None:
                os.environ["PYTEST_CURRENT_TEST"] = env_backup


# ---------------------------------------------------------------------------
# ensure_db_writable — unwritable existing file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="root/uid semantics are POSIX-only")
def test_existing_unwritable_file_raises(tmp_path: Path) -> None:
    db = tmp_path / "readonly.db"
    db.touch()
    with patch("os.access", return_value=False):
        with pytest.raises(PersistenceOwnershipError) as exc_info:
            ensure_db_writable(db)
    msg = str(exc_info.value)
    assert str(db) in msg
    assert "process_uid" in msg
    assert "file_owner_uid" in msg
    assert "chown" in msg
