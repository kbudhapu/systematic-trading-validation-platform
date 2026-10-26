"""N1 — THE soak market-data feed. Single source of truth.

Every live-path Alpaca market-data client — the REST bar fetch, the websocket bar/quote
stream, and the broker's latest-quote (NBBO) lookup — MUST resolve its feed from here. Never
the alpaca-py default (which is DataFeed.IEX), never a per-call-site literal.

Why this exists as ONE value routed everywhere, not a convention:
  - THE PRICE REFERENCE IS THE FILL REFERENCE. slippage = fill_price - reference_price. An IEX
    reference on a SIP-decided trade calibrates Stage-5 against a market we do not trade in (F4),
    and it would be baked into the very first fill.
  - The tape reconciler compares the websocket-fed active matrix against the REST-fed shadow
    matrix and DIVERGENT-LATCHES the leg (scale 0.0) on any mismatch. IEX is ~2% of volume, so a
    websocket-IEX / shadow-SIP split would disagree on nearly every bar and latch the leg off.

A test (tests/test_market_data_feed_single_tape.py) asserts every live-path stock client
resolves to this one feed and that DataFeed.IEX appears at no live-path construction site —
making the mixed-tape state unrepresentable, the same principle as H1a's forming bar and
assert_config_fully_consumed(). Crypto has no IEX/SIP distinction (one consolidated feed) and is
exempt. To change the soak feed, change it HERE, in one place.
"""
from __future__ import annotations

from alpaca.data.enums import DataFeed

# The one feed the soak trades and prices on. SIP: consolidated tape, a print in essentially
# every 15-min window for the soak universe. Bought and provisioned for the pilot.
SOAK_MARKET_DATA_FEED: DataFeed = DataFeed.SIP


def soak_market_data_feed() -> DataFeed:
    """The single market-data feed every live-path stock client must use (never the default)."""
    return SOAK_MARKET_DATA_FEED
