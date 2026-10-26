"""
Operational chaos simulator and historical state replay harness.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from unittest.mock import patch

from src.config import RiskConfig, load_config
from src.control.config_watcher import ConfigWatcher
from src.control.maintenance_scheduler import (
    MaintenanceScheduler,
    audit_champion_staleness,
    _validate_pre_open_payload,
)
from src.engine.config_engine import (
    ConfigurationPrecedenceResolver,
    assess_vault_integrity,
)
from src.engine.drift_evaluator import (
    CLASS_OLD_AND_WRONG,
    DIRECTIVE_FORCE_MINI_SWEEP,
    DIRECTIVE_STAGED_ROLLBACK,
    ModelDriftEvaluator,
)
from src.engine.policy_lifecycle import (
    AIPolicyLifecycleManager,
    RollbackTier,
    STATE_PASSIVE,
    STATE_PROBATIONAL,
)
from src.engine.promotion_dry_run import PromotionDryRunEngine
from src.engine.telemetry_gate import BrokerTelemetryBridge, CapitalGate
from src.models import Account, Bar
from src.persistence import db as persistence
from src.persistence.db import RESEARCH_VAULT_PATH
from src.router.risk_manager import (
    AI_POLICY_PASSIVE_SHADOW,
    AI_POLICY_PROBATIONAL,
    AI_POLICY_SOVEREIGN,
    RiskManager,
    apply_fallback_defensive_profile,
)


class ChaosDrill(str, Enum):
    STALE_CHAMPION = "STALE_CHAMPION"
    FAILED_MAINTENANCE = "FAILED_MAINTENANCE"
    EVICTED_MODEL = "EVICTED_MODEL"
    STORAGE_OUTAGE = "STORAGE_OUTAGE"
    BROKER_REJECT_STORM = "BROKER_REJECT_STORM"


@dataclass(frozen=True)
class ChaosDrillResult:
    drill: ChaosDrill
    passed: bool
    observations: dict[str, Any]
    assertions: tuple[str, ...]


@dataclass(frozen=True)
class ReplayPolicyCheckpoint:
    bar_index: int
    execution_state: str
    params: dict[str, Any]
    trigger_rollback: bool = False
    rollback_reason: str = ""
    catastrophic: bool = False
    live_hit_rate: float | None = None
    expected_post_state: str | None = None
    expected_tier: str | None = None


@dataclass(frozen=True)
class ReplayTransitionRecord:
    bar_index: int
    bar_timestamp: str
    prior_state: str
    new_state: str
    tier: str
    reason: str


@dataclass(frozen=True)
class HistoricalReplayResult:
    strategy_id: str
    symbol: str
    bars_replayed: int
    transitions: tuple[ReplayTransitionRecord, ...]
    passed: bool
    failures: tuple[str, ...]


@dataclass
class AdaptabilityChaosSimulator:
    """Injects synthetic operational faults and verifies structural resilience."""

    vault_path: Path = RESEARCH_VAULT_PATH
    maintenance_scheduler: MaintenanceScheduler | None = None
    config_watcher: ConfigWatcher | None = None
    policy_lifecycle: AIPolicyLifecycleManager | None = None
    _restore_hooks: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.policy_lifecycle is None:
            self.policy_lifecycle = AIPolicyLifecycleManager(db_path=self.vault_path)
        if self.config_watcher is None:
            self.config_watcher = ConfigWatcher()

    def run_drill(self, drill: ChaosDrill) -> ChaosDrillResult:
        runners = {
            ChaosDrill.STALE_CHAMPION: self.drill_stale_champion,
            ChaosDrill.FAILED_MAINTENANCE: self.drill_failed_maintenance,
            ChaosDrill.EVICTED_MODEL: self.drill_evicted_model,
            ChaosDrill.STORAGE_OUTAGE: self.drill_storage_outage,
            ChaosDrill.BROKER_REJECT_STORM: self.drill_broker_reject_storm,
        }
        try:
            return runners[drill]()
        finally:
            self._restore_all()

    def drill_stale_champion(self) -> ChaosDrillResult:
        symbol = "QQQ"
        stale_promoted_at = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        assertions: list[str] = []
        observations: dict[str, Any] = {}

        original_promoted_at = self._read_champion_promoted_at(symbol)
        self._write_champion_promoted_at(symbol, stale_promoted_at)
        self._restore_hooks.append(
            lambda: self._write_champion_promoted_at(symbol, original_promoted_at)
        )

        staleness = audit_champion_staleness(self.vault_path, [symbol], max_age_days=14)
        observations["staleness"] = staleness.get(symbol, {})
        assert staleness[symbol]["stale"] is True
        assertions.append("staleness_detected")

        from src.engine.drift_evaluator import ensure_state_drift_schema

        ensure_state_drift_schema(self.vault_path)
        self._ensure_promotion_attribution_table()
        with sqlite3.connect(self.vault_path) as conn:
            for idx in range(12):
                conn.execute(
                    """
                    INSERT INTO active_promotion_attribution (
                        timestamp, strategy_id, symbol, trade_pnl, bars_held,
                        slippage_pct, execution_tax, live_sharpe_10t,
                        holding_drift_ratio, config_version_id, params_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (datetime.now(timezone.utc) - timedelta(hours=idx)).isoformat(),
                        "mean_reversion_qqq",
                        symbol,
                        -2.5,
                        3,
                        0.002,
                        0.002,
                        -0.4,
                        1.1,
                        1,
                        "{}",
                    ),
                )

        evaluator = ModelDriftEvaluator(db_path=self.vault_path, stale_age_days=14)
        verdict = evaluator.evaluate_champion(
            symbol=symbol,
            regime="CALM_MR",
            strategy_id="mean_reversion_qqq",
            register_alert=False,
        )
        observations["drift_verdict"] = {
            "classification": verdict.classification,
            "directive": verdict.directive,
            "reason": verdict.reason,
        }
        assert verdict.classification == CLASS_OLD_AND_WRONG
        assert verdict.directive in {
            DIRECTIVE_STAGED_ROLLBACK,
            DIRECTIVE_FORCE_MINI_SWEEP,
        }
        assertions.append("drift_rollback_directive")

        fallback_params = apply_fallback_defensive_profile(
            {
                "fallback_mode_active": True,
                "fallback_class": "BASELINE_FALLBACK",
                "max_position_pct": 0.95,
            }
        )
        observations["fallback_max_position_pct"] = fallback_params["max_position_pct"]
        assert float(fallback_params["max_position_pct"]) < 0.95
        assertions.append("fallback_profile_contracts_capacity")

        pre_open_payload = {
            "config_ready": True,
            "staleness": staleness,
            "auto_seed": {"seeded": False, "symbols": []},
            "inventory": {symbol: {"easy_to_borrow": True, "shortable": True}},
        }
        ok, reason = _validate_pre_open_payload(pre_open_payload)
        observations["pre_open_validation"] = {"ok": ok, "reason": reason}
        assert ok is False
        assertions.append("pre_open_blocks_stale_champion")

        return ChaosDrillResult(
            drill=ChaosDrill.STALE_CHAMPION,
            passed=True,
            observations=observations,
            assertions=tuple(assertions),
        )

    def drill_failed_maintenance(self) -> ChaosDrillResult:
        scheduler = self.maintenance_scheduler or MaintenanceScheduler(
            self.config_watcher,
            vault_path=self.vault_path,
        )
        assertions: list[str] = []
        observations: dict[str, Any] = {}

        prior_status = persistence.get_maintenance_job_status(
            "pre_open_readiness",
            self.vault_path,
        )
        persistence.upsert_maintenance_job_status(
            job_id="pre_open_readiness",
            status="validation_failed",
            validation_passed=False,
            error="simulated maintenance crash loop",
            db_path=self.vault_path,
        )
        self._restore_hooks.append(
            lambda: persistence.upsert_maintenance_job_status(
                job_id="pre_open_readiness",
                status=str((prior_status or {}).get("last_status") or "success"),
                validation_passed=bool((prior_status or {}).get("validation_passed", 1)),
                db_path=self.vault_path,
                success=(prior_status or {}).get("last_status") == "success",
            )
        )

        gate = scheduler.live_consumption_gate()
        observations["consumption_gate"] = {
            "allowed": gate.allowed,
            "reason": gate.reason,
        }
        assert gate.allowed is False
        assertions.append("live_consumption_gate_blocked")

        allowed = scheduler.assert_live_consumption_allowed()
        observations["assert_live_consumption_allowed"] = allowed
        assert allowed is False
        assertions.append("orchestrator_consumption_assertion_fails")

        scheduler._block_live_consumption("simulated_failed_maintenance")
        self._restore_hooks.append(scheduler._release_live_consumption)
        blocked_gate = scheduler.live_consumption_gate()
        observations["manual_block"] = {
            "allowed": blocked_gate.allowed,
            "reason": blocked_gate.reason,
        }
        assert blocked_gate.allowed is False
        assertions.append("manual_consumption_block_active")

        return ChaosDrillResult(
            drill=ChaosDrill.FAILED_MAINTENANCE,
            passed=True,
            observations=observations,
            assertions=tuple(assertions),
        )

    def drill_evicted_model(self) -> ChaosDrillResult:
        strategy_id = "mean_reversion_qqq"
        symbol = "QQQ"
        assertions: list[str] = []
        observations: dict[str, Any] = {}

        prior = persistence.get_ai_policy_lifecycle_state(strategy_id, self.vault_path)
        persistence.upsert_baseline_regime_champion(
            symbol="GLOBAL",
            params={"weights": [0.1, 0.2, 0.3]},
            promoted_at=datetime.now(timezone.utc).isoformat(),
            db_path=self.vault_path,
        )
        with sqlite3.connect(self.vault_path) as conn:
            conn.execute(
                """
                UPDATE regime_champions
                SET regime = ?
                WHERE symbol = ?
                """,
                ("SHADOW_ML_OPTIMIZED_WEIGHTS", "GLOBAL"),
            )
        persistence.upsert_ai_policy_lifecycle_state(
            strategy_id=strategy_id,
            symbol=symbol,
            execution_state=AI_POLICY_SOVEREIGN,
            probation_clean_trading_days=12,
            db_path=self.vault_path,
        )
        if prior is not None:
            self._restore_hooks.append(
                lambda: persistence.upsert_ai_policy_lifecycle_state(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    execution_state=str(prior.get("execution_state") or AI_POLICY_PASSIVE_SHADOW),
                    probation_started_at=prior.get("probation_started_at"),
                    probation_clean_trading_days=int(
                        prior.get("probation_clean_trading_days", 0) or 0
                    ),
                    last_anomaly_session=prior.get("last_anomaly_session"),
                    eviction_lockout_until=prior.get("eviction_lockout_until"),
                    db_path=self.vault_path,
                )
            )

        rollback = self.policy_lifecycle.execute_staged_rollback(
            AI_POLICY_SOVEREIGN,
            strategy_id=strategy_id,
            symbol=symbol,
            reason="execution_drift",
            catastrophic=True,
            live_hit_rate=0.10,
        )
        observations["rollback"] = {
            "prior_state": rollback.prior_state,
            "new_state": rollback.new_state,
            "tier": rollback.tier.value,
            "catastrophic": rollback.catastrophic,
            "eviction_lockout_until": rollback.eviction_lockout_until,
        }
        assert rollback.tier == RollbackTier.CATASTROPHIC_EVICTION
        assert rollback.new_state == AI_POLICY_PASSIVE_SHADOW
        assertions.append("catastrophic_eviction_to_passive")

        stored = persistence.get_ai_policy_lifecycle_state(strategy_id, self.vault_path)
        assert stored is not None
        assert stored["execution_state"] == AI_POLICY_PASSIVE_SHADOW
        assertions.append("lifecycle_state_persisted_passive")

        evicted, lockout_until = persistence.is_shadow_policy_model_evicted(self.vault_path)
        observations["eviction_lockout"] = {
            "evicted": evicted,
            "lockout_until": lockout_until,
        }
        assert evicted is True
        assertions.append("eviction_lockout_engaged")

        persistence.upsert_ai_policy_lifecycle_state(
            strategy_id=strategy_id,
            symbol=symbol,
            execution_state=AI_POLICY_PROBATIONAL,
            probation_clean_trading_days=3,
            db_path=self.vault_path,
        )
        probation_rollback = self.policy_lifecycle.execute_staged_rollback(
            AI_POLICY_PROBATIONAL,
            strategy_id=strategy_id,
            symbol=symbol,
            reason="probation_edge_decay",
            catastrophic=False,
        )
        observations["staged_probation_rollback"] = {
            "prior_state": probation_rollback.prior_state,
            "new_state": probation_rollback.new_state,
            "tier": probation_rollback.tier.value,
        }
        assert probation_rollback.tier.value == "PROBATIONAL_TO_PASSIVE"
        assertions.append("probation_staged_de_risk_layer")

        return ChaosDrillResult(
            drill=ChaosDrill.EVICTED_MODEL,
            passed=True,
            observations=observations,
            assertions=tuple(assertions),
        )

    def drill_storage_outage(self) -> ChaosDrillResult:
        strategy_id = "mean_reversion_qqq"
        assertions: list[str] = []
        observations: dict[str, Any] = {}

        watcher = self.config_watcher or ConfigWatcher()
        watcher._config = load_config()
        watcher._last_fetch = time.monotonic()
        watcher.set_runtime_params_override(
            strategy_id,
            {
                "ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW,
                "max_position_pct": 0.50,
            },
            source="storage_outage_cache",
        )

        with self._storage_outage_context():
            integrity = assess_vault_integrity(self.vault_path)
            observations["vault_integrity"] = {
                "readable": integrity.readable,
                "integrity_ok": integrity.integrity_ok,
                "error": integrity.error,
            }
            assert integrity.readable is False or integrity.integrity_ok is False
            assertions.append("vault_integrity_degraded")

            cached = self._resolve_local_cache_fallback(
                strategy_id,
                {"max_position_pct": 0.95, "entry_z": 1.75},
                watcher,
            )
            observations["local_cache_params"] = cached
            assert cached["ai_policy_execution_state"] == AI_POLICY_PASSIVE_SHADOW
            assert float(cached["max_position_pct"]) == 0.50
            assertions.append("runtime_override_cache_served")

            resolver = ConfigurationPrecedenceResolver(db_path=self.vault_path)
            try:
                resolver.resolve_strategy_params(
                    strategy_id,
                    yaml_params={"max_position_pct": 0.95},
                    config_watcher=watcher,
                )
                vault_read_succeeded = True
            except sqlite3.Error:
                vault_read_succeeded = False
            observations["vault_read_blocked"] = not vault_read_succeeded
            assert vault_read_succeeded is False
            assertions.append("vault_reads_blocked_without_crash")

        restored = assess_vault_integrity(self.vault_path)
        observations["post_outage_integrity_ok"] = restored.integrity_ok
        assert restored.integrity_ok is True
        assertions.append("vault_restored_after_outage")

        return ChaosDrillResult(
            drill=ChaosDrill.STORAGE_OUTAGE,
            passed=True,
            observations=observations,
            assertions=tuple(assertions),
        )

    def drill_broker_reject_storm(self) -> ChaosDrillResult:
        assertions: list[str] = []
        observations: dict[str, Any] = {}

        risk = RiskManager(RiskConfig())
        gate = CapitalGate(risk)
        bridge = BrokerTelemetryBridge(gate)
        healthy = Account(equity=100_000.0, cash=80_000.0, buying_power=75_000.0)
        bridge.feed_capital_gate_telemetry(
            {"event": "account_sync", "account": healthy, "leg_count": 1}
        )

        for _ in range(4):
            bridge.feed_capital_gate_telemetry(
                {"event": "order_reject", "reason": "insufficient_buying_power"}
            )

        signal = bridge.feed_capital_gate_telemetry(
            {"event": "order_reject", "reason": "insufficient_buying_power"}
        )
        snapshot = gate.snapshot()
        observations["capital_gate"] = {
            "emergency_active": signal.emergency_active,
            "block_new_entries": signal.block_new_entries,
            "position_size_multiplier": signal.position_size_multiplier,
            "reject_count_window": snapshot.reject_count_window,
            "reason": signal.reason,
        }
        assert signal.emergency_active is True
        assert signal.block_new_entries is True
        assertions.append("reject_storm_blocks_entries")

        clamped = gate.apply_emergency_to_max_position(0.95)
        observations["clamped_max_position_pct"] = clamped
        assert clamped == 0.0
        assertions.append("position_cap_clamped_to_zero")

        assert snapshot.reject_count_window >= 3
        assertions.append("reject_window_threshold_breached")

        return ChaosDrillResult(
            drill=ChaosDrill.BROKER_REJECT_STORM,
            passed=True,
            observations=observations,
            assertions=tuple(assertions),
        )

    @contextmanager
    def _storage_outage_context(self) -> Iterator[None]:
        target = self.vault_path.resolve()
        original_connect = sqlite3.connect

        def blocked_connect(database: str | Path, *args: Any, **kwargs: Any):
            db_path = Path(str(database)).resolve()
            if db_path == target:
                raise sqlite3.OperationalError("simulated storage outage")
            return original_connect(database, *args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=blocked_connect):
            yield

    def _resolve_local_cache_fallback(
        self,
        strategy_id: str,
        yaml_params: Mapping[str, Any],
        config_watcher: ConfigWatcher,
    ) -> dict[str, Any]:
        merged = dict(yaml_params)
        override = config_watcher._runtime_param_overrides.get(strategy_id)
        if override:
            merged.update(override)
        return merged

    def _ensure_promotion_attribution_table(self) -> None:
        with sqlite3.connect(self.vault_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS active_promotion_attribution (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_pnl REAL NOT NULL,
                    bars_held INTEGER NOT NULL,
                    slippage_pct REAL NOT NULL,
                    execution_tax REAL NOT NULL,
                    live_sharpe_10t REAL,
                    holding_drift_ratio REAL,
                    config_version_id INTEGER,
                    params_json TEXT NOT NULL
                );
                """
            )

    def _read_champion_promoted_at(self, symbol: str) -> str | None:
        persistence.ensure_regime_champions_table(self.vault_path)
        with sqlite3.connect(self.vault_path) as conn:
            row = conn.execute(
                """
                SELECT promoted_at FROM regime_champions
                WHERE symbol = ? ORDER BY promoted_at DESC LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return str(row[0]) if row else None

    def _write_champion_promoted_at(self, symbol: str, promoted_at: str | None) -> None:
        if promoted_at is None:
            return
        persistence.ensure_regime_champions_table(self.vault_path)
        with sqlite3.connect(self.vault_path) as conn:
            conn.execute(
                """
                UPDATE regime_champions
                SET promoted_at = ?
                WHERE symbol = ?
                """,
                (promoted_at, symbol.upper()),
            )

    def _restore_all(self) -> None:
        while self._restore_hooks:
            hook = self._restore_hooks.pop()
            try:
                hook()
            except Exception:
                pass


@dataclass
class HistoricalStateReplayHarness:
    """Replays historical bars and policy checkpoints to verify rollback timing."""

    vault_path: Path = RESEARCH_VAULT_PATH
    dry_run_engine: PromotionDryRunEngine | None = None

    def replay(
        self,
        *,
        strategy_id: str,
        symbol: str,
        bars: list[Bar],
        policy_timeline: list[ReplayPolicyCheckpoint],
        challenger_id: str = "replay-challenger",
    ) -> HistoricalReplayResult:
        if not bars:
            return HistoricalReplayResult(
                strategy_id=strategy_id,
                symbol=symbol,
                bars_replayed=0,
                transitions=(),
                passed=False,
                failures=("empty_bar_slice",),
            )

        owns_engine = self.dry_run_engine is None
        engine = self.dry_run_engine or PromotionDryRunEngine(
            production_db_path=self.vault_path
        )
        try:
            engine._bootstrap_dry_run_vault()
            assert engine._memory is not None
            dry_path = engine._memory.vault_path
            self._seed_replay_vault_on_path(
                dry_path,
                strategy_id,
                symbol,
                challenger_id,
            )
            lifecycle = AIPolicyLifecycleManager(db_path=dry_path)

            transitions: list[ReplayTransitionRecord] = []
            failures: list[str] = []
            timeline_by_index = {cp.bar_index: cp for cp in policy_timeline}

            current_state = STATE_PASSIVE
            persistence.upsert_ai_policy_lifecycle_state(
                strategy_id=strategy_id,
                symbol=symbol,
                execution_state=current_state,
                db_path=dry_path,
            )

            for bar_index, bar in enumerate(bars):
                checkpoint = timeline_by_index.get(bar_index)
                if checkpoint is None:
                    continue

                current_state = checkpoint.execution_state
                persistence.upsert_ai_policy_lifecycle_state(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    execution_state=current_state,
                    db_path=dry_path,
                )

                if not checkpoint.trigger_rollback:
                    continue

                rollback = lifecycle.execute_staged_rollback(
                    current_state,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    reason=checkpoint.rollback_reason or "replay_trigger",
                    catastrophic=checkpoint.catastrophic,
                    live_hit_rate=checkpoint.live_hit_rate,
                )
                transitions.append(
                    ReplayTransitionRecord(
                        bar_index=bar_index,
                        bar_timestamp=bar.timestamp.isoformat(),
                        prior_state=rollback.prior_state,
                        new_state=rollback.new_state,
                        tier=rollback.tier.value,
                        reason=rollback.reason,
                    )
                )
                current_state = rollback.new_state

                if checkpoint.expected_post_state is not None:
                    if rollback.new_state != checkpoint.expected_post_state:
                        failures.append(
                            f"bar {bar_index}: expected state "
                            f"{checkpoint.expected_post_state}, got {rollback.new_state}"
                        )
                if checkpoint.expected_tier is not None:
                    if rollback.tier.value != checkpoint.expected_tier:
                        failures.append(
                            f"bar {bar_index}: expected tier "
                            f"{checkpoint.expected_tier}, got {rollback.tier.value}"
                        )

            return HistoricalReplayResult(
                strategy_id=strategy_id,
                symbol=symbol,
                bars_replayed=len(bars),
                transitions=tuple(transitions),
                passed=not failures,
                failures=tuple(failures),
            )
        finally:
            if owns_engine:
                engine.close()

    def _seed_replay_vault_on_path(
        self,
        dry_path: Path,
        strategy_id: str,
        symbol: str,
        challenger_id: str,
    ) -> None:
        from src.engine.challenger_registry import ChallengerRegistry

        registry = ChallengerRegistry(db_path=dry_path)
        registry.register_challenger(
            challenger_id=challenger_id,
            champion_id="champion:GLOBAL:SHADOW_ML_OPTIMIZED_WEIGHTS",
            model_metadata={
                "strategy_id": strategy_id,
                "symbol": symbol,
            },
        )
        persistence.upsert_baseline_regime_champion(
            symbol=symbol,
            params={"runtime_regime": "CALM_MR"},
            promoted_at=datetime.now(timezone.utc).isoformat(),
            db_path=dry_path,
        )
