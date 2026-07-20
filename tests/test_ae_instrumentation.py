"""AE — instrumentation + alert-semantics tests (both directions).

The 10h window freeze was invisible because RollingWindow discarded bars silently, reconcile logged
the TARGET close (not the data), and the degradation alert re-stamped entered_at every cycle while
never sending a 'recovered' message. These pin the fixes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.rolling_window import RollingWindow
from src.models import Bar


def _bar(ts: datetime, close: float = 100.0) -> Bar:
    return Bar(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0, symbol="BTC/USD")


T0 = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


# --- AE2.2: append/upsert must LOG every rejection (never silent) ---

def test_stale_append_is_rejected_and_logged(monkeypatch):
    import src.core.rolling_window as rw
    events = []
    monkeypatch.setattr(rw.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    w = RollingWindow(maxlen=10, label="BTC/USD:1Hour")
    assert w.append(_bar(T0)) is True
    assert w.append(_bar(T0)) is False                     # duplicate ts -> rejected
    assert w.append(_bar(T0 - timedelta(hours=1))) is False  # out-of-order -> rejected
    rejects = [e for e in events if e[0] == "rolling_window_bar_rejected"]
    assert rejects, "a discarded bar must be LOGGED, never silent"
    assert rejects[0][1]["window"] == "BTC/USD:1Hour"
    assert rejects[0][1]["reason"] in {"duplicate", "stale_or_out_of_order"}


def test_fresh_append_does_not_log_a_rejection(monkeypatch):
    import src.core.rolling_window as rw
    events = []
    monkeypatch.setattr(rw.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    w = RollingWindow(maxlen=10, label="QQQ:15Min")
    for i in range(5):
        assert w.append(_bar(T0 + timedelta(minutes=15 * i))) is True
    assert not [e for e in events if e[0] == "rolling_window_bar_rejected"]


def test_upsert_out_of_order_before_latest_logs(monkeypatch):
    import src.core.rolling_window as rw
    events = []
    monkeypatch.setattr(rw.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    w = RollingWindow(maxlen=10, label="BTC/USD:1Hour")
    w.upsert_bar(_bar(T0))
    w.upsert_bar(_bar(T0 + timedelta(hours=1)))
    assert w.upsert_bar(_bar(T0)) == "ignored"            # before latest -> ignored + logged
    assert [e for e in events if e[0] == "rolling_window_bar_rejected"]


def test_rejection_log_is_rate_limited(monkeypatch):
    import src.core.rolling_window as rw
    events = []
    monkeypatch.setattr(rw.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    w = RollingWindow(maxlen=10, label="X")
    w.append(_bar(T0))
    for _ in range(50):
        w.append(_bar(T0))                                # 50 duplicates in a burst
    # rate-limited: far fewer than 50 log lines (the CHECK still runs every time; only the LOG throttles)
    assert 0 < len([e for e in events if e[0] == "rolling_window_bar_rejected"]) < 5


# --- AE3.3 / AE3.2: entered_at is not re-stamped; recovered fires on every downgrade ---

def _mgr(tmp_path, notifier=None):
    from src.engine.degradation_manager import DegradationManager
    return DegradationManager(db_path=tmp_path / "gov.db", notifier=notifier)


def test_hard_reassert_does_not_restamp_entered_at(tmp_path):
    m = _mgr(tmp_path)
    m.apply_hard_critical_degrade("bar_freshness_critical")
    first = m._activated_at
    for _ in range(5):
        m.apply_hard_critical_degrade("bar_freshness_critical")   # persistent breach re-asserts
    assert m._activated_at == first, "entered_at must be stamped ONCE on the transition, not re-stamped"


class _RecordingNotifier:
    def __init__(self):
        self.notified, self.resolved = [], []
    def notify(self, payload):
        self.notified.append(payload)
    def resolve(self, **kw):
        self.resolved.append(kw)


def test_soft_to_normal_dispatches_a_recovered_message(tmp_path):
    n = _RecordingNotifier()
    m = _mgr(tmp_path, notifier=n)
    m.apply_soft_degrade("soft_reason")
    m.reset_to_normal("slo_recovered")
    kinds = [p.get("kind", "") for p in n.notified]
    assert any("recovered" in k for k in kinds), "SOFT->NORMAL must send a recovered message"


def test_hard_to_soft_dispatches_recovered(tmp_path):
    n = _RecordingNotifier()
    m = _mgr(tmp_path, notifier=n)
    m.apply_hard_critical_degrade("hard_reason")
    m._downgrade_hard_to_soft("hard_critical_recovered_to_soft")
    assert any("recovered" in p.get("kind", "") for p in n.notified)
    assert n.resolved, "resolve() must re-arm the notifier on recovery"


# --- AE2.4: the ingester watchdog (detect a dead ingester; dormant is not dead) ---

class _StubIngestor:
    def __init__(self):
        self.fetch_calls = []
    def _fetch_bars_sync(self, symbol, timeframe, start, end, asset_class):
        self.fetch_calls.append(symbol)
        return []   # empty -> refresh returns early; we only assert the recovery was ATTEMPTED


def _coord_with_leg(symbol, timeframe, asset_class):
    from src.ingestor.dual_buffer_manager import DualBufferDataCoordinator
    ing = _StubIngestor()
    c = DualBufferDataCoordinator(ingestor=ing)
    c.register_leg("leg", symbol, timeframe, asset_class=asset_class,
                   window=RollingWindow(maxlen=50, label=f"{symbol}:{timeframe}"))
    return c, ing


def test_watchdog_fires_and_recovers_on_live_crypto_stall(monkeypatch):
    import time as _t
    import src.ingestor.dual_buffer_manager as dbm
    events = []
    monkeypatch.setattr(dbm.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    c, ing = _coord_with_leg("BTC/USD", "1Hour", "crypto")
    key = ("BTC/USD", "1Hour")
    c._last_promotion_mono[key] = _t.monotonic() - 100_000.0   # promoted long ago -> stalled
    c._check_ingestion_watchdog()
    assert key in c._ingester_stalled_keys
    assert any(e[0] == "ingester_stall_detected" for e in events)
    assert ing.fetch_calls, "watchdog must ATTEMPT recovery (force a shadow re-fetch)"


def test_watchdog_silent_on_dormant_equity(monkeypatch):
    import time as _t
    import src.ingestor.dual_buffer_manager as dbm
    events = []
    monkeypatch.setattr(dbm.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    c, ing = _coord_with_leg("QQQ", "15Min", "stock")
    monkeypatch.setattr(c._watchdog_calendar, "is_within_rth", lambda _ts: False)  # market CLOSED
    c._last_promotion_mono[("QQQ", "15Min")] = _t.monotonic() - 100_000.0
    c._check_ingestion_watchdog()
    assert not [e for e in events if e[0] == "ingester_stall_detected"], "dormant != dead"
    assert not ing.fetch_calls


def test_watchdog_silent_when_promotions_are_fresh(monkeypatch):
    import time as _t
    import src.ingestor.dual_buffer_manager as dbm
    events = []
    monkeypatch.setattr(dbm.log, "warning", lambda evt, **kw: events.append((evt, kw)))
    c, ing = _coord_with_leg("BTC/USD", "1Hour", "crypto")
    c._last_promotion_mono[("BTC/USD", "1Hour")] = _t.monotonic()   # just promoted
    c._check_ingestion_watchdog()
    assert not [e for e in events if e[0] == "ingester_stall_detected"]
