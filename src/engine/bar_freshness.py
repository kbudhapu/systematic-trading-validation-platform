"""G3 — the ONE shared bar-staleness measurement. No exceptions.

A bar is stamped at its OPEN (Alpaca convention), and it only becomes COMPLETE at its close
(bar_open + bar_period). "How stale is the data" therefore means "how long since the bar closed",
NOT "how long since it opened". Measuring from the open forces an unsatisfiable threshold, which the
cold-start gate then compensated for with an undocumented `bar_period` floor (commit 9649b1c) that
left only 5s of post-close delivery slack and flapped at every bar boundary.

EVERY staleness / freshness check routes through `bar_staleness_seconds`. Thresholds are then declared
in POST-CLOSE DELIVERY LATENCY — the only unit that means anything. Making the measurement shared makes
the open-vs-close mistake UNREPRESENTABLE, not merely fixed (a test asserts no site computes bar age
independently -- the same shape as assert_config_fully_consumed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _coerce_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def bar_staleness_seconds(bar_open_ts: datetime, bar_period_seconds: float,
                          now: datetime) -> float:
    """Seconds since the bar CLOSED -- since the data became complete.

    `bar_open_ts` is the bar's open timestamp (as the feed stamps it); `bar_period_seconds` is the
    bar interval (900 for 15Min, 3600 for 1H); `now` is the reference instant (the CALLER supplies a
    calendar-adjusted reference where a closed market must not count as staleness). A freshly-closed
    bar reads ~its delivery latency (tens of seconds); a bar one full period missed reads ~bar_period.
    Clamped at 0 (a not-yet-closed bar is not "stale")."""
    bar_close = _coerce_utc(bar_open_ts) + timedelta(seconds=float(bar_period_seconds))
    return max((_coerce_utc(now) - bar_close).total_seconds(), 0.0)


def stale_after_seconds(bar_period_seconds: float, delivery_latency_seconds: float) -> float:
    """T1 — the staleness-since-close threshold at which the feed is STALE, expressed PERIOD-
    RELATIVELY so the mistake of comparing an absolute second-count against a longer-period producer
    is unrepresentable.

    `bar_staleness_seconds` measures how long since the LATEST bar closed. For a healthy feed that
    grows from ~delivery-latency (just after a close) up to ~one bar period (just before the next
    closes) -- so a bare absolute threshold BELOW the bar period (e.g. 600s vs a 3600s bar) fires for
    most of every period by construction. The feed is only genuinely stale when the NEXT bar is
    OVERDUE: staleness > one full bar period (the expected inter-bar gap) + the post-close delivery
    latency. `delivery_latency_seconds` is a DELIVERY property (how late may a bar arrive after its
    close), INDEPENDENT of the bar period; the period is added here, once, structurally.

    This is the fourth instance of the same bug (soak heartbeat 900/120; cold-start 900/905 floor;
    max(65, bar_period+5); SLO bar_freshness 3600/600). Every freshness threshold now routes through
    here, so a threshold can never again be a bare absolute bar age."""
    return float(bar_period_seconds) + float(delivery_latency_seconds)
