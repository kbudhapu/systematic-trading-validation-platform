"""N1c — ONE tape. The soak must trade, price, stream and reconcile on a SINGLE, EXPLICIT
market-data feed. A mixed IEX/SIP state is made UNREPRESENTABLE by source inspection:

  - every live-path stock market-data construction passes feed=soak_market_data_feed()
  - no live-path module hardcodes DataFeed.IEX
  - no live-path stock client is constructed on the alpaca-py default (no feed= at all)

Same principle as H1a's forming bar and assert_config_fully_consumed(): the defect cannot be
introduced without failing a test. Crypto has no IEX/SIP distinction (one consolidated feed)
and is exempt.
"""
from __future__ import annotations

import inspect
import re

from alpaca.data.enums import DataFeed

from src.ingestor.market_data_feed import SOAK_MARKET_DATA_FEED, soak_market_data_feed

# Every module that constructs a STOCK market-data client on the live/soak path.
import src.ingestor.alpaca as ingestor_alpaca
import src.ingestor.market_data_stream as market_data_stream
import src.broker.alpaca as broker_alpaca
import src.engine.regime_intelligence as regime_intelligence

LIVE_PATH_MODULES = [
    ingestor_alpaca,
    market_data_stream,
    broker_alpaca,
    regime_intelligence,
]

# Stock market-data client / request constructors whose feed selection matters.
STOCK_FEED_CONSTRUCTORS = ("StockDataStream(", "StockBarsRequest(", "StockLatestQuoteRequest(")


def test_soak_feed_is_sip_and_single_source():
    assert soak_market_data_feed() is SOAK_MARKET_DATA_FEED
    assert SOAK_MARKET_DATA_FEED == DataFeed.SIP


def test_no_live_path_module_hardcodes_iex():
    offenders = []
    for mod in LIVE_PATH_MODULES:
        src = inspect.getsource(mod)
        for i, line in enumerate(src.splitlines(), 1):
            if "DataFeed.IEX" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{mod.__name__}:{i}: {line.strip()}")
    assert not offenders, "live-path module hardcodes IEX (mixed-tape risk):\n" + "\n".join(offenders)


def test_every_live_path_stock_constructor_sets_the_shared_feed():
    """Each StockDataStream / StockBarsRequest / StockLatestQuoteRequest construction on the
    live path must carry feed=soak_market_data_feed() (never a literal, never defaulted)."""
    offenders = []
    for mod in LIVE_PATH_MODULES:
        src = inspect.getsource(mod)
        for ctor in STOCK_FEED_CONSTRUCTORS:
            for m in re.finditer(re.escape(ctor), src):
                # scan the call + its (possibly multi-line, commented) argument window for the
                # shared resolver. Generous window so a verbose in-call comment can't hide it.
                near = src[m.start(): m.start() + 1200]
                if "soak_market_data_feed()" not in near:
                    line_no = src[: m.start()].count("\n") + 1
                    offenders.append(f"{mod.__name__}:{line_no}: {ctor} without soak_market_data_feed()")
    assert not offenders, (
        "live-path stock market-data client not on the single shared feed:\n" + "\n".join(offenders)
    )


def test_shared_feed_is_actually_accepted_by_the_alpaca_api():
    """Guard against a silent API drift: the resolver's value must be a real DataFeed the
    request objects accept (a wrong type would TypeError only at live runtime)."""
    from alpaca.data.requests import StockLatestQuoteRequest

    req = StockLatestQuoteRequest(symbol_or_symbols="QQQ", feed=soak_market_data_feed())
    assert req.feed == DataFeed.SIP
