"""Cash-out fixed-arm forward paper — deterministic-logic tests (no network, no real orders)."""
from __future__ import annotations

from pathlib import Path

from src.research.cashout_forward import edgar, frozen
from src.research.cashout_forward.runner import ForwardRunner
from src.research.cashout_forward.store import Store

# realistic SC 13E-3 going-private reverse-split text that passes the FROZEN parser
EVENT_TEXT = (
    "The Company will effect a 1-for-1000 reverse stock split. Stockholders holding fewer than "
    "1,000 shares immediately prior to the split will be cashed out and will receive $12.50 in cash, "
    "without interest, per pre-split share. The reverse split will be effective on January 5, 2026."
)
NOT_MATERIAL_TEXT = (
    "In lieu of issuing fractional shares resulting from the 1-for-3 reverse stock split, holders "
    "will receive $4.00 in cash per pre-split share for fractional interests only."
)


def test_frozen_parser_parity_and_predicates():
    # the loaded parser IS the committed artifact (sha256 stamped)
    assert len(frozen.PARSER_SHA256) == 64
    # frozen text-side event definition holds on a real going-private cash-out
    assert frozen.is_material(EVENT_TEXT) is True
    assert frozen.is_fixed_price(EVENT_TEXT) is True
    assert frozen.is_fixed_arm_event_text(EVENT_TEXT) is True
    assert frozen.fixed_cash_out_price(EVENT_TEXT) == 12.5
    assert frozen.split_ratio(EVENT_TEXT) == 1000
    assert frozen.effective_date(EVENT_TEXT) == "January 5, 2026"
    # fractional-rounding-only is NOT a material sub-threshold cash-out
    assert frozen.is_material(NOT_MATERIAL_TEXT) is False
    assert frozen.is_fixed_arm_event_text(NOT_MATERIAL_TEXT) is False


def test_entry_rule_math():
    # edge = (fixed - proxy)/proxy - cost
    assert frozen.edge_per_event(12.5, 10.0, 0.02) == (2.5 / 10.0) - 0.02
    assert frozen.edge_per_event(12.5, 12.5, 0.02) < 0  # at fixed -> no gap net of cost


class FakeBroker:
    """No network, no orders. daily_bars returns a session AFTER the filing with a chosen (H+L+C)/3."""
    def __init__(self, proxy, enabled=False, exchange="NASDAQ", tradeable=True):
        self.proxy, self.enabled, self._ex, self._tr = proxy, enabled, exchange, tradeable
        self.orders = []

    def account_number(self):
        return "<redacted-account>"

    def tradeable(self, ticker):
        return (self._tr, self._ex)

    def daily_bars(self, ticker, n=25):
        # 22 flat sessions then the entry session (2026-01-02, after a 2026-01-01 filing) with (H+L+C)/3=proxy
        bars = [(f"2025-12-{d:02d}", 10.0, 10.2, 9.8, 10.0) for d in range(1, 23)]
        p = self.proxy
        bars.append(("2026-01-02", p, p, p, p))  # H=L=C=p -> (H+L+C)/3 = p
        return bars[-n:]

    def latest_price(self, ticker):
        return self.proxy

    def place_paper_buy(self, ticker, shares):
        if not self.enabled:
            return None
        oid = f"paper-{len(self.orders)}"
        self.orders.append((ticker, shares, oid))
        return oid

    def get_fill_price(self, oid):
        return None


def _patch_edgar(monkeypatch, text=EVENT_TEXT, ticker="ABCD"):
    monkeypatch.setattr(edgar, "poll", lambda s, e, max_hits=200: [
        {"accession": "0001-25-000001", "cik": "1234567", "ticker": ticker,
         "company": "TEST CO", "filed_date": "2026-01-01", "doc": "d.htm"}])
    monkeypatch.setattr(edgar, "fetch_filing_text", lambda a, c, d=None: text)
    monkeypatch.setattr(edgar, "cik_to_ticker", lambda c: ticker)


def test_full_flow_enter_then_book(tmp_path, monkeypatch):
    _patch_edgar(monkeypatch)
    store = Store(str(tmp_path / "cf.db"))
    broker = FakeBroker(proxy=10.0, enabled=True)  # 10.0 < 12.5 net cost -> ENTER
    r = ForwardRunner(store, broker)
    r.detect(log=lambda *a: None)
    # detected as event
    assert store._c.execute("SELECT is_event, fixed_price FROM cf_filings").fetchone() == (1, 12.5)
    r.enter_due(log=lambda *a: None)
    pos = store._c.execute("SELECT decision, ticker, shares, broker_order_id FROM cf_positions").fetchone()
    assert pos[0] == "ENTER" and pos[1] == "ABCD" and pos[2] >= 1 and pos[3] is not None
    assert len(broker.orders) == 1  # a paper order was placed (enabled=True)
    # effective date 2026-01-05 is in the past relative to _today() (2026-08+) -> booking due
    r.book_due(log=lambda *a: None)
    bk = store._c.execute("SELECT realized_edge_net_cost FROM cf_bookings").fetchone()
    assert bk is not None and bk[0] > 0.2  # (12.5-10)/10 - cost ~ +0.23
    clk = store.forward_clock()
    assert clk["n_events_detected"] == 1 and clk["n_booked"] == 1
    store.close()


def test_no_trade_when_gap_absent(tmp_path, monkeypatch):
    _patch_edgar(monkeypatch)
    store = Store(str(tmp_path / "cf.db"))
    broker = FakeBroker(proxy=12.5, enabled=True)  # at fixed -> no gap net cost -> NO_TRADE
    r = ForwardRunner(store, broker)
    r.detect(log=lambda *a: None)
    r.enter_due(log=lambda *a: None)
    pos = store._c.execute("SELECT decision, no_trade_reason FROM cf_positions").fetchone()
    assert pos[0] == "NO_TRADE"
    assert len(broker.orders) == 0            # no order on a no-trade
    assert store.forward_clock()["n_no_trade"] == 1
    store.close()


def test_observe_mode_places_no_orders(tmp_path, monkeypatch):
    _patch_edgar(monkeypatch)
    store = Store(str(tmp_path / "cf.db"))
    broker = FakeBroker(proxy=10.0, enabled=False)  # ENTER decision but OBSERVE -> no order
    r = ForwardRunner(store, broker)
    r.detect(log=lambda *a: None)
    r.enter_due(log=lambda *a: None)
    pos = store._c.execute("SELECT decision, broker_order_id FROM cf_positions").fetchone()
    assert pos[0] == "ENTER" and pos[1] is None   # decision logged, but NO order placed
    assert len(broker.orders) == 0
    store.close()


def test_not_tradeable_is_non_event(tmp_path, monkeypatch):
    _patch_edgar(monkeypatch)
    store = Store(str(tmp_path / "cf.db"))
    broker = FakeBroker(proxy=10.0, tradeable=False)  # not on Alpaca -> not an event forward
    r = ForwardRunner(store, broker)
    r.detect(log=lambda *a: None)
    row = store._c.execute("SELECT is_event, reason FROM cf_filings").fetchone()
    assert row[0] == 0 and row[1] == "not_tradeable_on_alpaca"
    store.close()


def test_committed_parser_file_present():
    p = (Path(__file__).resolve().parents[1] / "docs" / "edge-research" / "validated_legs"
         / "cashout_reverse_splits_fixed_arm" / "parser.py")
    assert p.exists(), "the validated frozen parser must be committed (PR #372) for parity"
