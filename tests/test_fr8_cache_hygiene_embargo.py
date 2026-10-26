"""
FR-8 — `_4yr` cache hygiene + default train/test boundary embargo in the
chained_backtest leg-window sweep (task_0098d8d6).

a) Cache provenance gate: a consumed cache without a SIP provenance stamp is
   REFUSED (never silently consumed); `_sip_`-named caches pass.
b) Boundary purge+embargo defaults ON (canonical purge_embargo_train, E from
   the BT2S LOCKED rule: max lookback + max holding, DERIVED from the active
   grid constants); --no-embargo reproduces the historical contiguous window.

Affects FUTURE runs only — the BT2S diagnostic ruled the BT-2 boundary leakage
NOISE (docs/audits/bt2_embargo_sensitivity_2026-07-15.md), so no registered
verdict is re-adjudicated by any of this.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.chained_backtest as cb
from src.research.psd.purged_wf import purge_embargo_train


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _NoFetchIngestor:
    """Ingestor stub that fails the test if any network fetch is attempted."""

    def __init__(self, *a, **k):
        pass

    async def fetch_historical(self, *a, **k):  # pragma: no cover - must not run
        raise AssertionError("network fetch must not be attempted in this test")


def _tiny_bars_df() -> pl.DataFrame:
    t0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    return pl.DataFrame(
        {
            "timestamp": [t0, t0 + timedelta(minutes=15)],
            "open": [1.0, 1.01],
            "high": [1.02, 1.03],
            "low": [0.99, 1.00],
            "close": [1.01, 1.02],
            "volume": [100.0, 110.0],
            "symbol": ["SPY", "SPY"],
        }
    )


def _stub_config():
    return SimpleNamespace(alpaca_api_key="test-key", alpaca_secret_key="test-secret")


# ---------------------------------------------------------------------------
# a) cache provenance gate
# ---------------------------------------------------------------------------
def test_assert_sip_cache_refuses_unstamped_names():
    """`_4yr` (IEX) and unrecognized cache names raise; never silently consumed."""
    with pytest.raises(RuntimeError, match="stale/non-SIP cache refused"):
        cb._assert_sip_cache(Path("SPY_15Min_4yr.parquet"))
    with pytest.raises(RuntimeError, match="feed='unknown'"):
        cb._assert_sip_cache(Path("mystery_cache.parquet"))
    # SIP-named caches pass (both the new _sip_4yr and the decade _sip_10yr)
    cb._assert_sip_cache(Path("SPY_15Min_sip_4yr.parquet"))
    cb._assert_sip_cache(Path("QQQ_15Min_sip_10yr.parquet"))


def test_fetch_all_data_refuses_stale_legacy_4yr_cache(tmp_path, monkeypatch):
    """A leftover pre-SIP-flip `_4yr` cache makes the load REFUSE (raise), not
    silently reuse stale IEX/unadjusted bars (BT-1e)."""
    monkeypatch.setattr(cb, "PARQUET_DIR", tmp_path)
    monkeypatch.setattr(cb, "AlpacaDataIngestor", _NoFetchIngestor)
    _tiny_bars_df().write_parquet(tmp_path / "SPY_15Min_4yr.parquet")  # legacy IEX-era name

    with pytest.raises(RuntimeError, match="_4yr"):
        cb.fetch_all_data(_stub_config(), ["SPY"])


def test_fetch_all_data_accepts_sip_stamped_cache(tmp_path, monkeypatch):
    """A `_sip_`-named cache passes the gate and is served with NO fetch."""
    monkeypatch.setattr(cb, "PARQUET_DIR", tmp_path)
    monkeypatch.setattr(cb, "AlpacaDataIngestor", _NoFetchIngestor)
    _tiny_bars_df().write_parquet(tmp_path / "SPY_15Min_sip_4yr.parquet")

    result = cb.fetch_all_data(_stub_config(), ["SPY"])
    assert "SPY" in result and len(result["SPY"]) == 2


def test_future_cache_path_is_sip_named():
    """Fresh fetches (SIP fetcher) are born with filename provenance."""
    p = cb._cache_path("SPY", "15Min")
    assert p.name == "SPY_15Min_sip_4yr.parquet"
    assert cb.feed_fidelity_from_cache(p) == "sip"
    legacy = cb._legacy_cache_path("SPY", "15Min")
    assert legacy.name == "SPY_15Min_4yr.parquet"
    assert cb.feed_fidelity_from_cache(legacy) == "iex"


# ---------------------------------------------------------------------------
# b) boundary embargo — default ON, derived E, --no-embargo escape hatch
# ---------------------------------------------------------------------------
def test_embargo_e_derivation_matches_grid_constants():
    """E = max lookback + max holding, DERIVED from the grid constants (the
    BT2S LOCKED rule), never hardcoded in the production code."""
    e_select = cb.derive_sweep_embargo_bars("select")
    assert e_select == (
        max(max(cb.MR_SMA_LONG_GRID_SELECT), max(cb.MR_SMA_SHORT_GRID_SELECT))
        + int(np.max(cb.MR_MAX_BARS_SELECT))
    )
    e_exh = cb.derive_sweep_embargo_bars("exhaustive")
    assert e_exh == (
        max(max(cb._SMA_LONG_GRID), max(cb._SMA_SHORT_GRID)) + int(np.max(cb.MR_MAX_BARS))
    )
    # Pinned to the CURRENT grids (79+120 / 80+120 per the BT2S doc). If a grid
    # change moves these, this assertion forces a conscious re-derivation of E.
    assert e_select == 199
    assert e_exh == 200


def test_embargo_applied_by_default_at_synthetic_boundary(monkeypatch):
    """Default path: training indices end exactly E bars before test start,
    and the mask comes from the canonical purge_embargo_train."""
    monkeypatch.delenv("_CHAINED_NO_EMBARGO", raising=False)
    e = cb._active_embargo_bars("select")
    assert e == cb.derive_sweep_embargo_bars("select") > 0   # ON by default

    train_start, train_end = 0, 1000        # contiguous pre-test window
    test_start, test_end = 1000, 1030       # the test week abuts train_end
    new_end = cb._embargoed_train_end(train_start, train_end, test_start, test_end, e)
    assert new_end == test_start - e        # train ends E bars before test start

    # bit-identical to the canonical CPCV function (imported, not reimplemented)
    masked = purge_embargo_train(
        np.arange(train_start, train_end, dtype=np.int64), test_start, test_end, e
    )
    assert masked[0] == train_start and int(masked[-1]) + 1 == new_end

    # a window smaller than E is consumed entirely -> empty training window
    assert cb._embargoed_train_end(900, 1000, 1000, 1030, e) == 900


def test_no_embargo_reproduces_old_contiguous_window(monkeypatch):
    """--no-embargo (env _CHAINED_NO_EMBARGO=1) is the explicit escape hatch:
    E resolves to 0 and the training window is the historical contiguous
    [train_start, test_start) block, unchanged."""
    monkeypatch.setenv("_CHAINED_NO_EMBARGO", "1")
    assert cb._active_embargo_bars("select") == 0
    assert cb._active_embargo_bars("exhaustive") == 0
    # e=0 is a proven no-op: the boundary stays contiguous (train_end == test_start)
    assert cb._embargoed_train_end(0, 1000, 1000, 1030, 0) == 1000


def test_no_embargo_cli_flag_exists():
    """The escape hatch is a first-class, documented CLI flag."""
    src = Path(cb.__file__).read_text(encoding="utf-8")
    assert '"--no-embargo"' in src and "_CHAINED_NO_EMBARGO" in src
