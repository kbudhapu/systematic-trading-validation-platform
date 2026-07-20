"""T2e — session awareness: a leg whose venue is CLOSED is DORMANT, not STALE.

A closed equity market has no expected bars, quotes, or ticks. Treating that as data-integrity
FAILURE would, via the shared degradation manager, force-flatten the 24/7 crypto legs every night
and weekend (~118 h/week) on evidence that says nothing about crypto. Crypto is never dormant.

NOTE (T2a classification / T2b-d): every current SLO input (bar_freshness, missing_bar_rate,
nbbo_fetch, ingest_circuit_breaker, sequence_misalignment, ingest_arrival_lag) is PER-LEG — it
measures ONE instrument's feed. The remaining architectural fix (a per-leg data-integrity signal
must degrade only its leg, never force-flatten the portfolio) is tracked separately as a dedicated
TIER-1 refactor (per-leg degradation state + per-leg governance rows). T2e closes the acute
session-scope case at the source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.engine.slo_monitor import IntegritySeverity, SLOMonitor

ET = ZoneInfo("America/New_York")


def _stock_payload(now_utc, latest_open) -> dict:
    return {
        "exchange_reference_ts": now_utc, "asset_class": "stock", "timeframe": "15Min",
        "latest_bar_timestamp": latest_open,
        "bar_timestamps": [latest_open], "nbbo_success_rate": 0.1,  # would be nbbo_fetch_critical (<0.55)
        "rl_backfill_lag_hours": 0.0,
    }


def test_equity_leg_is_dormant_when_market_closed():
    # 02:00 ET = deep overnight, market closed. Even a totally stale bar + zero nbbo -> DORMANT/OK.
    now = datetime(2026, 6, 24, 2, 0, tzinfo=ET).astimezone(timezone.utc)
    v = SLOMonitor().evaluate_data_integrity(_stock_payload(now, now - timedelta(hours=12)))
    assert v.severity == IntegritySeverity.OK
    assert v.passed is True
    assert "dormant_market_closed" in v.reasons
    # crucially: NONE of the live-feed criticals fire while dormant
    assert "nbbo_fetch_critical" not in v.reasons
    assert "bar_freshness_critical" not in v.reasons


def test_equity_leg_is_evaluated_normally_during_rth():
    # 11:00 ET = RTH: a genuine nbbo failure still HARD-breaches (dormancy must not mask real faults)
    now = datetime(2026, 6, 24, 11, 0, tzinfo=ET).astimezone(timezone.utc)
    v = SLOMonitor().evaluate_data_integrity(_stock_payload(now, now))
    assert v.severity == IntegritySeverity.HARD_BREACH
    assert "nbbo_fetch_critical" in v.reasons
    assert "dormant_market_closed" not in v.reasons


def test_crypto_leg_is_never_dormant():
    # 02:00 ET overnight: the equity market is closed, but BTC trades 24/7 and is evaluated normally.
    now = datetime(2026, 6, 24, 2, 0, tzinfo=ET).astimezone(timezone.utc)
    payload = {
        "exchange_reference_ts": now, "asset_class": "crypto", "timeframe": "1Hour",
        "latest_bar_timestamp": now - timedelta(minutes=30),
        "bar_timestamps": [now - timedelta(hours=i) for i in range(6)],
        "nbbo_success_rate": 1.0, "rl_backfill_lag_hours": 0.0,
    }
    v = SLOMonitor().evaluate_data_integrity(payload)
    assert "dormant_market_closed" not in v.reasons  # crypto is never dormant
    assert v.severity == IntegritySeverity.OK        # and a fresh 30-min-old 1H bar is fine (T1)
