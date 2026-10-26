"""
FP-1b — feed/cache provenance stamping on the chained_backtest sweep verdicts.

Every future leg-window verdict must be born stamped with the exact cache it
trained on and that cache's DERIVED feed fidelity, so the IEX-vs-SIP provenance
gap the 2026-07-15 backtest-parity audit found (no verdict-by-verdict ledger)
cannot recur silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.chained_backtest import (
    _compile_candidate_result,
    _provenance_stamp,
    feed_fidelity_from_cache,
)


def test_feed_fidelity_derived_from_cache_filename():
    """_4yr -> iex, _sip -> sip, anything else -> unknown (never guessed)."""
    assert feed_fidelity_from_cache("SPY_15Min_4yr.parquet") == "iex"
    assert feed_fidelity_from_cache("QQQ_15Min_sip_10yr.parquet") == "sip"
    assert feed_fidelity_from_cache("GLD_4Hour_sip_10yr.parquet") == "sip"
    # a path (not just a bare name) resolves identically
    assert feed_fidelity_from_cache(Path("data/parquet/BTC_USD_1Hour_4yr.parquet")) == "iex"
    # no recognizable token -> UNKNOWN, not a fabricated feed
    assert feed_fidelity_from_cache("SPY_15Min.parquet") == "unknown"
    assert feed_fidelity_from_cache("mystery_cache.parquet") == "unknown"


def test_provenance_stamp_shape_and_derivation():
    """The stamp carries the real cache filename + its derived feed for a real leg."""
    stamp = _provenance_stamp("SPY")
    assert set(stamp) == {"source_cache", "feed"}
    # FR-8: the sweep's cache path is now SIP-named (`_sip_4yr`); legacy `_4yr`
    # (IEX) caches are refused at load (_assert_sip_cache), so a future verdict
    # can only ever be born stamped feed="sip".
    assert stamp["source_cache"].endswith("_sip_4yr.parquet")
    assert stamp["feed"] == "sip"
    assert feed_fidelity_from_cache(stamp["source_cache"]) == stamp["feed"]


def test_compiled_candidate_result_is_stamped():
    """A compiled candidate verdict dict carries source_cache + feed."""
    week_results = [
        {"week": 0, "signal": True, "week_ret": 0.01, "regime": "MED_VOL"},
        {"week": 1, "signal": False, "week_ret": 0.0, "regime": "LOW_VOL"},
    ]
    result = _compile_candidate_result("QQQ", 100, week_results)
    assert "source_cache" in result and "feed" in result
    assert result["feed"] == "sip"  # FR-8: QQQ sweep cache is the SIP-named `_sip_4yr` file
    assert result["source_cache"].endswith("_sip_4yr.parquet")
    # the stamp is self-consistent with its own derivation
    assert feed_fidelity_from_cache(result["source_cache"]) == result["feed"]
