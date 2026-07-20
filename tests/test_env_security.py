"""P3 security: paper/live endpoint guard + a MOCK key-rotation drill (config
reload picks up new-key env values). No real rotation is performed."""
from __future__ import annotations

import pytest

from src.config.env_guard import (
    EnvironmentEndpointMismatch, assert_endpoint_matches_environment,
)

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"


# ---- paper/live endpoint guard -------------------------------------------- #

def test_paper_env_with_paper_url_ok() -> None:
    assert_endpoint_matches_environment("paper", PAPER)   # no raise


def test_live_env_with_live_url_ok() -> None:
    assert_endpoint_matches_environment("live", LIVE)     # no raise


def test_paper_env_with_live_url_raises() -> None:
    with pytest.raises(EnvironmentEndpointMismatch):
        assert_endpoint_matches_environment("paper", LIVE)


def test_live_env_with_paper_url_raises() -> None:
    with pytest.raises(EnvironmentEndpointMismatch):
        assert_endpoint_matches_environment("live", PAPER)


def test_backtest_and_empty_url_unconstrained() -> None:
    assert_endpoint_matches_environment("backtest", LIVE)   # backtest uses no live endpoint
    assert_endpoint_matches_environment("paper", "")        # unconfigured -> not checked


def test_load_config_enforces_endpoint_match(monkeypatch) -> None:
    """load_config() fails fast if ALPACA_BASE_URL contradicts the env.yaml
    environment (env.yaml ships environment: paper)."""
    from src.config import load_config
    monkeypatch.setenv("ALPACA_BASE_URL", LIVE)   # live URL against paper env.yaml
    with pytest.raises(EnvironmentEndpointMismatch):
        load_config()


# ---- MOCK key-rotation drill ---------------------------------------------- #

def test_key_rotation_reload_picks_up_new_keys(monkeypatch) -> None:
    """App-side rotation procedure: after updating .env with a rotated key, a
    config reload surfaces the NEW key (no real rotation performed here)."""
    from src.config import load_config
    monkeypatch.setenv("ALPACA_BASE_URL", PAPER)          # keep endpoint consistent
    monkeypatch.setenv("ALPACA_API_KEY", "OLD_PAPER_KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "OLD_SECRET")
    cfg1 = load_config()
    assert cfg1.alpaca_api_key == "OLD_PAPER_KEY"

    # operator rotates the paper key in the Alpaca dashboard and updates .env:
    monkeypatch.setenv("ALPACA_API_KEY", "NEW_PAPER_KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "NEW_SECRET")
    cfg2 = load_config()                                   # reload path
    assert cfg2.alpaca_api_key == "NEW_PAPER_KEY", "reload must surface the rotated key"
    assert cfg2.alpaca_secret_key == "NEW_SECRET"
