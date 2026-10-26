"""S2 — the live-trading SLO admits ONLY live-execution-safety signals.

rl_backfill_lag is a research/ML metric (the shadow RL policy's reward backfill; the shadow policy
does not trade). A stale reward backfill says nothing about the price, the broker, or the feed, so
it must NEVER appear in the live-trading SLO verdict. It is monitored + alerted on its own research
channel instead. Evidence: rl_backfill_lag held the live engine in SOFT_DEGRADE and, via the
HARD-recovery reset, blocked its exit from six days of force-flatten.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.engine.slo_monitor import (
    IntegritySeverity,
    RL_BACKFILL_LAG_HARD_HOURS,
    SLOMonitor,
)


def _healthy_payload(rl_backfill_lag_hours: float) -> dict:
    now = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)  # within RTH (11:00 ET)
    latest = now - timedelta(seconds=30)                     # freshly-closed bar
    bars = [now - timedelta(minutes=15 * i) for i in range(6)][::-1]
    return {
        "exchange_reference_ts": now,
        "asset_class": "stock",
        "timeframe": "15Min",
        "latest_bar_timestamp": latest,
        "bar_timestamps": bars,
        "nbbo_success_rate": 1.0,
        "ingest_arrival_lag_seconds": 5.0,
        "rl_backfill_lag_hours": rl_backfill_lag_hours,
    }


def test_rl_backfill_lag_does_not_appear_in_the_live_slo_verdict():
    monitor = SLOMonitor()
    # a 200h-stale reward backfill (far past the former HARD threshold) with everything else healthy
    verdict = monitor.evaluate_data_integrity(_healthy_payload(200.0))
    assert verdict.severity == IntegritySeverity.OK, verdict.reasons
    assert verdict.passed is True
    assert "rl_backfill_lag" not in verdict.reasons
    assert "rl_backfill_lag_critical" not in verdict.reasons
    # still measured + carried for reporting
    assert verdict.rl_backfill_lag_hours == 200.0


def test_research_pipeline_health_alerts_off_the_order_path():
    monitor = SLOMonitor()
    now = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)
    healthy = monitor.evaluate_research_pipeline_health(reference_ts=now, rl_backfill_lag_hours=1.0)
    assert healthy["healthy"] is True and healthy["alert"] is False

    stale = monitor.evaluate_research_pipeline_health(
        reference_ts=now, rl_backfill_lag_hours=RL_BACKFILL_LAG_HARD_HOURS + 100.0)
    assert stale["alert"] is True and stale["healthy"] is False
    assert stale["rl_backfill_lag_hours"] == RL_BACKFILL_LAG_HARD_HOURS + 100.0


def test_genuine_nbbo_hard_breach_still_engages():
    """S2 must not weaken the LIVE signals — a real nbbo failure still HARD-breaches."""
    monitor = SLOMonitor()
    payload = _healthy_payload(0.0)
    payload["nbbo_success_rate"] = 0.1     # broker/quote genuinely failing (< NBBO_SUCCESS_HARD 0.55)
    verdict = monitor.evaluate_data_integrity(payload)
    assert verdict.severity == IntegritySeverity.HARD_BREACH
    assert "nbbo_fetch_critical" in verdict.reasons
