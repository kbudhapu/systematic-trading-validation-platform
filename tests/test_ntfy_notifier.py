"""B5 — ntfy push notifier: boot-safe, alerts-only, and the BINDING security fence.

The channel is PUBLIC. These tests lock, structurally, that the payload builder can never emit a
financial figure, that the notifier never raises / self-disables without NTFY_TOPIC, and that an
alert always still reaches the durable LogNotifier row.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from src.alerts.ntfy_notifier import (
    NtfyNotifier, NtfyRateLimited, _http_post_ntfy, build_liveness_ping, build_ntfy_payload,
)


def test_security_fence_strips_forbidden_financial_fields():
    """A malicious/careless alert carrying money must NOT put any of it on the public topic."""
    alert = {
        "kind": "heartbeat_stale", "severity": "critical",
        "message": "soak balance $50,123.45 down",           # money in free text
        "detail": {
            "component": "soak", "heartbeat_age_min": 12,     # allowed liveness facts
            "equity": 50123.45, "realized_pnl": -1234.0, "filled_price": 512.33,
            "position_qty": 100, "symbol": "QQQ", "api_key": "sk-secret", "cash": 9999,
        },
    }
    title, body, priority = build_ntfy_payload(alert)
    blob = (title + " " + body).lower()
    for forbidden in ("equity", "pnl", "filled_price", "position", "symbol", "api_key", "cash",
                      "50123", "1234", "512.33", "qqq", "sk-secret", "9999"):
        assert forbidden not in blob, f"SECURITY LEAK: {forbidden!r} reached the public payload"
    # the allowed liveness facts DO survive
    assert "component=soak" in body and "heartbeat_age_min=12" in body


def test_liveness_ping_is_counts_only():
    a = build_liveness_ping(cadence_min=5, candidate_count=13, feed="sip", epoch="PHASE0_SIP")
    _, body, _ = build_ntfy_payload(a)
    assert "13 candidates" in body and "feed=sip" in body and "epoch=PHASE0_SIP" in body
    # no money keys anywhere
    assert not any(w in body.lower() for w in ("equity", "pnl", "price", "balance"))


def test_liveness_ping_non_trading_day_says_market_closed():
    """P3: on a weekend/holiday the ping still fires and its text says market CLOSED (counts-only),
    so silence always means 'go look' rather than 'it's the weekend'."""
    a = build_liveness_ping(cadence_min=5, candidate_count=0, feed="sip", epoch="PHASE0_SIP",
                            trading_day=False)
    _, body, _ = build_ntfy_payload(a)
    assert "market CLOSED" in body and "alive" in body
    assert "candidates" not in body        # no screen expected -> not reported
    assert a["detail"]["trading_day"] is False
    assert not any(w in body.lower() for w in ("equity", "pnl", "price", "balance"))


def test_disabled_without_topic_never_posts_or_raises(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    posted = []
    n = NtfyNotifier(post_fn=lambda *a: posted.append(a))
    assert n.enabled() is False
    n.notify({"kind": "x", "severity": "info", "message": "m", "detail": {}})  # no-op, no raise
    assert posted == []


def test_post_failure_never_raises(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "mbappe-test")

    def boom(*a):
        raise RuntimeError("network down")

    n = NtfyNotifier(post_fn=boom)
    n.notify({"kind": "x", "severity": "critical", "message": "m", "detail": {}})  # swallowed


def test_enabled_posts_once(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "mbappe-test")
    posted = []
    n = NtfyNotifier(post_fn=lambda topic, title, body, prio: posted.append((topic, title, body, prio)))
    n.notify(build_liveness_ping(cadence_min=5, candidate_count=13, feed="sip", epoch="PHASE0_SIP"))
    assert len(posted) == 1 and posted[0][0] == "mbappe-test"


def test_transport_surfaces_429_as_rate_limited(monkeypatch):
    """F5: httpx.post does NOT raise on 429, so the transport must surface it explicitly -- otherwise
    a rate-limited push is a SILENT DROP. (Opt in past the test interlock; httpx is mocked.)"""
    monkeypatch.setenv("MBAPPE_ALLOW_REAL_NOTIFY", "1")
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: SimpleNamespace(status_code=429))
    with pytest.raises(NtfyRateLimited):
        _http_post_ntfy("topic", "title", "body", "urgent")


def test_notify_swallows_rate_limit_but_stays_loud(monkeypatch):
    """F5: a 429 must be LOGGED LOUDLY and never raise into the watchdog (the durable operator_alerts
    row is retained by the LogNotifier leg regardless)."""
    monkeypatch.setenv("NTFY_TOPIC", "mbappe-test")

    def rate_limited(*a):
        raise NtfyRateLimited("HTTP 429")

    NtfyNotifier(post_fn=rate_limited).notify(
        {"kind": "heartbeat_stale", "severity": "critical", "message": "m", "detail": {}})  # no raise


def test_construction_never_connects_and_composite_is_boot_safe(tmp_path, monkeypatch):
    """B5 boot-safety (same as B4): empty env -> build_log_and_email does not raise/connect and the
    alert still reaches the durable LogNotifier row."""
    import sqlite3
    for k in list(os.environ):
        if k.startswith("NTFY") or k.startswith("SMTP") or k.startswith("EMAIL"):
            monkeypatch.delenv(k, raising=False)
    from src.persistence.db import init_db
    from src.alerts.email_dispatcher import build_log_and_email
    db = tmp_path / "trading.db"
    init_db(db)
    comp = build_log_and_email(db)               # constructs Log+Email+Ntfy, all disabled, no raise

    async def scenario():
        comp.notify({"kind": "heartbeat_stale", "severity": "critical", "message": "m", "detail": {}})
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM operator_alerts").fetchone()[0] >= 1
