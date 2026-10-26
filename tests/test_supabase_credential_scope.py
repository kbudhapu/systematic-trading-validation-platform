"""D3 / CP7 F3 — scoped bot credential preference + legacy warn-not-crash."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def sc(monkeypatch):
    """Fresh supabase_client module with all relevant env vars cleared."""
    for var in (
        "SUPABASE_TRADING_BOT_KEY",
        "SUPABASE_BOT_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "TRADING_ENVIRONMENT",
    ):
        monkeypatch.delenv(var, raising=False)
    import src.control.supabase_client as module

    importlib.reload(module)
    return module


def test_prefers_trading_bot_key(sc, monkeypatch):
    monkeypatch.setenv("SUPABASE_TRADING_BOT_KEY", "botjwt")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    key, channel = sc.resolve_supabase_api_key(prefer_bot=True)
    assert key == "botjwt"
    assert channel == "trading_bot_node"


def test_supabase_bot_key_alias(sc, monkeypatch):
    monkeypatch.setenv("SUPABASE_BOT_KEY", "aliasjwt")
    key, channel = sc.resolve_supabase_api_key(prefer_bot=True)
    assert key == "aliasjwt"
    assert channel == "trading_bot_node"


def test_service_role_fallback_is_legacy_channel_not_crash(sc, monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("TRADING_ENVIRONMENT", "paper")
    # Must NOT raise — warn-not-crash during the transition window.
    key, channel = sc.resolve_supabase_api_key(prefer_bot=True)
    assert key == "svc"
    assert channel == "service_role_legacy"


def test_backtest_service_role_is_quiet_channel(sc, monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("TRADING_ENVIRONMENT", "backtest")
    key, channel = sc.resolve_supabase_api_key(prefer_bot=True)
    assert (key, channel) == ("svc", "service_role_legacy")


def test_unconfigured_returns_empty(sc):
    key, channel = sc.resolve_supabase_api_key(prefer_bot=True)
    assert (key, channel) == ("", "")
