"""
AI policy lifecycle state machine — probation sieve, sovereign gates, and staged rollback.
"""

from __future__ import annotations

import json
import sqlite3
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import polars as pl

from src.persistence.db import RESEARCH_VAULT_PATH
from src.engine.regime_intelligence import RegimeStabilizationVerdict
from src.router.risk_manager import (
    AI_POLICY_MIN_LEDGER_TUPLES,
    AI_POLICY_PASSIVE_SHADOW,
    AI_POLICY_PROBATIONAL,
    AI_POLICY_SOVEREIGN,
    AI_POLICY_VALID_STATES,
    resolve_trading_session,
)

STATE_PASSIVE = AI_POLICY_PASSIVE_SHADOW
STATE_PROBATIONAL = AI_POLICY_PROBATIONAL
STATE_SOVEREIGN = AI_POLICY_SOVEREIGN

LOOKBACK_30D = 30
LOOKBACK_60D = 60
MIN_STRATUM_SAMPLES = 3
MIN_REGIME_BUCKETS_PASS = 2
MIN_SESSION_BUCKETS_PASS = 2
PROBATION_EDGE_DECAY_RATIO = 0.85
PROBATION_EDGE_DECAY_STREAK = 3
PROBATION_ATTRIBUTION_LOOKBACK_DAYS = 14
DEFAULT_ATTRIBUTION_MAX_DRAWDOWN_PCT = 0.08
PROBATION_MIN_LIVE_SHARPE = 0.0
PROBATION_MIN_ATTRIBUTION_TRADES = 5

SOVEREIGN_MIN_VERIFIED_TRADING_DAYS = 10
SOVEREIGN_MIN_FILLED_TRADES = 25
SOVEREIGN_MAX_AVG_SLIPPAGE_PCT = 0.0015
SOVEREIGN_MIN_FILL_QUALITY_SCORE = 0.70
SOVEREIGN_MIN_EDGE_VS_RULES = 0.02

CATASTROPHIC_HIT_RATE_FLOOR = 0.30
CATASTROPHIC_EXECUTION_DRIFT = "execution_drift"
MACRO_VELOCITY_SHOCK_REASON = "macro_velocity_shock"
VELOCITY_SHOCK_RECOVERY_REASON = "velocity_shock_cool_off_recovery"
SHOCK_RECOVERY_MIN_DUAL_SHADOW_SAMPLES = 5
SHOCK_RECOVERY_SHADOW_LOOKBACK_DAYS = 14


class RollbackTier(str, Enum):
    NONE = "NONE"
    SOVEREIGN_TO_PROBATIONAL = "SOVEREIGN_TO_PROBATIONAL"
    PROBATIONAL_TO_PASSIVE = "PROBATIONAL_TO_PASSIVE"
    CATASTROPHIC_EVICTION = "CATASTROPHIC_EVICTION"
    VELOCITY_SHOCK_DEMOTION = "VELOCITY_SHOCK_DEMOTION"


@dataclass(frozen=True)
class StratifiedWindowMetrics:
    lookback_days: int
    challenger_return: float
    rules_return: float
    challenger_sharpe: float
    rules_sharpe: float
    sample_count: int
    regime_buckets_passed: int
    session_buckets_passed: int
    stratum_details: dict[str, dict[str, float]]


@dataclass(frozen=True)
class ProbationEntryVerdict:
    approved: bool
    reasons: tuple[str, ...]
    window_30d: StratifiedWindowMetrics | None
    window_60d: StratifiedWindowMetrics | None


@dataclass(frozen=True)
class ProbationEdgeVerdict:
    maintains_edge: bool
    directive: str
    challenger_edge: float
    rules_edge: float
    edge_ratio: float
    decay_streak: int
    reason: str


@dataclass(frozen=True)
class SovereignPromotionVerdict:
    approved: bool
    reasons: tuple[str, ...]
    verified_trading_days: int
    filled_trade_count: int
    avg_slippage_pct: float
    fill_quality_score: float


@dataclass(frozen=True)
class AttributionSloVerdict:
    breached: bool
    reasons: tuple[str, ...]
    live_sharpe: float
    max_drawdown_pct: float
    trade_count: int
    drawdown_limit_pct: float


@dataclass(frozen=True)
class StagedRollbackResult:
    prior_state: str
    new_state: str
    tier: RollbackTier
    catastrophic: bool
    reason: str
    eviction_lockout_until: str | None = None
    restored_params: dict[str, Any] | None = None
    restored_config_version_id: int | None = None


@dataclass(frozen=True)
class VelocityShockRecoveryVerdict:
    approved: bool
    reasons: tuple[str, ...]
    challenger_edge: float
    rules_edge: float
    edge_ratio: float
    sample_count: int


@dataclass(frozen=True)
class VelocityShockDemotionResult:
    demoted: bool
    transitions: tuple[LifecycleTransition, ...]
    reason: str


@dataclass
class LifecycleTransition:
    strategy_id: str
    symbol: str
    from_state: str
    to_state: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _load_dual_policy_shadow_frame(
    db_path: Path,
    *,
    symbol: str,
    lookback_days: int = SHOCK_RECOVERY_SHADOW_LOOKBACK_DAYS,
    since_timestamp: str | None = None,
) -> pl.DataFrame:
    from src.engine.challenger_registry import ensure_challenger_schema

    ensure_challenger_schema(db_path)
    cutoff = (
        since_timestamp
        if since_timestamp
        else (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    )
    query = """
        SELECT timestamp, symbol, challenger_pnl, rules_baseline_pnl, execution_path_json
        FROM dual_policy_shadow_log
        WHERE timestamp >= ? AND symbol = ?
    """
    with sqlite3.connect(db_path) as conn:
        try:
            return pl.read_database(
                query,
                conn,
                execute_options={"parameters": [cutoff, symbol.upper()]},
            )
        except Exception:
            return pl.DataFrame()


def _sharpe(returns: np.ndarray) -> float:
    if returns.size < 2:
        return 0.0
    std = float(np.std(returns))
    if std < 1e-12:
        return 0.0
    return float(np.mean(returns) / std)


def _max_drawdown_pct(pnls: np.ndarray, *, initial_equity: float = 100_000.0) -> float:
    if pnls.size == 0:
        return 0.0
    equity = float(initial_equity)
    peak = equity
    max_dd = 0.0
    for pnl in pnls:
        equity += float(pnl)
        peak = max(peak, equity)
        if peak > 0.0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return float(max_dd)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_attribution_frame(
    db_path: Path,
    *,
    lookback_days: int,
    strategy_id: str | None = None,
) -> pl.DataFrame:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    query = """
        SELECT timestamp, strategy_id, symbol, pnl, slippage_pct, regime_id, session_type, execution_tactic
        FROM live_attribution_ledger
        WHERE timestamp >= ?
    """
    params: list[Any] = [cutoff]
    with sqlite3.connect(db_path) as conn:
        try:
            df = pl.read_database(query, conn, execute_options={"parameters": params})
        except Exception:
            return pl.DataFrame()
    if strategy_id and not df.is_empty() and "strategy_id" in df.columns:
        df = df.filter(pl.col("strategy_id") == strategy_id)
    return df


def _load_shadow_frame(db_path: Path, *, lookback_days: int, symbol: str | None = None) -> pl.DataFrame:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    query = """
        SELECT timestamp, symbol, shadow_action_taken, realized_reward_24h, rules_engine_action
        FROM shadow_rl_ledger
        WHERE timestamp >= ? AND realized_reward_24h IS NOT NULL
    """
    with sqlite3.connect(db_path) as conn:
        try:
            df = pl.read_database(query, conn, execute_options={"parameters": [cutoff]})
        except Exception:
            return pl.DataFrame()
    if symbol and not df.is_empty():
        df = df.filter(pl.col("symbol") == symbol.upper())
    return df


def _shadow_allocation(action: str) -> float:
    mapping = {
        "STAND_DOWN": 0.0,
        "ALLOCATION_HALF": 0.5,
        "ALLOCATION_MAX": 1.0,
    }
    return mapping.get(action, 0.75)


def _rules_allocation(action: str) -> float:
    mapping = {
        "NO_TRADE_FALLBACK": 0.0,
        "DEFENSIVE_FALLBACK": 0.25,
        "BASELINE_FALLBACK": 0.5,
        "UNSAFE_DISABLED": 0.0,
        "BEAR_DEFENSIVE": 0.25,
        "HIGH_VOL_MR": 1.0,
        "CALM_MR": 1.0,
        "UNKNOWN": 0.75,
    }
    return mapping.get(action, 0.75)


def _compute_stratified_window(
    *,
    lookback_days: int,
    attribution_df: pl.DataFrame,
    shadow_df: pl.DataFrame,
) -> StratifiedWindowMetrics:
    stratum_details: dict[str, dict[str, float]] = {}
    regime_pass = 0
    session_pass = 0

    if not attribution_df.is_empty():
        regime_results: dict[str, bool] = {}
        session_results: dict[str, bool] = {}
        grouped = attribution_df.group_by(["regime_id", "session_type"]).agg(
            pl.col("pnl").mean().alias("mean_pnl"),
            pl.col("pnl").count().alias("n"),
        )
        for row in grouped.iter_rows(named=True):
            regime = str(row["regime_id"])
            session = str(row["session_type"])
            key = f"{regime}|{session}"
            n = int(row["n"])
            mean_pnl = float(row["mean_pnl"])
            passed = n >= MIN_STRATUM_SAMPLES and mean_pnl > 0.0
            stratum_details[key] = {"n": n, "mean_pnl": mean_pnl, "passed": int(passed)}
            regime_results[regime] = regime_results.get(regime, False) or passed
            session_results[session] = session_results.get(session, False) or passed
        regime_pass = sum(1 for ok in regime_results.values() if ok)
        session_pass = sum(1 for ok in session_results.values() if ok)

    challenger_returns: list[float] = []
    rules_returns: list[float] = []

    if not attribution_df.is_empty() and "pnl" in attribution_df.columns:
        challenger_returns = [float(x) for x in attribution_df["pnl"].to_list()]

    for row in shadow_df.iter_rows(named=True):
        reward = row.get("realized_reward_24h")
        if reward is None:
            continue
        if not challenger_returns:
            challenger_returns.append(
                _shadow_allocation(str(row.get("shadow_action_taken") or "")) * float(reward)
            )
        rules_returns.append(
            _rules_allocation(str(row.get("rules_engine_action") or "UNKNOWN")) * float(reward)
        )

    if attribution_df.is_empty() and challenger_returns:
        ts_series = shadow_df["timestamp"].to_list()
        for ts_raw, ch_ret, ru_ret in zip(ts_series, challenger_returns, rules_returns):
            ts = _parse_ts(str(ts_raw))
            session = resolve_trading_session(ts)
            regime = "SHADOW"
            key = f"{regime}|{session}"
            bucket = stratum_details.setdefault(key, {"n": 0, "challenger": 0.0, "rules": 0.0})
            bucket["n"] = int(bucket["n"]) + 1
            bucket["challenger"] = float(bucket.get("challenger", 0.0)) + ch_ret
            bucket["rules"] = float(bucket.get("rules", 0.0)) + ru_ret
        regime_results: dict[str, bool] = {}
        session_results: dict[str, bool] = {}
        for key, bucket in stratum_details.items():
            n = int(bucket["n"])
            if n < MIN_STRATUM_SAMPLES:
                continue
            ch_avg = float(bucket.get("challenger", 0.0)) / n
            ru_avg = float(bucket.get("rules", 0.0)) / n
            bucket["mean_pnl"] = ch_avg - ru_avg
            passed = ch_avg > ru_avg
            bucket["passed"] = int(passed)
            regime, session = key.split("|", 1)
            regime_results[regime] = regime_results.get(regime, False) or passed
            session_results[session] = session_results.get(session, False) or passed
        regime_pass = sum(1 for ok in regime_results.values() if ok)
        session_pass = sum(1 for ok in session_results.values() if ok)

    ch_arr = np.asarray(challenger_returns, dtype=np.float64)
    ru_arr = np.asarray(rules_returns, dtype=np.float64)
    sample_count = max(len(challenger_returns), attribution_df.height if not attribution_df.is_empty() else 0)

    return StratifiedWindowMetrics(
        lookback_days=lookback_days,
        challenger_return=float(np.sum(ch_arr)) if ch_arr.size else 0.0,
        rules_return=float(np.sum(ru_arr)) if ru_arr.size else 0.0,
        challenger_sharpe=_sharpe(ch_arr),
        rules_sharpe=_sharpe(ru_arr),
        sample_count=sample_count,
        regime_buckets_passed=regime_pass,
        session_buckets_passed=session_pass,
        stratum_details=stratum_details,
    )


class AIPolicyLifecycleManager:
    """Staged AI policy promotion sieve and rollback controller."""

    def __init__(self, db_path: Path = RESEARCH_VAULT_PATH) -> None:
        self.db_path = db_path
        self._edge_decay_streak: dict[str, int] = {}

    def evaluate_probation_entry(
        self,
        challenger_metrics: Mapping[str, Any],
    ) -> ProbationEntryVerdict:
        from src.persistence import db as persistence

        strategy_id = str(challenger_metrics.get("strategy_id") or "")
        symbol = str(challenger_metrics.get("symbol") or "").upper()
        reasons: list[str] = []

        windows_in = challenger_metrics.get("windows")
        if isinstance(windows_in, dict) and "30d" in windows_in and "60d" in windows_in:
            w30 = self._window_from_payload(windows_in["30d"], LOOKBACK_30D)
            w60 = self._window_from_payload(windows_in["60d"], LOOKBACK_60D)
        else:
            attr_30 = _load_attribution_frame(self.db_path, lookback_days=LOOKBACK_30D)
            attr_60 = _load_attribution_frame(self.db_path, lookback_days=LOOKBACK_60D)
            sh_30 = _load_shadow_frame(self.db_path, lookback_days=LOOKBACK_30D, symbol=symbol or None)
            sh_60 = _load_shadow_frame(self.db_path, lookback_days=LOOKBACK_60D, symbol=symbol or None)
            w30 = _compute_stratified_window(
                lookback_days=LOOKBACK_30D,
                attribution_df=attr_30,
                shadow_df=sh_30,
            )
            w60 = _compute_stratified_window(
                lookback_days=LOOKBACK_60D,
                attribution_df=attr_60,
                shadow_df=sh_60,
            )

        tuple_count = persistence.count_shadow_rl_ledger_tuples(self.db_path)
        if tuple_count < AI_POLICY_MIN_LEDGER_TUPLES:
            reasons.append("insufficient_shadow_ledger_tuples")

        evicted, _ = persistence.is_shadow_policy_model_evicted(self.db_path)
        if evicted:
            reasons.append("model_eviction_lockout_active")

        for label, window in (("30d", w30), ("60d", w60)):
            if window.sample_count < MIN_STRATUM_SAMPLES * 2:
                reasons.append(f"{label}_insufficient_samples")
            if window.challenger_return <= window.rules_return:
                reasons.append(f"{label}_underperforms_rules_return")
            if window.challenger_sharpe <= window.rules_sharpe:
                reasons.append(f"{label}_underperforms_rules_sharpe")
            if window.regime_buckets_passed < MIN_REGIME_BUCKETS_PASS:
                reasons.append(f"{label}_insufficient_regime_strata")
            if window.session_buckets_passed < MIN_SESSION_BUCKETS_PASS:
                reasons.append(f"{label}_insufficient_session_strata")

        approved = len(reasons) == 0
        return ProbationEntryVerdict(
            approved=approved,
            reasons=tuple(reasons),
            window_30d=w30,
            window_60d=w60,
        )

    @staticmethod
    def _window_from_payload(payload: Mapping[str, Any], lookback_days: int) -> StratifiedWindowMetrics:
        return StratifiedWindowMetrics(
            lookback_days=lookback_days,
            challenger_return=float(payload.get("challenger_return", 0.0)),
            rules_return=float(payload.get("rules_return", 0.0)),
            challenger_sharpe=float(payload.get("challenger_sharpe", 0.0)),
            rules_sharpe=float(payload.get("rules_sharpe", 0.0)),
            sample_count=int(payload.get("sample_count", 0)),
            regime_buckets_passed=int(payload.get("regime_buckets_passed", 0)),
            session_buckets_passed=int(payload.get("session_buckets_passed", 0)),
            stratum_details=dict(payload.get("stratum_details", {})),
        )

    def evaluate_probation_edge(
        self,
        active_probation_policy: Mapping[str, Any],
    ) -> ProbationEdgeVerdict:
        strategy_id = str(active_probation_policy.get("strategy_id") or "global")
        symbol = str(active_probation_policy.get("symbol") or "").upper()
        shadow_df = _load_shadow_frame(self.db_path, lookback_days=14, symbol=symbol or None)
        if shadow_df.is_empty():
            return ProbationEdgeVerdict(
                maintains_edge=True,
                directive="NONE",
                challenger_edge=0.0,
                rules_edge=0.0,
                edge_ratio=1.0,
                decay_streak=0,
                reason="insufficient_recent_shadow_sample",
            )

        challenger_returns: list[float] = []
        rules_returns: list[float] = []
        for row in shadow_df.iter_rows(named=True):
            reward = row.get("realized_reward_24h")
            if reward is None:
                continue
            challenger_returns.append(
                _shadow_allocation(str(row.get("shadow_action_taken") or "")) * float(reward)
            )
            rules_returns.append(
                _rules_allocation(str(row.get("rules_engine_action") or "UNKNOWN")) * float(reward)
            )

        ch_arr = np.asarray(challenger_returns, dtype=np.float64)
        ru_arr = np.asarray(rules_returns, dtype=np.float64)
        challenger_edge = float(np.mean(ch_arr)) if ch_arr.size else 0.0
        rules_edge = float(np.mean(ru_arr)) if ru_arr.size else 0.0
        edge_ratio = challenger_edge / max(rules_edge, 1e-9) if rules_edge > 0 else (
            1.0 if challenger_edge >= 0 else 0.0
        )

        maintains = edge_ratio >= PROBATION_EDGE_DECAY_RATIO and challenger_edge >= rules_edge
        streak = self._edge_decay_streak.get(strategy_id, 0)
        if maintains:
            streak = 0
        else:
            streak += 1
        self._edge_decay_streak[strategy_id] = streak

        directive = "NONE"
        reason = "edge_stable"
        if streak >= PROBATION_EDGE_DECAY_STREAK:
            directive = "PREEMPTIVE_DEGRADE"
            reason = "probation_edge_decay_streak"
        elif not maintains:
            reason = "probation_edge_below_rules"

        return ProbationEdgeVerdict(
            maintains_edge=maintains,
            directive=directive,
            challenger_edge=challenger_edge,
            rules_edge=rules_edge,
            edge_ratio=float(edge_ratio),
            decay_streak=streak,
            reason=reason,
        )

    def evaluate_live_attribution_slo(
        self,
        policy_context: Mapping[str, Any],
    ) -> AttributionSloVerdict:
        strategy_id = str(policy_context.get("strategy_id") or "")
        symbol = str(policy_context.get("symbol") or "").upper()
        params = dict(policy_context.get("params") or {})
        drawdown_limit = float(
            params.get("max_live_drawdown_pct", DEFAULT_ATTRIBUTION_MAX_DRAWDOWN_PCT)
        )
        lookback_days = int(
            params.get("probation_attribution_lookback_days", PROBATION_ATTRIBUTION_LOOKBACK_DAYS)
        )
        attr = _load_attribution_frame(
            self.db_path,
            lookback_days=lookback_days,
            strategy_id=strategy_id or None,
        )
        if not attr.is_empty() and symbol:
            attr = attr.filter(pl.col("symbol") == symbol)
        if attr.is_empty():
            return AttributionSloVerdict(
                breached=False,
                reasons=("insufficient_attribution_sample",),
                live_sharpe=0.0,
                max_drawdown_pct=0.0,
                trade_count=0,
                drawdown_limit_pct=drawdown_limit,
            )
        pnls = attr["pnl"].to_numpy().astype(np.float64)
        trade_count = int(pnls.size)
        if trade_count < PROBATION_MIN_ATTRIBUTION_TRADES:
            return AttributionSloVerdict(
                breached=False,
                reasons=("insufficient_attribution_trades",),
                live_sharpe=_sharpe(pnls),
                max_drawdown_pct=_max_drawdown_pct(pnls),
                trade_count=trade_count,
                drawdown_limit_pct=drawdown_limit,
            )
        live_sharpe = _sharpe(pnls)
        max_drawdown = _max_drawdown_pct(pnls)
        reasons: list[str] = []
        if live_sharpe < PROBATION_MIN_LIVE_SHARPE:
            reasons.append("live_sharpe_below_zero")
        if max_drawdown > drawdown_limit:
            reasons.append("live_drawdown_breach")
        return AttributionSloVerdict(
            breached=bool(reasons),
            reasons=tuple(reasons),
            live_sharpe=live_sharpe,
            max_drawdown_pct=max_drawdown,
            trade_count=trade_count,
            drawdown_limit_pct=drawdown_limit,
        )

    def execute_attribution_rules_rollback(
        self,
        *,
        strategy_id: str,
        symbol: str,
        current_state: str,
        reason: str,
        change_journal: Any | None = None,
    ) -> StagedRollbackResult:
        from src.persistence import db as persistence

        rollback = self.execute_staged_rollback(
            current_state,
            strategy_id=strategy_id,
            symbol=symbol,
            reason=reason,
            catastrophic=False,
        )
        prior_backup = persistence.get_prior_config_backup(symbol, self.db_path)
        restored_params: dict[str, Any] = {}
        restored_version: int | None = None
        if prior_backup is not None:
            restored_version, raw_yaml = prior_backup
            try:
                document = yaml.safe_load(raw_yaml) or {}
                restored_params = dict(document.get("params") or {})
                restored_params["fallback_mode_active"] = True
                restored_params["fallback_class"] = "BASELINE_FALLBACK"
                restored_params["config_version_id"] = restored_version
            except Exception:
                restored_params = {}
        if change_journal is not None and restored_params:
            change_journal.record_recovery_rollback(
                scope_key=strategy_id,
                previous_state={"execution_state": current_state},
                requested_state={
                    "execution_state": STATE_PASSIVE,
                    "restored_params": restored_params,
                },
                rationale=reason,
                metadata={
                    "symbol": symbol,
                    "attribution_rollback": True,
                    "restored_version": restored_version,
                },
            )
        persistence.mark_failed_live_attribution(
            symbol=symbol,
            regime=str(restored_params.get("runtime_regime") or restored_params.get("regime") or "UNKNOWN"),
            strategy_id=strategy_id,
            db_path=self.db_path,
        )
        return StagedRollbackResult(
            prior_state=rollback.prior_state,
            new_state=rollback.new_state,
            tier=rollback.tier,
            catastrophic=rollback.catastrophic,
            reason=rollback.reason,
            eviction_lockout_until=rollback.eviction_lockout_until,
            restored_params=restored_params or None,
            restored_config_version_id=restored_version,
        )

    def evaluate_sovereign_promotion(
        self,
        policy_context: Mapping[str, Any],
    ) -> SovereignPromotionVerdict:
        strategy_id = str(policy_context.get("strategy_id") or "")
        symbol = str(policy_context.get("symbol") or "").upper()
        reasons: list[str] = []

        attr = _load_attribution_frame(self.db_path, lookback_days=30)
        if not attr.is_empty() and symbol:
            attr = attr.filter(pl.col("symbol") == symbol)

        filled_trade_count = attr.height
        if filled_trade_count < SOVEREIGN_MIN_FILLED_TRADES:
            reasons.append("insufficient_filled_trades")

        verified_trading_days = 0
        if not attr.is_empty():
            verified_trading_days = (
                attr.with_columns(pl.col("timestamp").str.slice(0, 10).alias("session_day"))
                .select("session_day")
                .unique()
                .height
            )
        clean_days = int(policy_context.get("probation_clean_trading_days", 0) or 0)
        verified_trading_days = max(verified_trading_days, clean_days)
        if verified_trading_days < SOVEREIGN_MIN_VERIFIED_TRADING_DAYS:
            reasons.append("insufficient_verified_trading_days")

        avg_slippage = 0.0
        fill_quality = 1.0
        if not attr.is_empty() and "slippage_pct" in attr.columns:
            avg_slippage = float(attr["slippage_pct"].mean())
            fill_quality = float(1.0 - np.clip(avg_slippage / SOVEREIGN_MAX_AVG_SLIPPAGE_PCT, 0.0, 1.0))
        if avg_slippage > SOVEREIGN_MAX_AVG_SLIPPAGE_PCT:
            reasons.append("execution_slippage_breach")
        if fill_quality < SOVEREIGN_MIN_FILL_QUALITY_SCORE:
            reasons.append("fill_quality_below_floor")

        shadow_df = _load_shadow_frame(self.db_path, lookback_days=30, symbol=symbol or None)
        edge_delta = 0.0
        if not shadow_df.is_empty():
            ch: list[float] = []
            ru: list[float] = []
            for row in shadow_df.iter_rows(named=True):
                reward = row.get("realized_reward_24h")
                if reward is None:
                    continue
                ch.append(_shadow_allocation(str(row.get("shadow_action_taken") or "")) * float(reward))
                ru.append(_rules_allocation(str(row.get("rules_engine_action") or "UNKNOWN")) * float(reward))
            if ch and ru:
                edge_delta = float(np.mean(ch) - np.mean(ru))
        if edge_delta < SOVEREIGN_MIN_EDGE_VS_RULES:
            reasons.append("sovereign_edge_vs_rules_insufficient")

        return SovereignPromotionVerdict(
            approved=len(reasons) == 0,
            reasons=tuple(reasons),
            verified_trading_days=verified_trading_days,
            filled_trade_count=filled_trade_count,
            avg_slippage_pct=avg_slippage,
            fill_quality_score=fill_quality,
        )

    def execute_staged_rollback(
        self,
        current_state: str,
        *,
        strategy_id: str,
        symbol: str,
        reason: str,
        catastrophic: bool = False,
        live_hit_rate: float | None = None,
    ) -> StagedRollbackResult:
        from src.persistence import db as persistence

        state = current_state if current_state in AI_POLICY_VALID_STATES else STATE_PASSIVE
        if catastrophic or (
            live_hit_rate is not None and live_hit_rate < CATASTROPHIC_HIT_RATE_FLOOR
        ) or reason == CATASTROPHIC_EXECUTION_DRIFT:
            lockout_until = (
                datetime.now(timezone.utc) + timedelta(days=14)
            ).isoformat()
            persistence.mark_shadow_policy_model_evicted(
                reason=reason,
                lockout_days=14,
                db_path=self.db_path,
            )
            persistence.upsert_ai_policy_lifecycle_state(
                strategy_id=strategy_id,
                symbol=symbol,
                execution_state=STATE_PASSIVE,
                probation_started_at=None,
                probation_clean_trading_days=0,
                last_anomaly_session=datetime.now(timezone.utc).date().isoformat(),
                eviction_lockout_until=lockout_until,
                db_path=self.db_path,
            )
            self._edge_decay_streak[strategy_id] = 0
            return StagedRollbackResult(
                prior_state=state,
                new_state=STATE_PASSIVE,
                tier=RollbackTier.CATASTROPHIC_EVICTION,
                catastrophic=True,
                reason=reason,
                eviction_lockout_until=lockout_until,
            )

        if state == STATE_SOVEREIGN:
            persistence.upsert_ai_policy_lifecycle_state(
                strategy_id=strategy_id,
                symbol=symbol,
                execution_state=STATE_PROBATIONAL,
                probation_started_at=datetime.now(timezone.utc).isoformat(),
                probation_clean_trading_days=0,
                last_anomaly_session=datetime.now(timezone.utc).date().isoformat(),
                eviction_lockout_until=None,
                db_path=self.db_path,
            )
            return StagedRollbackResult(
                prior_state=state,
                new_state=STATE_PROBATIONAL,
                tier=RollbackTier.SOVEREIGN_TO_PROBATIONAL,
                catastrophic=False,
                reason=reason,
            )

        if state == STATE_PROBATIONAL:
            persistence.upsert_ai_policy_lifecycle_state(
                strategy_id=strategy_id,
                symbol=symbol,
                execution_state=STATE_PASSIVE,
                probation_started_at=None,
                probation_clean_trading_days=0,
                last_anomaly_session=datetime.now(timezone.utc).date().isoformat(),
                eviction_lockout_until=None,
                db_path=self.db_path,
            )
            self._edge_decay_streak[strategy_id] = 0
            return StagedRollbackResult(
                prior_state=state,
                new_state=STATE_PASSIVE,
                tier=RollbackTier.PROBATIONAL_TO_PASSIVE,
                catastrophic=False,
                reason=reason,
            )

        return StagedRollbackResult(
            prior_state=state,
            new_state=state,
            tier=RollbackTier.NONE,
            catastrophic=False,
            reason="no_rollback_applicable",
        )

    def execute_velocity_shock_demotion(
        self,
        current_state: str,
        *,
        strategy_id: str,
        symbol: str,
        shock_reason: str,
    ) -> StagedRollbackResult:
        """
        Immediately demote active AI authority to PASSIVE on macro velocity shock.

        Bypasses staged trailing-window progression — sovereign and probational
        models are stripped of autonomous control in a single step.
        """
        from src.persistence import db as persistence

        state = current_state if current_state in AI_POLICY_VALID_STATES else STATE_PASSIVE
        if state not in (STATE_SOVEREIGN, STATE_PROBATIONAL):
            return StagedRollbackResult(
                prior_state=state,
                new_state=state,
                tier=RollbackTier.NONE,
                catastrophic=False,
                reason="velocity_shock_not_applicable",
            )

        reason = shock_reason or MACRO_VELOCITY_SHOCK_REASON
        demoted_at = datetime.now(timezone.utc).isoformat()
        persistence.upsert_ai_policy_lifecycle_state(
            strategy_id=strategy_id,
            symbol=symbol,
            execution_state=STATE_PASSIVE,
            probation_started_at=None,
            probation_clean_trading_days=0,
            last_anomaly_session=datetime.now(timezone.utc).date().isoformat(),
            eviction_lockout_until=None,
            velocity_shock_prior_state=state,
            velocity_shock_demoted_at=demoted_at,
            db_path=self.db_path,
        )
        self._edge_decay_streak[strategy_id] = 0
        return StagedRollbackResult(
            prior_state=state,
            new_state=STATE_PASSIVE,
            tier=RollbackTier.VELOCITY_SHOCK_DEMOTION,
            catastrophic=False,
            reason=reason,
        )

    def handle_macro_velocity_shock(
        self,
        shock_verdict: Mapping[str, Any],
        leg_contexts: list[tuple[str, str, dict[str, Any] | None]],
    ) -> VelocityShockDemotionResult:
        """
        Pre-emptive bypass handler — demote all active AI legs on velocity shock.

        Skips probation edge and sovereign promotion windows entirely.
        """
        from src.persistence import db as persistence

        shock_detected = bool(shock_verdict.get("shock_detected"))
        shock_reason = str(shock_verdict.get("reason") or MACRO_VELOCITY_SHOCK_REASON)
        if not shock_detected:
            return VelocityShockDemotionResult(
                demoted=False,
                transitions=(),
                reason="no_velocity_shock",
            )

        transitions: list[LifecycleTransition] = []
        for strategy_id, symbol, params in leg_contexts:
            stored = persistence.get_ai_policy_lifecycle_state(strategy_id, self.db_path)
            current = (
                str(stored.get("execution_state"))
                if stored is not None
                else str((params or {}).get("ai_policy_execution_state", STATE_PASSIVE))
            )
            if current not in AI_POLICY_VALID_STATES:
                current = STATE_PASSIVE
            rollback = self.execute_velocity_shock_demotion(
                current,
                strategy_id=strategy_id,
                symbol=symbol,
                shock_reason=shock_reason,
            )
            if rollback.tier != RollbackTier.VELOCITY_SHOCK_DEMOTION:
                continue
            transitions.append(
                LifecycleTransition(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    from_state=rollback.prior_state,
                    to_state=rollback.new_state,
                    reason=rollback.reason,
                    metadata={
                        "bypass": "macro_velocity_shock",
                        "triggered_metrics": list(
                            shock_verdict.get("triggered_metrics", ())
                        ),
                        "max_z_score": shock_verdict.get("max_z_score"),
                    },
                )
            )

        return VelocityShockDemotionResult(
            demoted=len(transitions) > 0,
            transitions=tuple(transitions),
            reason=shock_reason,
        )

    def evaluate_velocity_shock_shadow_recovery(
        self,
        *,
        strategy_id: str,
        symbol: str,
        demoted_at: str | None,
    ) -> VelocityShockRecoveryVerdict:
        shadow_df = _load_dual_policy_shadow_frame(
            self.db_path,
            symbol=symbol,
            since_timestamp=demoted_at,
        )
        if shadow_df.is_empty():
            return VelocityShockRecoveryVerdict(
                approved=False,
                reasons=("insufficient_dual_policy_shadow_log",),
                challenger_edge=0.0,
                rules_edge=0.0,
                edge_ratio=0.0,
                sample_count=0,
            )

        challenger_pnls: list[float] = []
        rules_pnls: list[float] = []
        for row in shadow_df.iter_rows(named=True):
            challenger_pnls.append(float(row.get("challenger_pnl") or 0.0))
            rules_pnls.append(float(row.get("rules_baseline_pnl") or 0.0))

        sample_count = len(challenger_pnls)
        if sample_count < SHOCK_RECOVERY_MIN_DUAL_SHADOW_SAMPLES:
            return VelocityShockRecoveryVerdict(
                approved=False,
                reasons=("insufficient_dual_policy_shadow_samples",),
                challenger_edge=0.0,
                rules_edge=0.0,
                edge_ratio=0.0,
                sample_count=sample_count,
            )

        ch_arr = np.asarray(challenger_pnls, dtype=np.float64)
        ru_arr = np.asarray(rules_pnls, dtype=np.float64)
        challenger_edge = float(np.mean(ch_arr))
        rules_edge = float(np.mean(ru_arr))
        edge_ratio = (
            challenger_edge / max(rules_edge, 1e-9)
            if rules_edge > 0
            else (1.0 if challenger_edge >= 0 else 0.0)
        )
        reasons: list[str] = []
        if challenger_edge < rules_edge:
            reasons.append("shadow_challenger_underperforms_rules")
        if edge_ratio < PROBATION_EDGE_DECAY_RATIO:
            reasons.append("shadow_edge_ratio_below_floor")
        return VelocityShockRecoveryVerdict(
            approved=len(reasons) == 0,
            reasons=tuple(reasons),
            challenger_edge=challenger_edge,
            rules_edge=rules_edge,
            edge_ratio=float(edge_ratio),
            sample_count=sample_count,
        )

    def attempt_velocity_shock_recovery(
        self,
        *,
        strategy_id: str,
        symbol: str,
        stored: Mapping[str, Any],
        stabilization: RegimeStabilizationVerdict | None = None,
    ) -> tuple[str, str, VelocityShockRecoveryVerdict | None]:
        prior_shock_state = str(stored.get("velocity_shock_prior_state") or "")
        if not prior_shock_state or prior_shock_state not in AI_POLICY_VALID_STATES:
            return str(stored.get("execution_state", STATE_PASSIVE)), "unchanged", None
        if str(stored.get("execution_state")) != STATE_PASSIVE:
            return str(stored.get("execution_state", STATE_PASSIVE)), "unchanged", None
        if stabilization is None or not stabilization.stabilized:
            reason = (
                stabilization.reason
                if stabilization is not None
                else "no_stabilization_verdict"
            )
            if reason == "cool_off_window_incomplete":
                return STATE_PASSIVE, "velocity_shock_cool_off_pending", None
            if reason == "composite_stress_above_hysteresis_threshold":
                return STATE_PASSIVE, "velocity_shock_hysteresis_blocked", None
            if reason == "no_recorded_velocity_shock":
                return STATE_PASSIVE, "unchanged", None
            return STATE_PASSIVE, reason, None

        recovery = self.evaluate_velocity_shock_shadow_recovery(
            strategy_id=strategy_id,
            symbol=symbol,
            demoted_at=str(stored.get("velocity_shock_demoted_at") or "") or None,
        )
        if not recovery.approved:
            return STATE_PASSIVE, "|".join(recovery.reasons), recovery
        return prior_shock_state, VELOCITY_SHOCK_RECOVERY_REASON, recovery

    def process_intraday_velocity_shock_recovery(
        self,
        leg_contexts: list[tuple[str, str, dict[str, Any] | None]],
        *,
        stabilization: RegimeStabilizationVerdict | None = None,
        change_journal: Any | None = None,
    ) -> dict[str, Any]:
        from src.persistence import db as persistence

        transitions: dict[str, Any] = {}
        stabilization_metadata: dict[str, Any] = {}
        if stabilization is not None:
            stabilization_metadata = {
                "stabilized": stabilization.stabilized,
                "reason": stabilization.reason,
                "stable_sub_sigma_bars": stabilization.stable_sub_sigma_bars,
                "required_bars": stabilization.required_bars,
                "composite_stress_z_score": stabilization.composite_stress_z_score,
                "recovery_sigma_threshold": stabilization.recovery_sigma_threshold,
                "shock_event_count_7d": stabilization.shock_event_count_7d,
                "shock_demotion_count_7d": stabilization.shock_demotion_count_7d,
            }
        for strategy_id, symbol, _params in leg_contexts:
            stored = persistence.get_ai_policy_lifecycle_state(strategy_id, self.db_path)
            if stored is None:
                continue
            current = str(stored.get("execution_state", STATE_PASSIVE))
            if current != STATE_PASSIVE:
                continue
            prior = current
            restored_state, recovery_reason, recovery_verdict = (
                self.attempt_velocity_shock_recovery(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    stored=stored,
                    stabilization=stabilization,
                )
            )
            if recovery_reason == "unchanged":
                continue

            transition_payload: dict[str, Any] = {
                "from": prior,
                "to": restored_state,
                "reason": recovery_reason,
            }
            if recovery_verdict is not None:
                transition_payload["velocity_shock_recovery"] = {
                    "approved": recovery_verdict.approved,
                    "edge_ratio": recovery_verdict.edge_ratio,
                    "sample_count": recovery_verdict.sample_count,
                }
            transitions[strategy_id] = transition_payload

            if restored_state == STATE_PASSIVE:
                continue

            if change_journal is not None:
                change_journal.record_ai_lifecycle_adjustment(
                    strategy_id=strategy_id,
                    previous_state={
                        "execution_state": prior,
                        "velocity_shock_prior_state": stored.get(
                            "velocity_shock_prior_state"
                        ),
                        "velocity_shock_demoted_at": stored.get(
                            "velocity_shock_demoted_at"
                        ),
                    },
                    requested_state={
                        "execution_state": restored_state,
                        "velocity_shock_prior_state": None,
                        "velocity_shock_demoted_at": None,
                        "recovery": {
                            "reason": recovery_reason,
                            "shadow_edge_ratio": (
                                recovery_verdict.edge_ratio
                                if recovery_verdict is not None
                                else None
                            ),
                            "shadow_sample_count": (
                                recovery_verdict.sample_count
                                if recovery_verdict is not None
                                else None
                            ),
                        },
                    },
                    rationale=recovery_reason,
                )

            probation_started = stored.get("probation_started_at")
            if restored_state == STATE_PROBATIONAL and probation_started is None:
                probation_started = datetime.now(timezone.utc).isoformat()

            persistence.upsert_ai_policy_lifecycle_state(
                strategy_id=strategy_id,
                symbol=symbol,
                execution_state=restored_state,
                probation_started_at=probation_started,
                probation_clean_trading_days=int(
                    stored.get("probation_clean_trading_days", 0) or 0
                ),
                last_anomaly_session=stored.get("last_anomaly_session"),
                eviction_lockout_until=stored.get("eviction_lockout_until"),
                velocity_shock_prior_state=None,
                velocity_shock_demoted_at=None,
                db_path=self.db_path,
            )

        return {
            "transitions": transitions,
            "regime_stabilization": stabilization_metadata,
        }

    def run_evening_progression(
        self,
        leg_contexts: list[tuple[str, str, dict[str, Any] | None]] | None = None,
        *,
        change_journal: Any | None = None,
    ) -> dict[str, Any]:
        from src.persistence import db as persistence

        if not leg_contexts:
            leg_contexts = self._default_leg_contexts()
        session_date = datetime.now(timezone.utc).date().isoformat()
        transitions: dict[str, Any] = {}

        for strategy_id, symbol, params in leg_contexts:
            stored = persistence.get_ai_policy_lifecycle_state(strategy_id, self.db_path)
            current = (
                str(stored.get("execution_state"))
                if stored is not None
                else str((params or {}).get("ai_policy_execution_state", STATE_PASSIVE))
            )
            if current not in AI_POLICY_VALID_STATES:
                current = STATE_PASSIVE

            prior = current
            reason = "unchanged"
            clean_days = int(stored.get("probation_clean_trading_days", 0) if stored else 0)
            probation_started = stored.get("probation_started_at") if stored else None
            velocity_shock_prior_state: str | None | object = ...
            velocity_shock_demoted_at: str | None | object = ...

            if current == STATE_PASSIVE:
                verdict = self.evaluate_probation_entry(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "params": params or {},
                    }
                )
                if verdict.approved:
                    current = STATE_PROBATIONAL
                    probation_started = datetime.now(timezone.utc).isoformat()
                    clean_days = 0
                    reason = "probation_entry_sieve_passed"
            elif current == STATE_PROBATIONAL:
                attr_slo = self.evaluate_live_attribution_slo(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "params": params or {},
                    }
                )
                if attr_slo.breached:
                    rollback = self.execute_attribution_rules_rollback(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        current_state=current,
                        reason="|".join(attr_slo.reasons),
                        change_journal=change_journal,
                    )
                    current = rollback.new_state
                    reason = "attribution_slo_rollback"
                else:
                    edge = self.evaluate_probation_edge(
                        {"strategy_id": strategy_id, "symbol": symbol, "params": params or {}}
                    )
                    if edge.directive == "PREEMPTIVE_DEGRADE":
                        rollback = self.execute_staged_rollback(
                            current,
                            strategy_id=strategy_id,
                            symbol=symbol,
                            reason=edge.reason,
                            catastrophic=False,
                        )
                        current = rollback.new_state
                        reason = rollback.reason
                    else:
                        had_anomaly = self._probation_anomaly_detected(strategy_id, session_date)
                        if had_anomaly:
                            clean_days = 0
                        elif stored is None or str(stored.get("updated_at", ""))[:10] != session_date:
                            clean_days += 1

                        sovereign_verdict = self.evaluate_sovereign_promotion(
                            {
                                "strategy_id": strategy_id,
                                "symbol": symbol,
                                "probation_clean_trading_days": clean_days,
                            }
                        )
                        if sovereign_verdict.approved:
                            current = STATE_SOVEREIGN
                            reason = "sovereign_promotion_criteria_met"
            elif current == STATE_SOVEREIGN:
                attr_slo = self.evaluate_live_attribution_slo(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "params": params or {},
                    }
                )
                if attr_slo.breached:
                    rollback = self.execute_attribution_rules_rollback(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        current_state=current,
                        reason="|".join(attr_slo.reasons),
                        change_journal=change_journal,
                    )
                    current = rollback.new_state
                    reason = "attribution_slo_rollback"
                else:
                    edge = self.evaluate_probation_edge(
                        {"strategy_id": strategy_id, "symbol": symbol, "params": params or {}}
                    )
                    if not edge.maintains_edge and edge.decay_streak >= 1:
                        rollback = self.execute_staged_rollback(
                            current,
                            strategy_id=strategy_id,
                            symbol=symbol,
                            reason="sovereign_edge_decay",
                            catastrophic=False,
                        )
                        current = rollback.new_state
                        reason = rollback.reason

            persistence.upsert_ai_policy_lifecycle_state(
                strategy_id=strategy_id,
                symbol=symbol,
                execution_state=current,
                probation_started_at=probation_started,
                probation_clean_trading_days=clean_days,
                last_anomaly_session=session_date
                if self._probation_anomaly_detected(strategy_id, session_date)
                else (stored.get("last_anomaly_session") if stored else None),
                eviction_lockout_until=stored.get("eviction_lockout_until") if stored else None,
                velocity_shock_prior_state=velocity_shock_prior_state,
                velocity_shock_demoted_at=velocity_shock_demoted_at,
                db_path=self.db_path,
            )
            transition_payload: dict[str, Any] = {
                "from": prior,
                "to": current,
                "reason": reason,
                "probation_clean_trading_days": clean_days,
            }
            transitions[strategy_id] = transition_payload

        return {"transitions": transitions, "session_date": session_date}

    def _default_leg_contexts(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        from src.config import load_config

        config = load_config()
        return [
            (s.strategy_id, s.symbol, dict(s.params))
            for s in config.strategies
            if s.enabled
        ]

    def _probation_anomaly_detected(self, strategy_id: str, session_date: str) -> bool:
        if not self.db_path.exists():
            return False
        cutoff = f"{session_date}T00:00:00"
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM system_health_ledger
                    WHERE strategy_id = ?
                      AND timestamp >= ?
                      AND new_status IN ('DEFENSIVE', 'DEGRADED')
                    """,
                    (strategy_id, cutoff),
                ).fetchone()
            return row is not None and int(row[0]) > 0
        except sqlite3.Error:
            return False
