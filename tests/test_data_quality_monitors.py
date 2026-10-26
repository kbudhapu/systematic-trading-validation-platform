"""G1.4 pre-signal data-quality monitors: staleness (calendar-aware), bar sanity,
and the corporate-action guard (USO 1:8 split signature). Plus config-block schema
validation (additive, OFF by default)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from src.config import CONFIG_DIR
from src.config.schema_check import SchemaValidationError, load_schema, validate_instance, validate_or_raise
from src.data_quality.monitors import (
    DataQualityConfig, DataQualityMonitors, DataQualityVerdict,
    check_bar_sanity, check_corporate_action, check_staleness,
)
from src.engine.slo_monitor import MarketSessionCalendar
from src.models import Bar

_CAL = MarketSessionCalendar()


def _bar(ts: datetime, o=100.0, h=101.0, l=99.0, c=100.5, v=1_000_000.0, sym="QQQ") -> Bar:
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v, symbol=sym)


def _run(coro):
    return asyncio.run(coro)


# ---- bar sanity ----------------------------------------------------------- #

@pytest.mark.parametrize("kwargs,reason", [
    ({"c": float("nan")}, "bad_close"),
    ({"o": 0.0}, "bad_open"),
    ({"l": -5.0}, "bad_low"),
    ({"h": 98.0, "l": 99.0}, "high_lt_low"),
    ({"v": -1.0}, "bad_volume"),
])
def test_bar_sanity_quarantines_corrupt_bars(kwargs, reason) -> None:
    ts = datetime(2024, 7, 1, 14, 0, tzinfo=timezone.utc)
    res = check_bar_sanity(_bar(ts, **kwargs))
    assert res.verdict == DataQualityVerdict.QUARANTINE_BAR
    assert res.reason == reason


def test_bar_sanity_non_monotonic_timestamp() -> None:
    ts = datetime(2024, 7, 1, 14, 0, tzinfo=timezone.utc)
    res = check_bar_sanity(_bar(ts), prev_timestamp=ts + timedelta(minutes=15))
    assert res.verdict == DataQualityVerdict.QUARANTINE_BAR
    assert res.reason == "non_monotonic_timestamp"


def test_bar_sanity_healthy_bar_allowed() -> None:
    ts = datetime(2024, 7, 1, 14, 0, tzinfo=timezone.utc)
    assert check_bar_sanity(_bar(ts)).verdict == DataQualityVerdict.ALLOW


# ---- staleness (calendar-aware) ------------------------------------------- #

def test_stale_feed_blocks_new_entries_intraday() -> None:
    cfg = DataQualityConfig(enabled=True, staleness_bar_age_multiple=2.5)
    # Wed 2024-07-10 mid-session; last bar 2h old on a 15m timeframe -> stale
    now = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)      # ~1pm ET, RTH
    last = now - timedelta(minutes=120)
    res = check_staleness("QQQ", last, now, timeframe_minutes=15, config=cfg, calendar=_CAL)
    assert res.verdict == DataQualityVerdict.BLOCK_NEW_ENTRIES


def test_weekend_closed_market_is_not_stale() -> None:
    """Calendar-closed weekend: a bar from Friday's close is NOT stale on Sunday --
    staleness is measured from the last session close, not wall-clock."""
    cfg = DataQualityConfig(enabled=True, staleness_bar_age_multiple=2.5)
    friday_close = datetime(2024, 7, 12, 20, 0, tzinfo=timezone.utc)   # ~4pm ET Fri
    sunday = datetime(2024, 7, 14, 17, 0, tzinfo=timezone.utc)         # market closed
    res = check_staleness("QQQ", friday_close, sunday, timeframe_minutes=15,
                          config=cfg, calendar=_CAL)
    assert res.verdict == DataQualityVerdict.ALLOW, "closed market must not be stale"


def test_crypto_uses_wallclock_staleness() -> None:
    cfg = DataQualityConfig(enabled=True, staleness_bar_age_multiple=2.5)
    now = datetime(2024, 7, 14, 3, 0, tzinfo=timezone.utc)      # Sunday, crypto trades
    last = now - timedelta(hours=5)
    res = check_staleness("BTC/USD", last, now, timeframe_minutes=60, config=cfg,
                          calendar=_CAL, asset_class="crypto")
    assert res.verdict == DataQualityVerdict.BLOCK_NEW_ENTRIES


# ---- corporate-action guard ----------------------------------------------- #

def test_uso_split_signature_quarantines_instrument() -> None:
    """USO 1:8 reverse split: price /8 overnight (~-87.5%) with the market flat ->
    quarantine the instrument."""
    cfg = DataQualityConfig(enabled=True, corporate_action_jump_threshold=0.25)
    res = check_corporate_action("USO", prev_close=80.0, today_open=10.0,
                                 market_prev_close=400.0, market_open=401.0, config=cfg)
    assert res.verdict == DataQualityVerdict.QUARANTINE_INSTRUMENT
    assert res.reason == "suspected_corporate_action"


def test_market_wide_gap_is_not_quarantined() -> None:
    """A big instrument jump matched by a comparable market-wide move (e.g. a
    crash-open) is a genuine gap, not a corporate action."""
    cfg = DataQualityConfig(enabled=True, corporate_action_jump_threshold=0.25)
    res = check_corporate_action("SPY", prev_close=400.0, today_open=280.0,
                                 market_prev_close=400.0, market_open=280.0, config=cfg)
    assert res.verdict == DataQualityVerdict.ALLOW
    assert res.reason == "market_wide_gap"


def test_small_overnight_move_allowed() -> None:
    cfg = DataQualityConfig(enabled=True, corporate_action_jump_threshold=0.25)
    res = check_corporate_action("QQQ", prev_close=400.0, today_open=404.0,
                                 market_prev_close=400.0, market_open=404.0, config=cfg)
    assert res.verdict == DataQualityVerdict.ALLOW


# ---- coordinator (OFF by default; per-instrument state) -------------------- #

def test_disabled_by_default_returns_allow() -> None:
    mon = DataQualityMonitors(DataQualityConfig(enabled=False))
    ts = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)
    res = _run(mon.evaluate(_bar(ts, c=float("nan")), now=ts, timeframe_minutes=15))
    assert res.verdict == DataQualityVerdict.ALLOW, "monitors OFF by default -> no-op"


def test_coordinator_split_quarantine_persists_until_cleared() -> None:
    reports: list[dict] = []
    mon = DataQualityMonitors(DataQualityConfig(enabled=True), report_sink=reports.append)
    ts = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)
    bar = _bar(ts, o=10.0, h=10.1, l=9.9, c=10.0, sym="USO")
    res = _run(mon.evaluate(bar, now=ts, timeframe_minutes=15,
                            prev_close=80.0, today_open=10.0,
                            market_prev_close=400.0, market_open=401.0))
    assert res.verdict == DataQualityVerdict.QUARANTINE_INSTRUMENT
    assert mon.entries_blocked("USO")
    assert any(r["kind"] == "data_quality" for r in reports)
    mon.clear("USO")
    assert not mon.entries_blocked("USO")


def test_coordinator_healthy_bar_clears_stale_block() -> None:
    mon = DataQualityMonitors(DataQualityConfig(enabled=True))
    stale_now = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)
    stale_bar = _bar(stale_now - timedelta(minutes=120))
    _run(mon.evaluate(stale_bar, now=stale_now, timeframe_minutes=15))
    assert mon.entries_blocked("QQQ")
    fresh_now = datetime(2024, 7, 10, 17, 15, tzinfo=timezone.utc)
    fresh_bar = _bar(fresh_now)
    _run(mon.evaluate(fresh_bar, now=fresh_now, timeframe_minutes=15))
    assert not mon.entries_blocked("QQQ"), "a fresh feed clears the staleness entry-block"


# ---- config parity / schema ----------------------------------------------- #

def _load_block() -> dict:
    with (CONFIG_DIR / "env.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)["data_quality"]


def test_repo_data_quality_block_matches_schema() -> None:
    schema = load_schema(CONFIG_DIR / "data_quality.schema.json")
    assert validate_instance(_load_block(), schema) == []


def test_data_quality_tracks_soak_state() -> None:
    """K5: data-quality monitors are armed for the soak and reverted with it. They
    track soak.enabled (both ON during the soak window, both OFF once reverted) --
    a monitors-on state is only valid while the soak is armed."""
    import yaml

    from src.config import CONFIG_DIR
    env = yaml.safe_load((CONFIG_DIR / "env.yaml").read_text(encoding="utf-8"))
    assert env["data_quality"]["enabled"] == env["soak"]["enabled"]


def test_malformed_data_quality_block_rejected() -> None:
    schema = load_schema(CONFIG_DIR / "data_quality.schema.json")
    bad = {**_load_block(), "corporate_action_jump_threshold": 5.0}   # > 1
    with pytest.raises(SchemaValidationError):
        validate_or_raise(bad, schema)
    with pytest.raises(SchemaValidationError):
        validate_or_raise({**_load_block(), "bogus": 1}, schema)
