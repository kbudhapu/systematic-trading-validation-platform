"""
Non-destructive promotion and lifecycle dry-run engine.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.engine.challenger_registry import ChallengerRegistry, ensure_challenger_schema
from src.engine.policy_lifecycle import (
    AIPolicyLifecycleManager,
    STATE_PASSIVE,
    STATE_PROBATIONAL,
    STATE_SOVEREIGN,
)
from src.persistence import db as persistence
from src.persistence.db import RESEARCH_VAULT_PATH
from src.router.risk_manager import AI_POLICY_VALID_STATES

@dataclass(frozen=True)
class DryRunTransition:
    strategy_id: str
    symbol: str
    from_state: str
    to_state: str
    reason: str
    tier: str | None = None
    catastrophic: bool = False


@dataclass(frozen=True)
class DryRunProgressionResult:
    challenger_id: str
    champion_id: str
    strategy_id: str
    symbol: str
    initial_state: str
    final_state: str
    transitions: tuple[DryRunTransition, ...]
    probation_checks: dict[str, Any]
    sovereign_checks: dict[str, Any] | None
    rollback_checks: dict[str, Any] | None
    production_vault_unmodified: bool


@dataclass
class DryRunMemory:
    vault_path: Path
    transitions: list[DryRunTransition] = field(default_factory=list)
    lifecycle_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PromotionDryRunEngine:
    """
    Evaluates challenger lifecycle progression in an isolated vault copy.

    All writes are confined to dry-run memory; production SQLite is read-only.
    """

    production_db_path: Path = RESEARCH_VAULT_PATH
    _memory: DryRunMemory | None = field(default=None, init=False, repr=False)
    _temp_dir: tempfile.TemporaryDirectory[str] | None = field(
        default=None, init=False, repr=False
    )

    def simulate_lifecycle_progression(
        self,
        challenger_id: str,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        simulate_evening: bool = True,
        force_catastrophic_rollback: bool = False,
        rollback_reason: str = "dry_run_catastrophic_eviction",
        live_hit_rate: float | None = None,
    ) -> DryRunProgressionResult:
        production_fingerprint = self._vault_fingerprint(self.production_db_path)
        registry = ChallengerRegistry(db_path=self.production_db_path)
        challenger = registry.get_challenger(challenger_id)
        if challenger is None:
            raise ValueError(f"unknown challenger_id: {challenger_id}")

        meta = challenger.model_metadata
        resolved_strategy = strategy_id or str(meta.get("strategy_id") or "mean_reversion_qqq")
        resolved_symbol = symbol or str(meta.get("symbol") or "QQQ").upper()

        self._bootstrap_dry_run_vault()
        assert self._memory is not None
        dry_path = self._memory.vault_path
        lifecycle = AIPolicyLifecycleManager(db_path=dry_path)

        stored = persistence.get_ai_policy_lifecycle_state(resolved_strategy, dry_path)
        initial_state = (
            str(stored.get("execution_state"))
            if stored is not None
            else STATE_PASSIVE
        )
        if initial_state not in AI_POLICY_VALID_STATES:
            initial_state = STATE_PASSIVE

        transitions: list[DryRunTransition] = []
        probation_checks: dict[str, Any] = {}
        sovereign_checks: dict[str, Any] | None = None
        rollback_checks: dict[str, Any] | None = None
        current_state = initial_state

        if current_state == STATE_PASSIVE:
            verdict = lifecycle.evaluate_probation_entry(
                {
                    "strategy_id": resolved_strategy,
                    "symbol": resolved_symbol,
                    "params": meta,
                }
            )
            probation_checks = {
                "approved": verdict.approved,
                "reasons": list(verdict.reasons),
            }
            if verdict.approved:
                persistence.upsert_ai_policy_lifecycle_state(
                    strategy_id=resolved_strategy,
                    symbol=resolved_symbol,
                    execution_state=STATE_PROBATIONAL,
                    probation_started_at=datetime.now(timezone.utc).isoformat(),
                    probation_clean_trading_days=0,
                    db_path=dry_path,
                )
                transitions.append(
                    DryRunTransition(
                        strategy_id=resolved_strategy,
                        symbol=resolved_symbol,
                        from_state=current_state,
                        to_state=STATE_PROBATIONAL,
                        reason="probation_entry_sieve_passed",
                    )
                )
                current_state = STATE_PROBATIONAL

        if current_state == STATE_PROBATIONAL and not force_catastrophic_rollback:
            stored = persistence.get_ai_policy_lifecycle_state(resolved_strategy, dry_path)
            clean_days = int(stored.get("probation_clean_trading_days", 0) if stored else 0)
            sovereign_verdict = lifecycle.evaluate_sovereign_promotion(
                {
                    "strategy_id": resolved_strategy,
                    "symbol": resolved_symbol,
                    "probation_clean_trading_days": clean_days + 10,
                }
            )
            sovereign_checks = {
                "approved": sovereign_verdict.approved,
                "reasons": list(sovereign_verdict.reasons),
                "verified_trading_days": sovereign_verdict.verified_trading_days,
                "filled_trade_count": sovereign_verdict.filled_trade_count,
            }
            if sovereign_verdict.approved:
                persistence.upsert_ai_policy_lifecycle_state(
                    strategy_id=resolved_strategy,
                    symbol=resolved_symbol,
                    execution_state=STATE_SOVEREIGN,
                    probation_clean_trading_days=clean_days + 10,
                    db_path=dry_path,
                )
                transitions.append(
                    DryRunTransition(
                        strategy_id=resolved_strategy,
                        symbol=resolved_symbol,
                        from_state=current_state,
                        to_state=STATE_SOVEREIGN,
                        reason="sovereign_promotion_criteria_met",
                    )
                )
                current_state = STATE_SOVEREIGN

        if force_catastrophic_rollback or current_state in {
            STATE_PROBATIONAL,
            STATE_SOVEREIGN,
        }:
            rollback = lifecycle.execute_staged_rollback(
                current_state,
                strategy_id=resolved_strategy,
                symbol=resolved_symbol,
                reason=rollback_reason,
                catastrophic=force_catastrophic_rollback
                or current_state == STATE_SOVEREIGN,
                live_hit_rate=live_hit_rate,
            )
            rollback_checks = {
                "prior_state": rollback.prior_state,
                "new_state": rollback.new_state,
                "tier": rollback.tier.value,
                "catastrophic": rollback.catastrophic,
                "eviction_lockout_until": rollback.eviction_lockout_until,
            }
            if rollback.new_state != current_state:
                transitions.append(
                    DryRunTransition(
                        strategy_id=resolved_strategy,
                        symbol=resolved_symbol,
                        from_state=current_state,
                        to_state=rollback.new_state,
                        reason=rollback.reason,
                        tier=rollback.tier.value,
                        catastrophic=rollback.catastrophic,
                    )
                )
                current_state = rollback.new_state

        if simulate_evening and not force_catastrophic_rollback:
            evening = lifecycle.run_evening_progression(
                [(resolved_strategy, resolved_symbol, dict(meta))]
            )
            leg_result = evening.get(resolved_strategy, {})
            evening_to = str(leg_result.get("execution_state") or current_state)
            if evening_to != current_state:
                transitions.append(
                    DryRunTransition(
                        strategy_id=resolved_strategy,
                        symbol=resolved_symbol,
                        from_state=current_state,
                        to_state=evening_to,
                        reason=str(leg_result.get("reason") or "evening_progression"),
                    )
                )
                current_state = evening_to

        self._memory.transitions.extend(transitions)
        self._memory.lifecycle_snapshots[resolved_strategy] = (
            persistence.get_ai_policy_lifecycle_state(resolved_strategy, dry_path) or {}
        )

        production_unmodified = (
            self._vault_fingerprint(self.production_db_path) == production_fingerprint
        )
        return DryRunProgressionResult(
            challenger_id=challenger_id,
            champion_id=challenger.champion_id,
            strategy_id=resolved_strategy,
            symbol=resolved_symbol,
            initial_state=initial_state,
            final_state=current_state,
            transitions=tuple(transitions),
            probation_checks=probation_checks,
            sovereign_checks=sovereign_checks,
            rollback_checks=rollback_checks,
            production_vault_unmodified=production_unmodified,
        )

    def memory_snapshot(self) -> dict[str, Any]:
        if self._memory is None:
            return {"initialized": False}
        return {
            "initialized": True,
            "vault_path": str(self._memory.vault_path),
            "transitions": [transition.__dict__ for transition in self._memory.transitions],
            "lifecycle_snapshots": self._memory.lifecycle_snapshots,
        }

    def close(self) -> None:
        self._memory = None
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def _bootstrap_dry_run_vault(self) -> None:
        self.close()
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="promotion_dry_run_",
            ignore_cleanup_errors=True,
        )
        dry_path = Path(self._temp_dir.name) / "dry_run_vault.db"
        self._clone_vault_subset(self.production_db_path, dry_path)
        self._memory = DryRunMemory(vault_path=dry_path)

    def _clone_vault_subset(self, source: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copy2(source, dest)
            return

        persistence.init_db()
        ensure_challenger_schema(dest)
        persistence.ensure_ai_policy_lifecycle_table(dest)
        persistence.ensure_regime_champions_table(dest)

    def _vault_fingerprint(self, db_path: Path) -> str:
        if not db_path.exists():
            return "missing"
        try:
            with sqlite3.connect(db_path) as conn:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                counts: list[str] = []
                for (table_name,) in tables:
                    if table_name.startswith("sqlite_"):
                        continue
                    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
                    counts.append(f"{table_name}:{int(row[0]) if row else 0}")
                return "|".join(counts)
        except sqlite3.Error:
            return "unreadable"
