"""FIX-2 (RA-1): effective-runtime-config parity attestation.

Additive, non-fatal boot-time check: hashes the EFFECTIVE merged config, surfaces its
guardrail values, and alerts (never hard-fails) when it diverges from the audited file config
with the diverging source attributed.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.config.effective_parity import emit_effective_config_parity


def _strat(sid, *, enabled=True, params=None, symbol="QQQ", tf="15Min", ac="stock"):
    return SimpleNamespace(strategy_id=sid, enabled=enabled, symbol=symbol,
                           timeframe=tf, asset_class=ac, params=params or {})


def _cfg(strategies, *, env="paper", max_dd=0.05):
    return SimpleNamespace(environment=env, strategies=strategies,
                           risk=SimpleNamespace(max_drawdown_pct=max_dd))


def test_effective_equals_audited_hash_logged_no_alert():
    params = {"regime_filter": True, "borrow_drag_coefficient": 1.0}
    audited = _cfg([_strat("mean_reversion_qqq", params=dict(params))])
    effective = _cfg([_strat("mean_reversion_qqq", params=dict(params))])
    alerts = []
    res = emit_effective_config_parity(effective, audited, alert_sink=alerts.append)
    assert res["match"] is True
    assert res["effective_config_hash"] == res["audited_file_config_hash"]
    assert alerts == [], "no alert when effective == audited"


def test_param_override_detected_alerts_and_attributes_source():
    audited = _cfg([_strat("mrq", params={"regime_filter": True})])
    effective = _cfg([_strat("mrq", params={"regime_filter": False})])  # runtime override
    alerts = []
    res = emit_effective_config_parity(effective, audited, alert_sink=alerts.append)
    assert res["match"] is False
    assert res["enablement"]["mrq"] == "remote_param_override"
    assert len(alerts) == 1
    a = alerts[0]
    assert a["kind"] == "config_parity_mismatch" and a["severity"] == "warning"
    assert a["detail"]["diverging_sources"] == {"mrq": "remote_param_override"}
    # guardrail surfaced from the EFFECTIVE config (regime_filter now False)
    assert res["guardrails"]["per_leg"]["mrq"]["regime_filter"] is False


def test_remote_enabled_leg_is_attributed_and_alerts():
    audited = _cfg([_strat("spy", enabled=False)])
    effective = _cfg([_strat("spy", enabled=True)])
    alerts = []
    res = emit_effective_config_parity(effective, audited, alert_sink=alerts.append)
    assert res["match"] is False
    assert res["enablement"]["spy"] == "remote_enabled"
    assert alerts and alerts[0]["detail"]["diverging_sources"] == {"spy": "remote_enabled"}


def test_guardrails_surfaced_from_effective_config():
    eff = _cfg([_strat("mrq", params={"regime_filter": True, "borrow_drag_coefficient": 1.2})],
               max_dd=0.05)
    res = emit_effective_config_parity(eff, eff)  # self-parity -> match, no sink needed
    g = res["guardrails"]
    assert g["risk_max_drawdown_pct"] == 0.05
    assert g["per_leg"]["mrq"] == {"regime_filter": True, "borrow_drag_coefficient": 1.2}


def test_boot_never_hard_fails_on_missing_alert_sink():
    # divergence with NO alert_sink must still return cleanly (boot never blocked)
    res = emit_effective_config_parity(
        _cfg([_strat("mrq", params={"a": 1})]), _cfg([_strat("mrq", params={"a": 2})]))
    assert res["match"] is False
