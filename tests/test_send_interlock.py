"""F1 (M3-RED-2) — the test suite must NEVER page a human.

Root cause: with NTFY_TOPIC present in the environment (D8 added it to the Ryzen .env), running
the suite constructed real NtfyNotifier/EmailNotifier instances that sent REAL pushes to the
operator's phone (the 999 fixture, the 18000s stale alert) and burned the ntfy rate limit. These
tests lock the interlock: with NTFY_TOPIC set, running code performs ZERO outbound HTTP to ntfy.sh
and ZERO real SMTP -- asserted ON THE TRANSPORT, not on a flag. Injected mock transports are
untouched, so "the notifier sends" tests still work against their mocks.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import src.alerts.ntfy_notifier as ntfy_mod
from src.alerts.email_dispatcher import EmailNotifier
from src.alerts.ntfy_notifier import NtfyNotifier, _http_post_ntfy
from src.alerts.send_interlock import running_under_test

_CFG = {"smtp_host": "smtp.test", "smtp_port": 587, "smtp_user": "u", "smtp_password": "p",
        "email_from": "a@test", "email_to": "b@test", "timeout_s": 5.0}
_ALERT = {"kind": "heartbeat_stale", "severity": "critical",
          "message": "soak heartbeat stale", "detail": {"age_seconds": 999, "component": "soak"}}


def test_running_under_test_is_true_here():
    assert running_under_test() is True                     # pytest is imported


def test_allow_flag_disables_interlock(monkeypatch):
    monkeypatch.setenv("MBAPPE_ALLOW_REAL_NOTIFY", "1")
    assert running_under_test() is False                    # explicit, deliberate opt-in


def test_real_transport_makes_zero_http_under_suite(monkeypatch):
    """THE acceptance test: the real ntfy transport hits the network ZERO times under pytest."""
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append((a, k)))
    _http_post_ntfy("any-topic", "t", "b", "default")       # real transport, real code path
    assert calls == []                                      # NOT ONE outbound HTTP


def test_notifier_self_disables_real_transport_under_suite(monkeypatch):
    """NtfyNotifier with a live topic + the REAL transport sends nothing under the suite."""
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    NtfyNotifier(topic="live-topic").notify(_ALERT)         # default (real) transport
    assert calls == []


def test_injected_mock_transport_still_fires(monkeypatch):
    """The interlock blocks ONLY the real transport -- an injected mock (the 'assert it sends'
    tests) is left free, otherwise every notifier test would go dark."""
    posted = []
    NtfyNotifier(topic="live-topic", post_fn=lambda *a: posted.append(a)).notify(_ALERT)
    assert len(posted) == 1


def test_allow_flag_lets_real_transport_through(monkeypatch):
    """With the explicit opt-in, the real transport IS used (proves the block is the interlock,
    not something else). httpx is mocked so no actual network call leaves the box."""
    monkeypatch.setenv("MBAPPE_ALLOW_REAL_NOTIFY", "1")
    calls = []

    def fake_post(*a, **k):
        calls.append((a, k))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "post", fake_post)
    _http_post_ntfy("t", "title", "body", "default")
    assert len(calls) == 1                                  # interlock off -> transport used


def test_email_default_send_blocked_under_suite():
    """EmailNotifier's real SMTP send is blocked under the suite; the alert still lands via the
    fallback (never silently dropped)."""
    calls = []

    class _Fallback:
        def notify(self, alert):
            calls.append(alert)

    n = EmailNotifier(_Fallback(), config=dict(_CFG))       # default real send_fn
    assert asyncio.run(n.deliver(_ALERT)) is False          # blocked -> not delivered
    assert calls and calls[0]["detail"].get("email_delivery_failed") is True
