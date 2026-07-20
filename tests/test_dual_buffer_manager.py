"""Tests for dual-buffer shadow matrix tape-correction guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.core.rolling_window import RollingWindow
from src.ingestor.dual_buffer_manager import (
    DualBufferDataCoordinator,
    LOCKED_BLOCK_COUNT,
    SHADOW_POST_CLOSE_DELAY_SECONDS,
    _most_recent_bar_close,
    _shadow_refresh_due,
)
from src.models import Bar


def _bar(
    minute: int,
    *,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000.0,
) -> Bar:
    ts = datetime(2026, 1, 2, 15, minute, tzinfo=timezone.utc)
    return Bar(
        timestamp=ts,
        open=close - 0.1,
        high=high if high is not None else close + 0.2,
        low=low if low is not None else close - 0.2,
        close=close,
        volume=volume,
        symbol="QQQ",
    )


def _seed_window(count: int = 5) -> RollingWindow:
    window = RollingWindow(maxlen=32)
    for minute in range(count):
        window.append(_bar(minute))
    return window


def test_verify_matrix_alignment_passes_when_buffers_match() -> None:
    coordinator = DualBufferDataCoordinator()
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    bars = [_bar(minute) for minute in range(LOCKED_BLOCK_COUNT)]
    key = ("QQQ", "15Min")
    with coordinator._lock:
        state = coordinator._matrices[key]
        state.active.replace_bars(bars)
        state.shadow.replace_bars(bars)

    verdict = coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )
    assert verdict.aligned is True
    assert verdict.compared_bars == LOCKED_BLOCK_COUNT
    assert coordinator.is_divergent_latch("leg_a") is False


def test_verify_matrix_alignment_engages_latch_on_divergence() -> None:
    coordinator = DualBufferDataCoordinator()
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    active = [_bar(minute) for minute in range(LOCKED_BLOCK_COUNT)]
    shadow = list(active)
    shadow[-1] = _bar(LOCKED_BLOCK_COUNT - 1, close=101.5, high=101.8, low=101.2)
    key = ("QQQ", "15Min")
    with coordinator._lock:
        state = coordinator._matrices[key]
        state.active.replace_bars(active)
        state.shadow.replace_bars(shadow)

    verdict = coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )
    assert verdict.aligned is False
    assert verdict.latch_engaged is True
    assert coordinator.is_divergent_latch("leg_a") is True
    assert any(delta.field == "close" for delta in verdict.mismatches)


def test_verify_matrix_alignment_clears_latch_after_resync() -> None:
    coordinator = DualBufferDataCoordinator()
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    active = [_bar(minute) for minute in range(LOCKED_BLOCK_COUNT)]
    shadow = list(active)
    shadow[-1] = _bar(LOCKED_BLOCK_COUNT - 1, close=101.5)
    key = ("QQQ", "15Min")
    with coordinator._lock:
        state = coordinator._matrices[key]
        state.active.replace_bars(active)
        state.shadow.replace_bars(shadow)

    coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )
    assert coordinator.is_divergent_latch("leg_a") is True

    with coordinator._lock:
        coordinator._matrices[key].shadow.replace_bars(active)

    cleared = coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )
    assert cleared.aligned is True
    assert cleared.latch_cleared is True
    assert coordinator.is_divergent_latch("leg_a") is False


def test_sync_active_from_window_mirrors_execution_window() -> None:
    coordinator = DualBufferDataCoordinator()
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    window = _seed_window(LOCKED_BLOCK_COUNT)
    coordinator.sync_active_from_window("QQQ", "15Min", window)

    with coordinator._lock:
        active_count = coordinator._matrices[("QQQ", "15Min")].active.count
    assert active_count == LOCKED_BLOCK_COUNT


def test_shadow_refresh_uses_locked_blocks_from_rest() -> None:
    ingestor = MagicMock()
    bars = [_bar(minute) for minute in range(LOCKED_BLOCK_COUNT + 2)]
    ingestor._fetch_bars_sync.return_value = bars
    coordinator = DualBufferDataCoordinator(ingestor)
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")

    coordinator.refresh_shadow_now("QQQ", "15Min")

    ingestor._fetch_bars_sync.assert_called_once()
    with coordinator._lock:
        shadow = coordinator._matrices[("QQQ", "15Min")].shadow
        shadow_count = shadow.count
        _, _, _, closes, _ = shadow.tail(1)
        last_close = float(closes[0])
    assert shadow_count == LOCKED_BLOCK_COUNT
    assert last_close == pytest.approx(bars[-1].close)


def test_corporate_action_offset_applies_to_active_and_shadow() -> None:
    coordinator = DualBufferDataCoordinator()
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    bars = [_bar(minute, close=100.0 + minute) for minute in range(LOCKED_BLOCK_COUNT)]
    key = ("QQQ", "15Min")
    with coordinator._lock:
        state = coordinator._matrices[key]
        state.active.replace_bars(bars)
        state.shadow.replace_bars(bars)

    applied = coordinator.apply_corporate_action_offset("QQQ", "15Min", 0.5)
    assert applied is True

    with coordinator._lock:
        _, _, _, active_closes, _ = coordinator._matrices[key].active.tail(1)
        _, _, _, shadow_closes, _ = coordinator._matrices[key].shadow.tail(1)
    assert float(active_closes[0]) == pytest.approx(bars[-1].close + 0.5)
    assert float(shadow_closes[0]) == pytest.approx(bars[-1].close + 0.5)


def test_shadow_refresh_due_respects_post_close_delay() -> None:
    close_dt = datetime(2026, 1, 2, 16, 30, tzinfo=timezone.utc)
    before_refresh = close_dt + timedelta(seconds=SHADOW_POST_CLOSE_DELAY_SECONDS - 1)
    after_refresh = close_dt + timedelta(seconds=SHADOW_POST_CLOSE_DELAY_SECONDS)

    not_due, _ = _shadow_refresh_due(
        before_refresh,
        bar_minutes=15,
        last_refreshed_close=None,
    )
    assert not_due is False

    due, resolved = _shadow_refresh_due(
        after_refresh,
        bar_minutes=15,
        last_refreshed_close=None,
    )
    assert due is True
    assert resolved == _most_recent_bar_close(after_refresh, 15)


def test_rolling_window_upsert_appends_and_revises() -> None:
    window = RollingWindow(maxlen=8)
    first = _bar(0, close=100.0)
    second = _bar(1, close=101.0)

    assert window.upsert_bar(first) == "appended"
    assert window.upsert_bar(replace(first, close=100.5)) == "updated"
    assert window.latest() is not None
    assert window.latest().close == pytest.approx(100.5)

    assert window.upsert_bar(second) == "appended"
    assert len(window) == 2
    assert window.upsert_bar(_bar(0, close=99.0)) == "ignored"


def test_ingest_stream_bar_fans_out_to_registered_leg_windows() -> None:
    coordinator = DualBufferDataCoordinator()
    leg_window = RollingWindow(maxlen=16)
    coordinator.register_leg(
        "leg_a",
        "QQQ",
        "15Min",
        asset_class="stock",
        window=leg_window,
    )

    # H1a (parity fix): a live websocket bar is the FORMING bar for its period. It goes to
    # the forming SLOT only — never the closed signal window. `latest()` stays empty until a
    # CLOSED bar arrives via the shadow reconcile.
    outcome = coordinator.ingest_stream_bar(_bar(0), "15Min")
    assert outcome == "forming"
    assert len(leg_window) == 0
    assert leg_window.latest() is None
    assert leg_window.forming_bar().close == pytest.approx(100.0)

    revised = coordinator.ingest_stream_bar(_bar(0, close=100.25), "15Min")
    assert revised == "forming"
    assert len(leg_window) == 0
    assert leg_window.forming_bar().close == pytest.approx(100.25)


def test_reconcile_streaming_windows_overwrites_stream_anomalies() -> None:
    coordinator = DualBufferDataCoordinator()
    leg_window = RollingWindow(maxlen=16)
    coordinator.register_leg(
        "leg_a",
        "QQQ",
        "15Min",
        asset_class="stock",
        window=leg_window,
    )

    # Websocket bars are forming-only now; the CLOSED signal window is populated by the
    # REST-authoritative shadow reconcile.
    coordinator.ingest_stream_bar(_bar(0, close=100.0), "15Min")
    coordinator.ingest_stream_bar(_bar(1, close=101.0), "15Min")
    assert leg_window.latest() is None                       # nothing CLOSED yet
    assert leg_window.forming_bar().close == pytest.approx(101.0)

    # first reconcile seeds the closed bars
    coordinator.reconcile_streaming_windows(
        "QQQ", "15Min",
        authority_bars=[_bar(0, close=100.0), _bar(1, close=101.0)],
    )
    assert leg_window.latest().close == pytest.approx(101.0)

    # a later REST-authoritative correction OVERWRITES the anomalous closed bar in place
    applied = coordinator.reconcile_streaming_windows(
        "QQQ", "15Min",
        authority_bars=[_bar(0, close=100.0), _bar(1, close=101.5, high=101.8, low=101.2)],
    )
    assert applied >= 1
    assert leg_window.latest().close == pytest.approx(101.5)
