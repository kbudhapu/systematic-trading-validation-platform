"""FR-1v2 tonight bundle — microstructure dormancy gate (task_13903b0c) + label fix (task_52e9e1d0).

FR-1: a CLOSED/dormant market contributes NO safety sample — the guard's evaluate() read the
closed QQQ tape as `regulatory_halt` STATE_UNSAFE every 15-min cycle all night (8x/evening).
FR-2: portfolio_mode_reason vs exposure_block_reason are separate fields; the macro_risk_off_cap
breach code appends only when the cap CAUSES a breach (never decorates an 'allowed' row).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from structlog.testing import capture_logs

from src.engine.microstructure_guard import (
    MicrostructureGuard,
    MicrostructureGuardConfig,
)
from src.engine.portfolio_coordinator import CycleCoordinationResult


# ── FR-1 (a): closed session — the dormancy predicate suppresses the guard sample ──
def test_a_closed_session_produces_zero_guard_events():
    """Reproduce the 8x-overnight pattern: a halted (closed-market) tape evaluated repeatedly.
    With the dormancy gate the orchestrator never calls evaluate() for a dormant equity leg —
    modeled here by asserting the gate short-circuit contract: dormant => evaluate NOT invoked."""
    from src.engine import orchestrator as orch_mod

    calls = []

    class _GuardSpy:
        def evaluate(self, symbol):
            calls.append(symbol)
            raise AssertionError("guard must not be evaluated for a dormant leg")

    class _SloDormant:
        def is_market_dormant(self, asset_class, reference):
            return asset_class == "stock"  # equities closed; crypto never dormant

    class _Cfg:
        symbol = "QQQ"
        timeframe = "15Min"
        asset_class = "stock"

    class _Host:
        microstructure_guard = _GuardSpy()
        slo_monitor = _SloDormant()
        _leg_market_dormant = orch_mod.TradingOrchestrator._leg_market_dormant
        _microstructure_blocks_entry = (
            orch_mod.TradingOrchestrator._microstructure_blocks_entry
        )

    host = _Host()
    # the 8x pattern: repeated cycles, zero guard evaluations, zero STATE_UNSAFE
    with capture_logs() as logs:
        for _ in range(8):
            dormant = host._leg_market_dormant(_Cfg())
            verdict = None if dormant else host._microstructure_blocks_entry(_Cfg.symbol)
            assert dormant is True and verdict is None
    assert calls == []
    assert not [e for e in logs if "STATE_UNSAFE" in str(e.get("event", ""))]


# ── FR-1 (b): POSITIVE CONTROL — a genuine intra-RTH halt still fires at unchanged thresholds ──
def test_b_positive_control_genuine_halt_still_fires():
    guard = MicrostructureGuard(MicrostructureGuardConfig())
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)  # Monday, mid-RTH
    guard.note_quote("QQQ", bid_price=560.00, ask_price=560.05, timestamp=now)
    guard.note_trading_status(
        "QQQ",
        status_code="HALT",
        status_message="Trading halt",
        reason_code="LUDP",
        reason_message="Volatility trading pause",
        timestamp=now,
    )
    with capture_logs() as logs:
        verdict = guard.evaluate("QQQ", now=now)
    assert verdict.blocks_entries is True
    assert verdict.halt_status is True
    assert verdict.reason == "regulatory_halt"
    unsafe = [e for e in logs if "STATE_UNSAFE" in str(e.get("event", ""))]
    assert unsafe, "a genuine halt must still produce the guard event"
    # thresholds untouched
    cfg = MicrostructureGuardConfig()
    assert cfg.max_allowed_spread_pct == pytest.approx(0.015)
    assert cfg.max_quote_stale_seconds == pytest.approx(5.0)


# ── FR-1 (c): crypto legs are structurally unaffected (24h market never dormant) ──
def test_c_crypto_never_dormant():
    from src.engine.slo_monitor import SLOMonitor

    monitor = SLOMonitor()
    # deep weekend timestamp — a closed time for equities, always-open for crypto
    weekend = datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc)
    assert monitor.is_market_dormant("crypto", weekend) is False
    assert monitor.is_market_dormant("stock", weekend) is True


# ── FR-2: label separation + cosmetic-code suppression (log-shape, no behavior change) ──
def test_fr2_result_carries_separate_exposure_block_reason():
    r = CycleCoordinationResult(
        cycle_id="c1",
        risk_budgets={},
        plans={},
        mode_reason="spy_config_disabled",
        exposure_allowed=False,
        exposure_block_reason="macro_risk_off_cap",
    )
    assert r.mode_reason != r.exposure_block_reason  # the two states are separate fields
    # default is None (allowed cycles carry no block reason)
    r2 = CycleCoordinationResult(
        cycle_id="c2", risk_budgets={}, plans={}, mode_reason="x", exposure_allowed=True
    )
    assert r2.exposure_block_reason is None


def test_fr2_macro_risk_off_cap_only_on_actual_breach():
    """The append site (portfolio_coordinator.py ~:687) now keys on `breach`, not `cap < 1.0`:
    an ELEVATED cap (0.75, allowed) must NOT decorate the codes; a blocking cap (<0.65) must."""
    import inspect

    from src.engine import portfolio_coordinator as pc

    src = inspect.getsource(pc.PortfolioCoordinator.coordinate_cycle)
    assert '("macro_risk_off_cap",) if breach else' in src, (
        "the cosmetic append regressed to cap<1.0 keying"
    )
    assert 'if gross_exposure_cap_multiplier < 1.0 else ()' not in src


def test_fr2_coordination_log_uses_new_field_names():
    """Log-shape pin: the coordination-complete line emits portfolio_mode_reason +
    exposure_block_reason, and the old ambiguous mode_reason= kwarg is gone."""
    import inspect

    from src.engine import orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    start = src.index('"portfolio_coordination_complete"')
    window = src[start : start + 600]
    assert "portfolio_mode_reason=" in window
    assert "exposure_block_reason=" in window
    assert "\n            mode_reason=" not in window
