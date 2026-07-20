"""
Persistence file ownership guard.

Catches the class of failure where a database file (or the directory that will
contain it) is not writable by the current process — typically because a
maintenance script or migration was run as root, silently creating a
root-owned file that the long-running service user (trader) cannot write to.

Without this guard the failure surfaces as a generic
``sqlite3.OperationalError: attempt to write a readonly database`` many cycles
later, with no indication of the root cause.  This guard converts it to an
immediate, descriptive error at the point where the file would be created or
first opened, naming the path, the current process UID, and the file's owner.

Root-cause incident: 2026-06-26 — research_vault.db owned by root while service
ran as trader; every cycle failed with sqlite3.OperationalError.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class PersistenceOwnershipError(Exception):
    """Raised when a database file or its parent directory is not writable.

    Operator action: ``chown <service_user>:<service_user> <path>``
    """


def _is_exempt_from_root_create_check(db_path: Path) -> bool:
    """Return True if the root-creates-new-file check should be skipped.

    Two exemption conditions (belt-and-suspenders):

    1. Running under pytest — ``PYTEST_CURRENT_TEST`` is set by pytest itself
       for the duration of the entire test process, no test-file changes needed.

    2. The target path is inside the OS temp directory — covers pytest's
       ``tmp_path`` fixture and any other ephemeral path strategy.

    The real-production failure mode (creating a file under the project's
    ``data/`` directory while running as root) satisfies neither condition, so
    the guard still fires there.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True
    try:
        db_path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        return True
    except ValueError:
        return False


def ensure_db_writable(db_path: Path) -> None:
    """Raise PersistenceOwnershipError if db_path would not be safely writable.

    Two failure modes are detected:

    1. **File already exists but is not writable** by the current process
       (e.g. owned by root, service runs as trader).  Detected via
       ``os.access(db_path, os.W_OK)``.

    2. **File does not yet exist and this process is root** (uid 0), AND the
       path is not a temporary/test path.  The file would be created owned by
       root, making it unwritable by the service user on the next run.

    Normal operation (file writable, or file absent and not running as root) is
    a no-op — no stat calls, no overhead beyond a single ``Path.exists()`` check.
    """
    if os.name != "posix":
        # Root-ownership traps (chown, uid 0) are a POSIX/Linux production
        # concept only — os.geteuid() doesn't exist on Windows, and the
        # failure mode this guard protects against can't occur there.
        return
    if db_path.exists():
        if not os.access(db_path, os.W_OK):
            uid = os.geteuid()
            try:
                st = db_path.stat()
                owner_uid = st.st_uid
                mode = oct(stat.S_IMODE(st.st_mode))
            except OSError:
                owner_uid = "unknown"
                mode = "unknown"
            raise PersistenceOwnershipError(
                f"Database file exists but is not writable by this process. "
                f"path={db_path}  process_uid={uid}  "
                f"file_owner_uid={owner_uid}  mode={mode}. "
                f"Fix: chown {uid}:{uid} {db_path}"
            )
    else:
        # File does not exist yet — it will be created by sqlite3.connect().
        # If we are running as root the new file will be owned by root, and
        # the service user won't be able to write to it (same trap as the
        # 2026-06-26 incident).  Refuse early with a clear message.
        # Exemption: temp paths and test contexts are safe to create as root.
        if os.geteuid() == 0 and not _is_exempt_from_root_create_check(db_path):
            raise PersistenceOwnershipError(
                f"Refusing to create database as root (uid 0): the resulting "
                f"file would be owned by root and unwritable by the service "
                f"user on the next run.  "
                f"path={db_path}. "
                f"Run this command as the service user (e.g. 'sudo -u trader ...')"
            )
