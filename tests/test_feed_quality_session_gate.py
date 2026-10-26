"""W2 — DORMANT IS NOT DISCONNECTED (T4 clause 4, 4th instance).

A closed equity WebSocket is correct behaviour — there is nothing to stream. It must contribute NO
feed-health sample of any kind (not degraded, not healthy — NO SAMPLE), identical to U1b's NBBO
ruling and T2e's bar-freshness ruling. The dormancy predicate is now ONE shared definition
(`SLOMonitor.is_market_dormant`) used by both the SLO path and the orchestrator's feed-quality path,
so no health/quality path can sample a closed market.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.engine.slo_monitor import SLOMonitor

# 2026-06-24 is a normal trading Wednesday.
RTH = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)      # 11:00 ET — equity market OPEN
CLOSED = datetime(2026, 6, 24, 2, 0, tzinfo=timezone.utc)    # 02:00 ET — equity market CLOSED


def test_dormancy_predicate_is_the_one_shared_definition():
    m = SLOMonitor()
    # a closed equity venue is dormant
    assert m.is_market_dormant("stock", CLOSED) is True
    # an open equity venue is NOT dormant
    assert m.is_market_dormant("stock", RTH) is False
    # crypto is NEVER dormant, open or "closed" wall-clock
    assert m.is_market_dormant("crypto", CLOSED) is False
    assert m.is_market_dormant("crypto", RTH) is False


def test_slo_and_feed_paths_agree_by_construction():
    """The feed-quality path (orchestrator) and the SLO path both call is_market_dormant, so they
    cannot disagree about whether a leg is dormant — the four-instances-of-the-same-bug class is
    closed structurally, not patched a fifth time."""
    m = SLOMonitor()
    # the SLO path's own dormancy early-return is driven by the SAME predicate
    verdict = m.evaluate_data_integrity({
        "exchange_reference_ts": CLOSED, "asset_class": "stock", "timeframe": "15Min",
        "symbol": "QQQ", "latest_bar_timestamp": CLOSED,
        "bar_timestamps": [CLOSED], "ingest_arrival_lag_seconds": 999.0,
    })
    assert "dormant_market_closed" in verdict.reasons
    assert verdict.passed is True
    # and the predicate the orchestrator gate consults returns the same answer for that leg
    assert m.is_market_dormant("stock", CLOSED) is True


def test_closed_equity_records_no_health_sample_of_any_kind():
    """W2b — MAKE IT UNREPRESENTABLE. Even with a fabricated feed_degraded / arrival-lag / nbbo
    failure in the payload, a closed equity leg yields the dormant verdict — none of those health
    signals are read, so a closed market cannot produce a breach on ANY of them."""
    m = SLOMonitor()
    verdict = m.evaluate_data_integrity({
        "exchange_reference_ts": CLOSED, "asset_class": "stock", "timeframe": "15Min",
        "symbol": "QQQ", "latest_bar_timestamp": CLOSED, "bar_timestamps": [CLOSED],
        "ingest_arrival_lag_seconds": 999.0,          # would HARD-breach if read
        "ingest_circuit_breaker_tripped": True,        # would SOFT-breach if read
        "nbbo_success_rate": 0.0,                      # would HARD-breach if read
    })
    assert verdict.reasons == ("dormant_market_closed",)
    assert verdict.severity.name == "OK"
