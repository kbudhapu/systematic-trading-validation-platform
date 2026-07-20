from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# R2.5 TEST-DB ISOLATION -- this MUST run before any `src` import.
#
# Every store path derives from the single env-aware `src.config.DATA_DIR`
# (which reads MBAPPE_DATA_DIR). By setting that env var here, at conftest
# import time -- before pytest collects any test module (and therefore before
# any `import src...` binds a DB_PATH / RESEARCH_VAULT_PATH default) -- the
# import-time path constants themselves resolve to a per-session tmp dir. That
# is why a post-import monkeypatch is NOT enough (function/dataclass defaults
# capture the value at def time): the redirect has to happen before import.
#
# Result: NO test can write production state (data/trading.db,
# data/research_vault.db, circuit_breaker_state.json, backups, ...). The
# session-scoped `_production_db_tripwire` fixture below is the permanent
# guarantee: it fails the suite loudly if any production DB file is touched.
# ---------------------------------------------------------------------------
import shutil
import tempfile
from pathlib import Path

import sqlite3 as _sqlite3
import sys as _sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_DATA_DIR = _REPO_ROOT / "data"
# Captured BEFORE the path-assertion patches sqlite3.connect, so the tripwire's own
# read of the real DB (live-writer detection) bypasses the guard.
_REAL_SQLITE_CONNECT = _sqlite3.connect

if not os.environ.get("MBAPPE_DATA_DIR"):
    _session_data_dir = Path(tempfile.mkdtemp(prefix="mbappe-test-data-"))
    # Seed the small JSON reference fixtures tests legitimately READ (research
    # validation index, parity baselines, calibration, ...). NEVER copy the
    # .db files -- tests must start from empty DBs, not inherit production
    # kill-state (the leak this fixture exists to prevent). Parquet caches are
    # skipped (large; the fast suite does not read them).
    try:
        if _REAL_DATA_DIR.is_dir():
            for _item in _REAL_DATA_DIR.iterdir():
                if _item.is_file() and _item.suffix == ".json":
                    shutil.copy2(_item, _session_data_dir / _item.name)
    except OSError:
        pass
    os.environ["MBAPPE_DATA_DIR"] = str(_session_data_dir)

import pytest


# --------------------------------------------------------------------------- #
# R2.5+ TEST-DB ISOLATION -- two layers (RA-TRIPWIRE fix).
#
# LAYER 1 (always-on, the guarantee): PATH-ASSERTION. sqlite3.connect is patched
# in the test process to assert no test opens a DB path under the REAL data dir.
# It is IMMUNE to concurrent writers -- it inspects only in-process connect() calls,
# so the live soak (a SEPARATE process, PID 29744 on the Ryzen) writing the real
# trading.db is irrelevant. This is the direct property; it works identically whether
# the soak is live or stopped, with no blind window.
#
# LAYER 2 (second, liveness-gated): the production-DB CONTENT HASH. Strict-asserts when
# no live writer is present (the R2.5 pollution-catch case). When a live writer IS
# detected it is loudly SKIPPED (not warning-downgraded) -- the hash cannot distinguish
# a legitimate concurrent writer from a test, so Layer 1 carries the guarantee there.
# --------------------------------------------------------------------------- #
def _resolve_db_path(target):
    """Normalize an sqlite3.connect target to a resolved filesystem path, or None for
    in-memory / non-path targets."""
    if not isinstance(target, (str, os.PathLike)):
        return None
    s = os.fspath(target)
    if not s or s == ":memory:" or s.startswith("file::memory:") or ":memory:" in s:
        return None
    if s.startswith("file:"):
        from urllib.parse import unquote, urlparse

        p = unquote(urlparse(s).path)
        if os.name == "nt" and len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        s = p
    try:
        return Path(s).resolve()
    except Exception:
        return None


def _guard_db_path(target) -> None:
    """LAYER 1: raise if a DB under the REAL data dir is opened from a test."""
    p = _resolve_db_path(target)
    if p is None:
        return
    try:
        under_real = p.is_relative_to(_REAL_DATA_DIR.resolve())
    except Exception:
        return
    if under_real:
        raise AssertionError(
            f"TEST-DB ISOLATION BREACH (path-assertion): a test opened the production "
            f"DB path {p} under {_REAL_DATA_DIR}. Route it through src.config.DATA_DIR "
            f"(the MBAPPE_DATA_DIR redirect), never a hardcoded real path."
        )


def _guarded_connect(*args, **kwargs):
    if args:
        _guard_db_path(args[0])
    elif "database" in kwargs:
        _guard_db_path(kwargs["database"])
    return _REAL_SQLITE_CONNECT(*args, **kwargs)


def _real_writer_is_live(threshold_seconds: float = 300.0) -> bool:
    """True if the REAL trading.db shows a fresh engine heartbeat -- i.e. a live soak
    bot is writing it concurrently. Uses the pre-patch connect so it never self-trips
    the path-assertion."""
    real_db = _REAL_DATA_DIR / "trading.db"
    if not real_db.exists():
        return False
    try:
        con = _REAL_SQLITE_CONNECT(f"file:{real_db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value_text FROM engine_operational_state "
                "WHERE state_key = 'last_successful_cycle_at'"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return False
    if not row or not row[0]:
        return False
    import datetime as _dt

    try:
        ts = _dt.datetime.fromisoformat(str(row[0]))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() < threshold_seconds


def _evaluate_tripwire(before: dict, after: dict, *, live_writer: bool) -> tuple[str, list]:
    """LAYER 2 verdict: 'ok' (unchanged), 'skip' (changed but a live writer explains it),
    or 'breach' (changed with no live writer -> a test escaped)."""
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    if not changed:
        return "ok", changed
    return ("skip" if live_writer else "breach"), changed


@pytest.fixture(scope="session", autouse=True)
def _db_path_assertion():
    """LAYER 1 install: patch sqlite3.connect for the whole test session."""
    _sqlite3.connect = _guarded_connect
    try:
        yield
    finally:
        _sqlite3.connect = _REAL_SQLITE_CONNECT


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast (with an actionable message) if the async plugin is missing.

    The suite runs ``asyncio_mode = "auto"`` and has async tests (order-path,
    governance, sieve). Without pytest-asyncio installed those tests do not run
    as async -- pytest reports the confusing "async def functions are not
    natively supported" and they silently go dark. Turn that into one clear
    error at collection time so the tests can never quietly regress to dark.
    """
    try:
        import pytest_asyncio  # noqa: F401
    except ImportError:  # pragma: no cover - environment guard
        raise pytest.UsageError(
            "pytest-asyncio is not installed but the suite has async tests and "
            "asyncio_mode=auto. Install the dev extras: pip install -e \".[dev]\" "
            "(or pip install 'pytest-asyncio>=0.24.0')."
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live-smoke",
        action="store_true",
        default=False,
        help="run live Alpaca sandbox smoke tests",
    )


@pytest.fixture
def live_smoke_enabled(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--run-live-smoke")) or os.getenv(
        "RUN_LIVE_SMOKE", ""
    ).strip() == "1"


@pytest.fixture(autouse=True)
def _reset_alert_cooldowns():
    """Clear module-level cooldown cache between tests to prevent ordering-dependent failures."""
    import src.control.alerts as _alerts_mod
    _alerts_mod._recent_pages.clear()


@pytest.fixture(scope="session", autouse=True)
def _init_isolated_data_dir():
    """Initialize the redirected (tmp) data dir the way production initializes
    data/ at startup: create the trading.db + research_vault.db schemas. Store
    helpers like log_system_event() assume the schema pre-exists (production
    calls init_db() on boot); without this the isolated DBs would be empty and
    those writes would fail. Runs once per session, before any test."""
    from src.persistence.db import init_db

    init_db()
    yield


def _hash_production_db_files() -> dict[str, str]:
    """SHA-256 of every production DB / kill-state file under the REAL data dir.

    Independent of the MBAPPE_DATA_DIR redirect -- it always points at the real
    repo `data/` so it can detect a test that escaped isolation."""
    import hashlib

    fingerprints: dict[str, str] = {}
    if not _REAL_DATA_DIR.is_dir():
        return fingerprints
    for item in sorted(_REAL_DATA_DIR.iterdir()):
        if item.is_file() and (item.suffix == ".db" or item.name == "circuit_breaker_state.json"):
            fingerprints[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
    return fingerprints


@pytest.fixture(scope="session", autouse=True)
def _production_db_tripwire():
    """PERMANENT tripwire (R2.5): the whole suite must not touch any production
    DB / kill-state file. Snapshot hashes of the real data/ DB files before the
    suite and assert them unchanged after. Any test that escapes MBAPPE_DATA_DIR
    isolation and writes production state fails the suite loudly, forever."""
    before = _hash_production_db_files()
    yield
    after = _hash_production_db_files()
    verdict, changed = _evaluate_tripwire(
        before, after, live_writer=_real_writer_is_live()
    )
    if verdict == "skip":
        # A live soak bot legitimately mutated the production DB(s) during the run; the
        # hash cannot distinguish that from a test, so Layer 1 (path-assertion) carries
        # the guarantee. Loud SKIP, not a warning-downgrade.
        print(
            f"\n[tripwire] LAYER-2 HASH CHECK SKIPPED: live writer detected (soak bot); "
            f"production DB(s) {changed} changed by the running soak, NOT by a test. "
            f"Layer-1 path-assertion remains enforced.",
            file=_sys.stderr,
        )
        return
    if verdict == "breach":
        raise AssertionError(
            "TEST-DB ISOLATION BREACH (hash layer, no live writer): the suite modified "
            f"production DB file(s) {changed} under {_REAL_DATA_DIR}. A test escaped the "
            "MBAPPE_DATA_DIR redirect. Route it through src.config.DATA_DIR."
        )
