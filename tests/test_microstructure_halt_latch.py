"""PR-2 (operator-signed 2026-07-20) — halt-latch detector precision + recovery path.

THE INCIDENT. From 2026-07-13T20:22Z the microstructure guard reported
`STATE_UNSAFE reason=regulatory_halt halt_status=True` on QQQ while the tape was healthy —
spread 4.3e-05..1.1e-04 against a 0.015 limit, quote age 0.01..0.09s. QQQ was not halted on any of
those days. On Monday 07-20 it suppressed a REAL entry:

    13:46:41Z microstructure_entry_suppressed action=short halt_status=True
              leg=mean_reversion_qqq reason=regulatory_halt symbol=QQQ

The soak process had run continuously since Sat 07-18 07:00 (NRestarts=0) and the false latch
appeared at 13:33:25Z Monday, three minutes after the 13:30Z open.

TWO DEFECTS, both fixed here.

(a) DETECTOR. Three ingress paths could set the latch, not one:
      - note_trading_status via a bare substring scan over all four concatenated fields, so
        "NOT HALTED" / "UNHALTED" latch as readily as "HALTED";
      - note_quote and note_trade via HALT_CONDITION_TOKENS, which contained "M", "P" and "Q" —
        under CTA/UTP these are Market Center Official Close, Prior Reference Price, and Market
        Center Official Open. Routine prints, emitted every session.
    Both ingress paths also folded `tape` (a venue id: A/B/C) into the matched token set.

(b) RECOVERY. `regulatory_halt` cleared only on an explicit resume message arriving via
    note_trading_status. The quote and trade paths could SET it and had no way to clear it, so a
    latch set by a condition token was permanent for the life of the process. Second occurrence of
    this defect class after the six-day HARD_CRITICAL_DEGRADE latch.

ON THE VERBATIM 07-13 MESSAGE. The ruling asked for an adversarial fixture replaying the real
session-close message that set the latch. That message CANNOT be recovered: the pre-fix guard
logged neither the triggering message nor the latch transition — only the downstream
STATE_UNSAFE verdict — so it was never written anywhere. Nothing in the journal, the DBs, or the
depth cache retains it. Rather than invent one and label it real, the fixtures below replay the
realistic CANDIDATES for that message (official-close/open condition prints, session-close status
text, and the negation forms the substring scan mismatched). The verbatim-trigger telemetry added
in this PR is precisely what makes the next occurrence self-identifying; this gap is the reason
that requirement exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from structlog.testing import capture_logs

from src.engine.microstructure_guard import (
    MicrostructureGuard,
    MicrostructureGuardConfig,
)

T0 = datetime(2026, 7, 20, 13, 30, 0, tzinfo=timezone.utc)


class _NoDepthCache:
    def get(self, symbol):  # noqa: ANN001, ANN201 - test double
        return None


def _guard(**kwargs) -> MicrostructureGuard:
    return MicrostructureGuard(
        MicrostructureGuardConfig(**kwargs), depth_cache=_NoDepthCache()
    )


def _healthy_quote(guard: MicrostructureGuard, at: datetime, *, conditions=None) -> None:
    """A two-sided, tight, fresh QQQ quote — the live 5.7e-05 spread."""
    guard.note_quote(
        "QQQ",
        bid_price=500.00,
        ask_price=500.0285,
        timestamp=at,
        conditions=conditions if conditions is not None else ["R"],
        tape="C",
    )


# ── (a) DETECTOR PRECISION ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("condition", ["M", "P", "Q"])
def test_routine_cta_utp_conditions_do_not_latch(condition):
    """M/P/Q are official close / prior reference / official open. Not halts.

    This is the strongest candidate for the 07-20T13:33Z latch: three minutes after the open, on
    a process that had been up since Saturday.
    """
    guard = _guard()
    guard.note_trade("QQQ", price=500.0, timestamp=T0, conditions=[condition], tape="C")
    assert guard.evaluate("QQQ", now=T0).halt_status is False, (
        f"routine condition {condition!r} latched a regulatory halt"
    )


@pytest.mark.parametrize("condition", ["M", "P", "Q"])
def test_routine_conditions_do_not_latch_via_quotes_either(condition):
    guard = _guard()
    guard.note_quote(
        "QQQ", bid_price=500.0, ask_price=500.03, timestamp=T0, conditions=[condition], tape="C"
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is False


@pytest.mark.parametrize("tape", ["A", "B", "C"])
def test_tape_identifier_never_latches(tape):
    """The tape is a venue id and was being matched against the halt vocabulary."""
    guard = _guard()
    guard.note_trade("QQQ", price=500.0, timestamp=T0, conditions=["R"], tape=tape)
    guard.note_quote(
        "QQQ", bid_price=500.0, ask_price=500.03, timestamp=T0, conditions=["R"], tape=tape
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is False


@pytest.mark.parametrize(
    "status_message",
    [
        "Security is NOT HALTED",
        "UNHALTED - trading proceeding normally",
        "non-halted security",
    ],
)
def test_negated_halt_text_does_not_latch(status_message):
    """The bare substring scan latched on any message CONTAINING the token.

    status_code is deliberately EMPTY. An "ACTIVE" code would be caught by the resume branch
    first and the negation logic would never be reached — the test would pass for the wrong
    reason and prove nothing about the detector.
    """
    guard = _guard()
    guard.note_trading_status(
        "QQQ",
        status_code="",
        status_message=status_message,
        reason_code="",
        reason_message="",
        timestamp=T0,
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is False, (
        f"negated text latched: {status_message!r}"
    )


@pytest.mark.parametrize(
    "status_message",
    [
        "Market Center Official Close",
        "End of day session close",
        "Closing auction complete",
        "Regular trading session ended",
    ],
)
def test_session_close_text_does_not_latch(status_message):
    """Candidate reconstructions of the 07-13T20:22Z (16:22 ET, post-close) message."""
    guard = _guard()
    guard.note_trading_status(
        "QQQ",
        status_code="",
        status_message=status_message,
        reason_code="",
        reason_message="",
        timestamp=datetime(2026, 7, 13, 20, 22, 0, tzinfo=timezone.utc),
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is False


def test_temporally_ambiguous_text_errs_safe_then_self_clears():
    """The DELIBERATE limit of the detector, and why (a) and (b) are one PR.

    "Issue was halted earlier; NOT HALTED now" contains an unnegated "halted". Resolving
    earlier-vs-now would need temporal parsing of free text, which is not something a safety gate
    should be doing — so the detector errs safe and latches. That is only acceptable BECAUSE the
    recovery path now bounds the cost: a healthy tape clears it in one confirmation window instead
    of holding for the life of the process. Detector precision reduces false latches; recovery
    caps the damage of the ones that get through. Neither half is sufficient alone.
    """
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ",
        status_code="",
        status_message="Issue was halted earlier; NOT HALTED now",
        reason_code="",
        reason_message="",
        timestamp=T0,
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is True  # errs safe
    for offset in range(0, 61, 10):
        _healthy_quote(guard, T0 + timedelta(seconds=offset))
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=61)).halt_status is False  # bounded


# ── the guard must STILL catch real halts ─────────────────────────────────────────────────────
@pytest.mark.parametrize("code", ["H", "HALT", "HALTED", "SUSPENDED", "T1", "T2", "T5", "T12"])
def test_real_halt_status_codes_still_latch(code):
    guard = _guard()
    guard.note_trading_status(
        "QQQ", status_code=code, status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is True
    assert guard.evaluate("QQQ", now=T0).blocks_entries is True


@pytest.mark.parametrize(
    "message",
    ["TRADING HALTED", "Trading halt - news pending", "Security SUSPENDED by regulator"],
)
def test_real_halt_text_still_latches(message):
    guard = _guard()
    guard.note_trading_status(
        "QQQ", status_code="", status_message=message, reason_code="", reason_message="",
        timestamp=T0,
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is True


def test_explicit_halt_condition_token_still_latches():
    guard = _guard()
    guard.note_trade("QQQ", price=500.0, timestamp=T0, conditions=["H"], tape="C")
    assert guard.evaluate("QQQ", now=T0).halt_status is True


# ── (b) RECOVERY PATH ─────────────────────────────────────────────────────────────────────────
def test_latch_clears_on_sustained_positive_reverification():
    """The core fix: healthy tape for the confirmation window un-latches."""
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    assert guard.evaluate("QQQ", now=T0).halt_status is True
    for offset in range(0, 61, 10):
        _healthy_quote(guard, T0 + timedelta(seconds=offset))
    verdict = guard.evaluate("QQQ", now=T0 + timedelta(seconds=61))
    assert verdict.halt_status is False
    assert verdict.blocks_entries is False


def test_latch_does_not_clear_before_the_window_elapses():
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    for offset in (0, 10, 20, 30):
        _healthy_quote(guard, T0 + timedelta(seconds=offset))
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=30)).halt_status is True


def test_recovery_never_fires_on_elapsed_time_alone():
    """Rule 2. No quotes at all — the latch must hold however long we wait."""
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    for hours in (1, 6, 24, 24 * 7):
        verdict = guard.evaluate("QQQ", now=T0 + timedelta(hours=hours))
        assert verdict.halt_status is True, f"latch decayed on time alone after {hours}h"


def test_unknowable_status_holds_the_latch_and_resets_the_run():
    """Rule 3. A stale-quote gap mid-run must not count toward confirmation."""
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    guard.note_stream_bar("QQQ", bar_timestamp=T0)
    _healthy_quote(guard, T0)
    # 50s later the quote is stale relative to a fresh bar → unobservable
    guard.note_stream_bar("QQQ", bar_timestamp=T0 + timedelta(seconds=50))
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=50)).halt_status is True
    # a fresh healthy quote now restarts the run from zero, so the ORIGINAL t0 no longer counts
    _healthy_quote(guard, T0 + timedelta(seconds=55))
    guard.note_stream_bar("QQQ", bar_timestamp=T0 + timedelta(seconds=55))
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=70)).halt_status is True


def test_a_halt_flagged_quote_resets_the_confirmation_run():
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    for offset in (0, 10, 20, 30, 40, 50):
        _healthy_quote(guard, T0 + timedelta(seconds=offset))
    _healthy_quote(guard, T0 + timedelta(seconds=55), conditions=["H"])  # halt reasserted
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=61)).halt_status is True


def test_wide_spread_quotes_do_not_count_as_recovery_evidence():
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    for offset in range(0, 61, 10):
        guard.note_quote(  # 4% spread, far outside the 1.5% limit
            "QQQ", bid_price=490.0, ask_price=510.0,
            timestamp=T0 + timedelta(seconds=offset), conditions=["R"], tape="C",
        )
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=61)).halt_status is True


def test_one_sided_quotes_do_not_count_as_recovery_evidence():
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    for offset in range(0, 61, 10):
        guard.note_quote(
            "QQQ", bid_price=500.0, ask_price=0.0,
            timestamp=T0 + timedelta(seconds=offset), conditions=["R"], tape="C",
        )
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=61)).halt_status is True


def test_explicit_resume_message_clears_immediately():
    """The authoritative signal needs no corroboration window."""
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=T0,
    )
    guard.note_trading_status(
        "QQQ", status_code="ACTIVE", status_message="Trading resumed", reason_code="",
        reason_message="", timestamp=T0 + timedelta(seconds=5),
    )
    assert guard.evaluate("QQQ", now=T0 + timedelta(seconds=5)).halt_status is False


# ── the live incident, end to end ─────────────────────────────────────────────────────────────
def test_the_live_incident_would_have_self_cleared():
    """A latch set at the open, then a healthy session tape — the 07-20 shape.

    Pre-fix this stayed latched all day and suppressed the 13:46:41Z short. Post-fix the routine
    condition never latches; and even if something else had, the healthy tape clears it.
    """
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    open_ts = datetime(2026, 7, 20, 13, 33, 25, tzinfo=timezone.utc)
    guard.note_trade("QQQ", price=500.0, timestamp=open_ts, conditions=["Q"], tape="C")
    assert guard.evaluate("QQQ", now=open_ts).halt_status is False  # never latches now

    guard.note_trading_status(  # force a latch by another route
        "QQQ", status_code="HALTED", status_message="", reason_code="", reason_message="",
        timestamp=open_ts,
    )
    assert guard.evaluate("QQQ", now=open_ts).halt_status is True
    for offset in range(0, 121, 15):
        _healthy_quote(guard, open_ts + timedelta(seconds=offset))
    signal_ts = datetime(2026, 7, 20, 13, 46, 41, tzinfo=timezone.utc)
    verdict = guard.evaluate("QQQ", now=signal_ts)
    assert verdict.halt_status is False, "the 13:46:41Z entry would still be suppressed"
    assert verdict.blocks_entries is False


# ── TELEMETRY ─────────────────────────────────────────────────────────────────────────────────
def test_latch_set_logs_the_triggering_message_verbatim():
    guard = _guard()
    with capture_logs() as logs:
        guard.note_trading_status(
            "QQQ",
            status_code="T1",
            status_message="Halt - news dissemination",
            reason_code="NEWS",
            reason_message="pending release",
            timestamp=T0,
        )
    events = [e for e in logs if "HALT_LATCH_SET" in str(e.get("event", ""))]
    assert events, "the latch transition must be logged"
    trigger = events[0]["trigger"]
    for fragment in ("T1", "Halt - news dissemination", "NEWS", "pending release"):
        assert fragment in trigger, f"{fragment!r} missing from the verbatim trigger"


def test_latch_clear_logs_the_reason_and_the_original_trigger():
    guard = _guard(halt_recovery_confirmation_seconds=60.0)
    guard.note_trading_status(
        "QQQ", status_code="HALTED", status_message="operator drill", reason_code="",
        reason_message="", timestamp=T0,
    )
    for offset in range(0, 61, 10):
        _healthy_quote(guard, T0 + timedelta(seconds=offset))
    with capture_logs() as logs:
        guard.evaluate("QQQ", now=T0 + timedelta(seconds=61))
    events = [e for e in logs if "HALT_LATCH_CLEARED" in str(e.get("event", ""))]
    assert events, "the clear transition must be logged"
    assert events[0]["recovery_reason"] == "positive_reverification"
    assert "operator drill" in events[0]["set_by_trigger"]


def test_latch_set_is_logged_once_not_every_message():
    guard = _guard()
    with capture_logs() as logs:
        for seconds in range(5):
            guard.note_trading_status(
                "QQQ", status_code="HALTED", status_message="", reason_code="",
                reason_message="", timestamp=T0 + timedelta(seconds=seconds),
            )
    events = [e for e in logs if "HALT_LATCH_SET" in str(e.get("event", ""))]
    assert len(events) == 1, "the latch transition must log once, not per message"
