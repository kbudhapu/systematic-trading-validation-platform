"""Z1b regression: crypto NBBO routing + `_fetch_live_spread_pct` semantics.

Two bugs converged here:
  1. U-queue added `infer_asset_class(symbol)` at orchestrator.py without importing it -> NameError
     that crashed the whole leg cycle (masked until W opened the degradation gate).
  2. `_fetch_live_spread_pct` (and the execution path) fetched the EQUITY NBBO for every symbol.
     For crypto that returns None -> recorded as an NBBO FAILURE -> nbbo_fetch_critical -> the crypto
     leg pinned in HARD_CRITICAL. But BTC DOES have a live quote (get_crypto_latest_quote); the fix
     is to route by asset class in the broker (get_nbbo_snapshot), NOT to mark crypto not-applicable
     and paper over a real, available spread source.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.engine.orchestrator import TradingOrchestrator


def _snap(**kw):
    return SimpleNamespace(**kw)


def _call(symbol, snapshot):
    recorded = []
    fake = SimpleNamespace(
        broker=SimpleNamespace(get_nbbo_snapshot=lambda _s: snapshot),
        slo_monitor=SimpleNamespace(
            note_nbbo_fetch=lambda ok, **kw: recorded.append((ok, kw)),
        ),
    )
    result = asyncio.run(TradingOrchestrator._fetch_live_spread_pct(fake, symbol))
    return result, recorded


def test_crypto_routes_to_a_real_quote_and_records_success():
    # BTC has a live crypto quote (routed inside the broker) -> success, real spread, no NameError.
    snap = _snap(mid_price=62500.0, ask_price=62536.68, bid_price=62464.9)
    result, recorded = _call("BTC/USD", snap)
    assert result is not None and result > 0.0
    assert recorded and recorded[0][0] is True
    assert recorded[0][1]["asset_class"] == "crypto"


def test_none_snapshot_is_a_failure_not_a_crash():
    # A genuine None (fetch failure) on an applicable instrument is recorded as a failure -- and
    # crucially does not NameError (the infer_asset_class import regression).
    result, recorded = _call("BTC/USD", None)
    assert result is None
    assert recorded and recorded[0][0] is False
    assert recorded[0][1]["asset_class"] == "crypto"


def test_equity_snapshot_returns_spread_and_infers_stock():
    snap = _snap(mid_price=100.0, ask_price=100.5, bid_price=99.5)
    result, recorded = _call("QQQ", snap)
    assert abs(result - 0.01) < 1e-9
    assert recorded and recorded[0][0] is True
    assert recorded[0][1]["asset_class"] == "stock"


def test_broker_get_nbbo_snapshot_routes_by_asset_class():
    """The routing is a broker property: crypto -> crypto quote, stock -> equity NBBO. Callers never
    branch on asset class. (Unbound call with a fake self -- no network / no client construction.)"""
    from src.broker.alpaca import AlpacaBroker

    calls = []
    fake = SimpleNamespace(
        _get_crypto_nbbo_snapshot_sync=lambda s: (calls.append(("crypto", s)), "CRYPTO")[1],
        _get_nbbo_snapshot_sync=lambda s: (calls.append(("equity", s)), "EQUITY")[1],
    )
    assert AlpacaBroker.get_nbbo_snapshot(fake, "BTC/USD") == "CRYPTO"
    assert AlpacaBroker.get_nbbo_snapshot(fake, "QQQ") == "EQUITY"
    assert calls == [("crypto", "BTC/USD"), ("equity", "QQQ")]
