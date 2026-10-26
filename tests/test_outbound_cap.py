"""AG5 — the shared outbound cap. The soak's degrade storm ate the collector's dead-man ping budget
(July 7); this pins the cap, the reserved priority lane, and the 429 backoff. Both directions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.alerts.outbound_cap import OutboundCap


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    def __call__(self):
        return self.now


def _cap(tmp_path, **kw):
    clk = _Clock()
    cap = OutboundCap(tmp_path / "alert_channel.db", clock=clk, **kw)
    return cap, clk


# --- AG2: the hard routine cap ---

def test_routine_capped_at_hourly_limit(tmp_path):
    cap, _ = _cap(tmp_path, max_routine_per_hour=8)
    allowed = [cap.allow("degrade", "info")[0] for _ in range(10)]
    assert allowed.count(True) == 8 and allowed[8] is False and allowed[9] is False


def test_healthy_single_ping_is_not_suppressed(tmp_path):
    cap, _ = _cap(tmp_path)
    allowed, reason = cap.allow("daily_liveness", "default")
    assert allowed is True
    assert cap.stats()["sends_today"] == 1  # normal operation not broken


# --- AG3: the priority lane the routine chatter cannot exhaust ---

def test_daily_ping_survives_routine_exhaustion(tmp_path):
    cap, _ = _cap(tmp_path, max_routine_per_hour=8)
    for _ in range(50):
        cap.allow("degrade", "info")          # flood routine well past its cap
    ok, reason = cap.allow("daily_liveness", "default")  # the dead-man
    assert ok is True, "the daily ping must never be starved by routine noise"
    ok2, _ = cap.allow("some_page", "urgent")            # critical/urgent also priority
    assert ok2 is True


# --- AG4: a 429 backs off routine HARD, but never the priority lane ---

def test_429_backs_off_routine_but_not_priority(tmp_path):
    cap, clk = _cap(tmp_path, backoff_s=3600.0)
    cap.note_429("degrade")
    assert cap.allow("degrade", "info") == (False, "channel_backoff_after_429")
    assert cap.allow("daily_liveness", "default")[0] is True   # dead-man still goes
    # after the backoff window, routine resumes
    clk.now = clk.now + timedelta(seconds=3601)
    assert cap.allow("degrade", "info")[0] is True


def test_stats_expose_channel_health(tmp_path):
    cap, _ = _cap(tmp_path)
    cap.allow("daily_liveness", "default")
    cap.allow("degrade", "info")
    cap.note_429("degrade")
    st = cap.stats()
    assert st["sends_last_hour"] == 2 and st["sends_today"] == 2
    assert st["last_429_at"] is not None and st["cap_routine_per_hour"] >= 1


def test_cap_fails_open_on_db_error(tmp_path):
    cap, _ = _cap(tmp_path)
    cap._db_path = "/root/nonexistent_dir/cannot_write.db"   # force a DB error
    ok, reason = cap.allow("degrade", "info")
    assert ok is True and reason == "cap_error_fail_open"  # a cap bug must never silence alerts


# --- the NtfyNotifier integration (suppress the phone, never the record) ---

def test_notifier_suppresses_over_cap_and_logs(tmp_path, monkeypatch):
    import src.alerts.ntfy_notifier as nn
    sent = []
    logged = []
    monkeypatch.setattr(nn.log, "warning", lambda evt, **kw: logged.append((evt, kw)))
    cap = OutboundCap(tmp_path / "c.db", max_routine_per_hour=3, clock=_Clock())
    notifier = nn.NtfyNotifier(topic="t", post_fn=lambda *a: sent.append(a), cap=cap)
    for _ in range(20):
        notifier.notify({"kind": "degrade", "severity": "info", "message": "m"})
    assert len(sent) == 3, "at most the routine cap leaves the process"
    assert any(e[0] == "ntfy_cap_exceeded" for e in logged)


def test_notifier_429_records_a_channel_incident(tmp_path, monkeypatch):
    import src.alerts.ntfy_notifier as nn
    def _boom(*a):
        raise nn.NtfyRateLimited("429")
    cap = OutboundCap(tmp_path / "c.db", clock=_Clock())
    errs = []
    monkeypatch.setattr(nn.log, "error", lambda evt, **kw: errs.append(evt))
    notifier = nn.NtfyNotifier(topic="t", post_fn=_boom, cap=cap)
    notifier.notify({"kind": "degrade", "severity": "info", "message": "m"})
    assert "ntfy_channel_incident_429" in errs
    assert cap.stats()["last_429_at"] is not None
