"""Dual-runtime guard: a second live trading run must be refused (MIG-DUALRUNTIME).

flock is POSIX-only; on Windows (the research box) the guard is a documented
no-op, so these tests skip there and validate on the Linux production platform.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

posix_only = pytest.mark.skipif(os.name != "posix", reason="flock is POSIX-only")


@posix_only
def test_second_live_run_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first acquirer holds the lock; a second acquirer is refused. flock
    denies a second open-file-description on the same file even in-process
    (flock(2): independent fds for the same file may be denied one another)."""
    monkeypatch.setenv("MBAPPE_TEST_RUN_LOCK", "1")  # enable the guard under pytest
    from src.single_instance import (
        SingleInstanceError,
        acquire_run_lock,
        release_run_lock,
    )

    lock = tmp_path / "mbappe-run.lock"
    acquire_run_lock(lock)
    try:
        assert lock.read_text().strip() == str(os.getpid()), "holder pid recorded"
        with pytest.raises(SingleInstanceError):
            acquire_run_lock(lock)
    finally:
        release_run_lock()


@posix_only
def test_lock_is_reacquirable_after_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Releasing (as the kernel does on process death) frees the lock, so a
    restart -- e.g. systemd reviving the soak -- can re-acquire it cleanly."""
    monkeypatch.setenv("MBAPPE_TEST_RUN_LOCK", "1")
    from src.single_instance import acquire_run_lock, release_run_lock

    lock = tmp_path / "mbappe-run.lock"
    acquire_run_lock(lock)
    release_run_lock()
    acquire_run_lock(lock)  # must not raise -- prior holder released
    release_run_lock()
