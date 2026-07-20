"""Bug-2 (shape 1): bounded retry-with-rebuild in supabase_client.run_with_supabase_retry.

The safety properties that MUST hold:
  (a) a transient ``ConnectionTerminated`` error is retried after the cached client is
      cleared (forcing a rebuild), and the eventual success is returned;
  (b) a persistently-terminal error is retried AT MOST ``attempts`` times, then returns
      ``None`` -- never an infinite loop, never a raise;
  (c) a non-terminal exception returns ``None`` immediately without raising.

Callers already catch+log+degrade, so the helper must NEVER raise.
"""
from __future__ import annotations

import pytest

from src.control import supabase_client


class _FakeClient:
    """Stand-in for a Supabase client (the retry helper only passes it to ``op``)."""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Retry backoff must not actually sleep during the test.
    monkeypatch.setattr(supabase_client.time, "sleep", lambda *_a, **_k: None)


def _install_fake_client(monkeypatch):
    """Monkeypatch ``get_supabase`` to a fake client + a cache_clear counter.

    Returns a dict of counters: ``get`` = # of get_supabase() calls,
    ``clear`` = # of cache_clear() calls.
    """
    counters = {"get": 0, "clear": 0}

    def fake_get_supabase():
        counters["get"] += 1
        return _FakeClient()

    def fake_cache_clear():
        counters["clear"] += 1

    fake_get_supabase.cache_clear = fake_cache_clear  # type: ignore[attr-defined]
    monkeypatch.setattr(supabase_client, "get_supabase", fake_get_supabase)
    return counters


def test_retries_terminal_then_succeeds(monkeypatch):
    counters = _install_fake_client(monkeypatch)
    calls = {"n": 0}

    def op(_client):
        calls["n"] += 1
        if calls["n"] == 1:
            # h2 GOAWAY surfaces as an error whose str() contains ConnectionTerminated.
            raise RuntimeError("<ConnectionTerminated error_code:0>")
        return "ok"

    result = supabase_client.run_with_supabase_retry(op, label="unit")

    assert result == "ok"  # eventual success is returned
    assert calls["n"] == 2  # retried exactly once
    assert counters["clear"] == 1  # cached client dropped before the rebuild
    assert counters["get"] == 2  # a fresh client fetched for the retry


def test_persistently_terminal_returns_none_bounded(monkeypatch):
    counters = _install_fake_client(monkeypatch)
    calls = {"n": 0}

    def op(_client):
        calls["n"] += 1
        raise RuntimeError("connection lost: ConnectionTerminated")

    result = supabase_client.run_with_supabase_retry(op, label="unit", attempts=3)

    assert result is None  # degrades, never raises
    assert calls["n"] == 3  # bounded at `attempts` -- no infinite loop
    assert counters["clear"] == 2  # cleared before each of the 2 retries (not the last)


def test_non_terminal_returns_none_without_raising(monkeypatch):
    counters = _install_fake_client(monkeypatch)
    calls = {"n": 0}

    def op(_client):
        calls["n"] += 1
        raise ValueError("some unrelated schema error")

    result = supabase_client.run_with_supabase_retry(op, label="unit", attempts=3)

    assert result is None  # returns None, never raises
    assert calls["n"] == 1  # non-terminal error is NOT retried
    assert counters["clear"] == 0  # cache not cleared for a non-terminal error


def test_returns_none_when_unconfigured(monkeypatch):
    counters = {"get": 0}

    def fake_get_supabase():
        counters["get"] += 1
        return None

    fake_get_supabase.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(supabase_client, "get_supabase", fake_get_supabase)

    called = {"n": 0}

    def op(_client):
        called["n"] += 1
        return "unreachable"

    result = supabase_client.run_with_supabase_retry(op, label="unit")

    assert result is None  # no client -> no-op
    assert called["n"] == 0  # op never invoked without a client
