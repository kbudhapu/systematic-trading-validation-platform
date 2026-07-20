"""P3.1 — EmailNotifier + CompositeNotifier. Mock SMTP only; no real send ever.

Locks the invariant this task exists for: an alert that cannot be emailed must still
land in the durable log (never silently swallowed).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from src.alerts.email_dispatcher import (
    CompositeNotifier, EmailNotifier, build_log_and_email,
)

_CFG = {"smtp_host": "smtp.test", "smtp_port": 587, "smtp_user": "u",
        "smtp_password": "p", "email_from": "a@test", "email_to": "b@test",
        "timeout_s": 5.0}
_ALERT = {"kind": "heartbeat_stale", "severity": "critical",
          "message": "soak heartbeat stale", "detail": {"age_seconds": 999}}


class _SpyFallback:
    def __init__(self):
        self.calls = []

    def notify(self, alert):
        self.calls.append(alert)


def test_deliver_success_no_fallback():
    sent = []

    async def send_fn(subject, body, cfg):
        sent.append((subject, body))

    fb = _SpyFallback()
    n = EmailNotifier(fb, send_fn=send_fn, config=dict(_CFG))
    assert asyncio.run(n.deliver(_ALERT)) is True
    assert len(sent) == 1 and "critical".upper() in sent[0][0].upper()
    assert fb.calls == []  # no fallback on success


def test_deliver_retries_once_then_falls_back():
    attempts = []

    async def send_fn(subject, body, cfg):
        attempts.append(1)
        raise RuntimeError("smtp down")

    fb = _SpyFallback()
    n = EmailNotifier(fb, send_fn=send_fn, config=dict(_CFG))
    assert asyncio.run(n.deliver(_ALERT)) is False
    assert len(attempts) == 2  # one retry
    assert len(fb.calls) == 1
    assert fb.calls[0]["detail"].get("email_delivery_failed") is True


def test_timeout_triggers_fallback():
    async def slow_send(subject, body, cfg):
        await asyncio.sleep(0.2)

    fb = _SpyFallback()
    n = EmailNotifier(fb, timeout_s=0.03, send_fn=slow_send, config={**_CFG, "timeout_s": 0.03})
    assert asyncio.run(n.deliver(_ALERT)) is False
    assert fb.calls and fb.calls[0]["detail"].get("email_delivery_failed") is True


def test_not_configured_falls_back_without_send():
    called = []

    async def send_fn(subject, body, cfg):
        called.append(1)

    fb = _SpyFallback()
    n = EmailNotifier(fb, send_fn=send_fn, config={**_CFG, "smtp_host": ""})
    assert asyncio.run(n.deliver(_ALERT)) is False
    assert called == []  # never attempted
    assert fb.calls[0]["detail"].get("email_not_configured") is True


def test_missing_aiosmtplib_degrades_to_fallback():
    """The default send path lazily imports aiosmtplib; if it's absent, deliver must
    fall back rather than raise (import error is caught like any send failure)."""
    fb = _SpyFallback()
    n = EmailNotifier(fb, config=dict(_CFG))  # default real send_fn
    # aiosmtplib is not installed in this env -> import inside send raises -> fallback
    assert asyncio.run(n.deliver(_ALERT)) is False
    assert fb.calls and fb.calls[0]["detail"].get("email_delivery_failed") is True


def test_notify_is_nonblocking_in_running_loop():
    delivered = []

    async def send_fn(subject, body, cfg):
        delivered.append(1)

    async def scenario():
        n = EmailNotifier(_SpyFallback(), send_fn=send_fn, config=dict(_CFG))
        n.notify(_ALERT)              # must return immediately, not block
        await asyncio.sleep(0.05)     # let the scheduled task run
    asyncio.run(scenario())
    assert delivered == [1]


def test_boot_safe_under_empty_env(tmp_path, monkeypatch):
    """B4: with NO SMTP config, construction must NOT raise or connect, and an alert must still
    reach the durable LogNotifier row. The soak boots even with no alert path."""
    import sqlite3
    for k in list(os.environ):
        if k.startswith("SMTP") or k.startswith("EMAIL"):
            monkeypatch.delenv(k, raising=False)
    from src.persistence.db import init_db
    from src.alerts.email_dispatcher import build_log_and_email
    db = tmp_path / "trading.db"
    init_db(db)
    comp = build_log_and_email(db)                      # must not raise, must not connect
    assert comp is not None

    async def scenario():
        comp.notify(_ALERT)
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM operator_alerts").fetchone()[0] >= 1


def test_composite_fans_out_and_isolates_failures():
    class _Boom:
        def notify(self, alert):
            raise RuntimeError("channel down")

    a, b = _SpyFallback(), _SpyFallback()
    comp = CompositeNotifier(a, _Boom(), b)
    comp.notify(_ALERT)               # must not raise despite the middle channel
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_build_log_and_email_persists_durable_row_and_pages(tmp_path, monkeypatch):
    import sqlite3

    for k, v in {"SMTP_HOST": "smtp.test", "EMAIL_FROM": "a@test", "EMAIL_TO": "b@test"}.items():
        monkeypatch.setenv(k, v)
    sent = []

    async def send_fn(subject, body, cfg):
        sent.append(subject)

    db = tmp_path / "research_vault.db"
    comp = build_log_and_email(db, send_fn=send_fn)

    async def scenario():
        comp.notify(_ALERT)
        await asyncio.sleep(0.05)
    asyncio.run(scenario())

    # durable log row landed (LogNotifier) ...
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM operator_alerts").fetchone()[0]
    assert rows >= 1
    # ... and the email was paged
    assert len(sent) == 1


def test_composite_writes_exactly_one_durable_row_per_alert(tmp_path, monkeypatch):
    """F4: with email UNCONFIGURED, one alert must produce exactly ONE operator_alerts row, not two.
    Previously EmailNotifier's not-configured fallback re-wrote the same durable row the composite's
    own LogNotifier leg already wrote -> a double row (and a double phone buzz per ping)."""
    import sqlite3
    for k in list(os.environ):
        if k.startswith("SMTP") or k.startswith("EMAIL"):
            monkeypatch.delenv(k, raising=False)
    from src.persistence.db import init_db
    db = tmp_path / "research_vault.db"
    init_db(db)
    comp = build_log_and_email(db)          # email unconfigured -> null fallback (no double-write)

    async def scenario():
        comp.notify(_ALERT)
        await asyncio.sleep(0.05)
    asyncio.run(scenario())

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM operator_alerts WHERE kind = 'heartbeat_stale'").fetchone()[0]
    assert rows == 1                        # EXACTLY one, not two
