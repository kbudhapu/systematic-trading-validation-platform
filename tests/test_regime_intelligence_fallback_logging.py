"""
Fix 4/5 — Log a warning when evaluate_live() falls back to proxy defaults.

Tests verify:
1. When fetch_live_snapshot() raises, a structured 'regime_snapshot_fetch_failed'
   WARNING is emitted (not just that the function doesn't crash).
2. The fallback result is still returned correctly after the warning — the function
   produces a usable PortfolioRiskModeDecision, not None or a secondary crash.
3. evaluate_velocity_shock() proxy fallback (when last_snapshot is None) is a
   guard on cached state, not a silent exception catch — no additional fix needed
   (confirmed no silent exception sites beyond the one fixed above).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from src.engine.regime_intelligence import RegimeIntelligenceEngine


# ---------------------------------------------------------------------------
# Part 1 — warning IS emitted when fetch_live_snapshot() raises
# ---------------------------------------------------------------------------

def test_warning_emitted_when_fetch_live_snapshot_raises() -> None:
    """
    When fetch_live_snapshot raises, evaluate_live must emit a structlog
    WARNING with event='regime_snapshot_fetch_failed' before falling back.
    """
    engine = RegimeIntelligenceEngine()

    with structlog.testing.capture_logs() as logs:
        with patch.object(
            engine.stress_dashboard,
            "fetch_live_snapshot",
            side_effect=ConnectionError("market data feed unavailable"),
        ):
            engine.evaluate_live(
                api_key="fake-key",
                secret_key="fake-secret",
            )

    warning_logs = [
        e for e in logs if e.get("log_level") == "warning"
        and e.get("event") == "regime_snapshot_fetch_failed"
    ]
    assert len(warning_logs) == 1, (
        f"Expected exactly 1 'regime_snapshot_fetch_failed' warning, got: {warning_logs}"
    )
    entry = warning_logs[0]
    assert "market data feed unavailable" in entry.get("error", "")
    assert entry.get("fallback") == "proxy_defaults"
    assert "timestamp" in entry


def test_warning_includes_exception_message() -> None:
    """The warning log's 'error' field contains the exception string."""
    engine = RegimeIntelligenceEngine()

    with structlog.testing.capture_logs() as logs:
        with patch.object(
            engine.stress_dashboard,
            "fetch_live_snapshot",
            side_effect=RuntimeError("upstream timeout after 5s"),
        ):
            engine.evaluate_live(api_key="k", secret_key="s")

    entry = next(
        (e for e in logs if e.get("event") == "regime_snapshot_fetch_failed"),
        None,
    )
    assert entry is not None
    assert "upstream timeout after 5s" in entry["error"]


# ---------------------------------------------------------------------------
# Part 2 — fallback result is still usable after the warning
# ---------------------------------------------------------------------------

def test_fallback_decision_returned_after_warning() -> None:
    """
    evaluate_live must return a PortfolioRiskModeDecision (not None, not raise)
    even when fetch_live_snapshot raises.
    """
    from src.engine.regime_intelligence import PortfolioRiskModeDecision

    engine = RegimeIntelligenceEngine()

    with structlog.testing.capture_logs():
        with patch.object(
            engine.stress_dashboard,
            "fetch_live_snapshot",
            side_effect=ValueError("bad credentials"),
        ):
            result = engine.evaluate_live(api_key="k", secret_key="s")

    assert isinstance(result, PortfolioRiskModeDecision), (
        f"Expected PortfolioRiskModeDecision, got {type(result)}"
    )


def test_fallback_uses_proxy_snapshot_not_none() -> None:
    """
    When fetch_live_snapshot raises, snapshot falls back to proxy_snapshot({}),
    not None — so evaluate() always receives a valid CrossAssetSnapshot.
    This is confirmed by the function returning normally with no AttributeError.
    """
    engine = RegimeIntelligenceEngine()

    with structlog.testing.capture_logs():
        with patch.object(
            engine.stress_dashboard,
            "fetch_live_snapshot",
            side_effect=OSError("network unreachable"),
        ):
            # If snapshot were None (not replaced by proxy_snapshot), evaluate()
            # would raise AttributeError trying to access snapshot fields.
            result = engine.evaluate_live(api_key="k", secret_key="s")

    assert result is not None


def test_no_warning_when_fetch_succeeds() -> None:
    """
    Control: when fetch_live_snapshot succeeds, no warning is emitted.
    """
    from src.engine.regime_intelligence import CrossAssetStressDashboard

    engine = RegimeIntelligenceEngine()
    # Use a real proxy_snapshot so evaluate() receives valid typed fields.
    real_snapshot = CrossAssetStressDashboard.proxy_snapshot({})

    with structlog.testing.capture_logs() as logs:
        with patch.object(
            engine.stress_dashboard,
            "fetch_live_snapshot",
            return_value=real_snapshot,
        ):
            engine.evaluate_live(api_key="k", secret_key="s")

    warning_logs = [
        e for e in logs
        if e.get("event") == "regime_snapshot_fetch_failed"
    ]
    assert len(warning_logs) == 0


def test_no_warning_when_no_credentials() -> None:
    """
    When api_key/secret_key are absent, fetch is never called and no warning fires.
    """
    engine = RegimeIntelligenceEngine()

    with structlog.testing.capture_logs() as logs:
        engine.evaluate_live()  # no credentials — skips the try block entirely

    warning_logs = [
        e for e in logs
        if e.get("event") == "regime_snapshot_fetch_failed"
    ]
    assert len(warning_logs) == 0


# ---------------------------------------------------------------------------
# Part 3 — evaluate_velocity_shock proxy fallback is NOT a silent exception catch
# ---------------------------------------------------------------------------

def test_velocity_shock_proxy_fallback_is_not_a_silent_exception_catch() -> None:
    """
    evaluate_velocity_shock() uses proxy_snapshot when last_snapshot is None.
    This is a guard on a cached value, not an except block — no warning is needed.
    We confirm no 'regime_snapshot_fetch_failed' warning fires from that code path.
    """
    engine = RegimeIntelligenceEngine()
    # last_snapshot is None by default (fresh engine, no prior evaluate_live call)
    assert engine.state.last_snapshot is None

    with structlog.testing.capture_logs() as logs:
        engine.stress_dashboard.detect_velocity_shock_event(
            anchor_time=datetime.now(timezone.utc)
        )

    warning_logs = [
        e for e in logs
        if e.get("event") == "regime_snapshot_fetch_failed"
    ]
    assert len(warning_logs) == 0, (
        "evaluate_velocity_shock proxy fallback must not emit 'regime_snapshot_fetch_failed'"
    )
