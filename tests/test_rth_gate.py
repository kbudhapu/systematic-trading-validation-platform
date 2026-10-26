"""T2 — the RTH gate (batch<->live parity). Equity legs must not act outside RTH.

Locks the decision used at BOTH the L1 signal guard and the L2 submission gate in the
orchestrator: default DENY for equities outside RTH; crypto and a registered extended_hours
opt-in are exempt; holiday/early-close awareness is delegated to the one shared
MarketSessionCalendar. Pure helper + real-calendar integration; no orchestrator run needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.core.market_calendar import MarketSessionCalendar
from src.engine.orchestrator import leg_blocked_outside_session


class _Cal:
    def __init__(self, rth: bool):
        self._rth = rth

    def is_within_rth(self, now):
        return self._rth


def _cfg(asset_class="stock", extended_hours=False):
    return SimpleNamespace(asset_class=asset_class, extended_hours=extended_hours,
                           strategy_id="mean_reversion_qqq", symbol="QQQ")


_NOW = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)  # placeholder; _Cal ignores it


def test_equity_blocked_outside_rth():
    assert leg_blocked_outside_session(_cfg(), _NOW, _Cal(rth=False)) is True


def test_equity_allowed_within_rth():
    assert leg_blocked_outside_session(_cfg(), _NOW, _Cal(rth=True)) is False


def test_crypto_never_blocked_even_when_closed():
    assert leg_blocked_outside_session(_cfg(asset_class="crypto"), _NOW, _Cal(rth=False)) is False


def test_registered_extended_hours_optin_never_blocked():
    assert leg_blocked_outside_session(_cfg(extended_hours=True), _NOW, _Cal(rth=False)) is False


def test_config_default_is_deny():
    from src.config import StrategyConfig
    c = StrategyConfig(strategy_id="s", module="m", symbol="QQQ", timeframe="15Min",
                       poll_interval_seconds=900, params={})
    assert c.extended_hours is False


# --- integration against the REAL calendar (holiday / early-close / weekend awareness) --- #
def test_real_calendar_blocks_premarket_and_overnight_and_weekend():
    cal = MarketSessionCalendar()
    equity = _cfg()
    premarket = datetime(2026, 7, 8, 11, 0, tzinfo=timezone.utc)   # Wed 07:00 ET (pre-open)
    overnight = datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)    # Wed 02:00 ET
    saturday = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)   # Sat 11:00 ET (market closed)
    for dt in (premarket, overnight, saturday):
        assert leg_blocked_outside_session(equity, dt, cal) is True


def test_real_calendar_allows_equity_inside_rth():
    cal = MarketSessionCalendar()
    within = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)      # Wed 11:00 ET (RTH)
    assert leg_blocked_outside_session(_cfg(), within, cal) is False
    # crypto is unaffected by the closed weekend
    sat = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
    assert leg_blocked_outside_session(_cfg(asset_class="crypto"), sat, cal) is False
