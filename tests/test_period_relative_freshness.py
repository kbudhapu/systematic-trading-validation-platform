"""T1 — freshness thresholds are PERIOD-RELATIVE, and the absolute-bar-age mistake is unrepresentable.

The bug, four times: soak heartbeat (900s producer / 120s threshold), cold-start gate (900/905 floor),
max(65, bar_period+5), SLO bar_freshness (3600s bar / 600s absolute). A threshold below the producer's
period fires for most of every period by construction. Every freshness threshold now routes through
bar_freshness.stale_after_seconds(period, delivery_latency), so it can never again be an absolute age.
"""
from __future__ import annotations

import inspect

from src.engine.bar_freshness import bar_staleness_seconds, stale_after_seconds


def test_stale_threshold_scales_with_the_bar_period():
    # the SAME delivery latency yields thresholds that differ by EXACTLY the period difference
    n = 65.0
    assert stale_after_seconds(3600.0, n) - stale_after_seconds(900.0, n) == 2700.0
    assert stale_after_seconds(3600.0, n) == 3600.0 + n
    assert stale_after_seconds(900.0, n) == 900.0 + n


def test_slo_bar_freshness_is_period_relative():
    from datetime import datetime, timedelta, timezone

    from src.engine.slo_monitor import (
        BAR_DELIVERY_LATENCY_HARD_SECONDS, IntegritySeverity, SLOMonitor,
    )

    now = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)  # within RTH
    P = 3600.0  # 1Hour period

    def _open_for_staleness(staleness_s: float):
        # latest_bar_timestamp is the bar's OPEN; staleness = now - (open + period)
        return now - timedelta(seconds=staleness_s + P)

    def _payload(staleness_s: float) -> dict:
        op = _open_for_staleness(staleness_s)
        return {
            "exchange_reference_ts": now, "asset_class": "crypto", "timeframe": "1Hour",
            "latest_bar_timestamp": op,
            "bar_timestamps": [op - timedelta(seconds=P * i) for i in range(6)],
            "nbbo_success_rate": 1.0, "rl_backfill_lag_hours": 0.0,
        }

    # a 1Hour bar that closed 52 min ago -> was HARD_BREACH pre-T1 (3120s > 600s abs); now OK because
    # the next hourly bar is not yet overdue (3120 < period + SOFT latency).
    v = SLOMonitor().evaluate_data_integrity(_payload(52 * 60))
    assert v.severity == IntegritySeverity.OK, v.reasons
    assert "bar_freshness_critical" not in v.reasons

    # genuinely overdue: staleness > one period + the HARD delivery latency -> HARD
    v2 = SLOMonitor().evaluate_data_integrity(_payload(P + BAR_DELIVERY_LATENCY_HARD_SECONDS + 120))
    assert v2.severity == IntegritySeverity.HARD_BREACH
    assert "bar_freshness_critical" in v2.reasons


def test_cold_start_max_bar_age_is_period_relative():
    from src.engine.cold_start_gate import ColdStartGateConfig, max_bar_age_for_timeframe

    cfg = ColdStartGateConfig()  # max_bar_age_seconds default 65
    assert max_bar_age_for_timeframe("15Min", cfg) == 900.0 + 65.0
    assert max_bar_age_for_timeframe("1Hour", cfg) == 3600.0 + 65.0
    # the 1Hour threshold is larger by exactly the period difference — period-relative, not absolute
    assert (max_bar_age_for_timeframe("1Hour", cfg)
            - max_bar_age_for_timeframe("15Min", cfg)) == 2700.0


def test_no_freshness_site_compares_against_a_bare_absolute_constant():
    """UNREPRESENTABLE (T1c): the SLO and cold-start freshness comparisons must route their threshold
    through stale_after_seconds — never a bare absolute BAR_*_SECONDS / max_bar_age constant. Same
    shape as assert_config_fully_consumed and H1a's forming bar."""
    import src.engine.cold_start_gate as csg
    import src.engine.slo_monitor as slo

    slo_src = inspect.getsource(slo.SLOMonitor.evaluate_data_integrity)
    assert "stale_after_seconds(" in slo_src
    # the raw absolute compares are gone
    assert "> BAR_FRESHNESS_SOFT_SECONDS" not in slo_src
    assert "> BAR_FRESHNESS_HARD_SECONDS" not in slo_src

    csg_src = inspect.getsource(csg.max_bar_age_for_timeframe)
    assert "stale_after_seconds(" in csg_src
