"""F1 — HARD INTERLOCK: the test suite must NEVER page a human.

Root cause (2026-07-13): D8 added ``NTFY_TOPIC`` to the Ryzen ``.env``; running the full
suite then constructed *real* ``NtfyNotifier`` / ``EmailNotifier`` instances that read the
now-present env var and sent REAL pushes to the operator's phone (e.g. ``age_seconds=999``
from ``test_email_dispatcher``'s fixture, ``main-loop heartbeat stale (soak)`` with
``18000s`` from ``test_liveness_watchdogs``'s 5-hour-stale helper). Repeated runs burned the
ntfy rate limit, so the real droplet->phone alert was throttled. This is the 07-07 failure
INVERTED: instead of a real alert reaching nobody, FAKE alerts reached the operator until he
would stop looking.

The interlock lives in the notifiers themselves so no test -- present or future, written by
anyone -- can page a human by accident. It blocks only the REAL outbound transports
(``_http_post_ntfy`` / ``_aiosmtplib_send``); injected mock transports are untouched, so a
test that asserts "the notifier sends" still works against its mock.

Detection is deliberately broad (``pytest`` imported OR ``PYTEST_CURRENT_TEST`` set) so it
holds during collection, fixtures, and test bodies alike. Production (systemd venv python)
never imports pytest, so real paging is unaffected. ``MBAPPE_ALLOW_REAL_NOTIFY=1`` is an
explicit, deliberate escape hatch for a hand-run integration test.
"""
from __future__ import annotations

import os
import sys

import structlog

log = structlog.get_logger()

_ALLOW_ENV = "MBAPPE_ALLOW_REAL_NOTIFY"
_blocked_logged: set[str] = set()


def running_under_test() -> bool:
    """True when a real outbound notification MUST be suppressed (test context).

    Broad on purpose: ``pytest`` in ``sys.modules`` covers collection/fixtures where
    ``PYTEST_CURRENT_TEST`` is not yet set. The explicit allow-flag lets a deliberate
    integration test opt back in."""
    if os.environ.get(_ALLOW_ENV) == "1":
        return False
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def log_blocked_once(channel: str) -> None:
    """Log the interlock firing exactly once per channel (avoids log spam under the suite)."""
    if channel not in _blocked_logged:
        _blocked_logged.add(channel)
        log.warning("real_notify_blocked_under_test", channel=channel,
                    reason="test context detected; refusing real outbound send",
                    override=f"set {_ALLOW_ENV}=1 to allow")
