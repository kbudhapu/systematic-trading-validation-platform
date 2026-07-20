"""
Vault replication and configuration precedence engine.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from csv import DictReader, DictWriter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import structlog
import yaml

from src.config import CONFIG_DIR, DATA_DIR, ROOT, StrategyConfig, load_config
from src.config.parity_auditor import (
    ConfigurationParityAuditor,
    ConfigurationParityViolation,
    ParityDrift,
)
from src.config.research_validation import require_research_validation_row
from src.control.config_watcher import ConfigWatcher
from src.control.supabase_client import get_supabase
from src.persistence import db as persistence
from src.persistence.db import RESEARCH_VAULT_PATH

OFFBOX_MIRROR_DIR = Path(
    os.getenv("VAULT_OFFBOX_MIRROR_DIR", str(DATA_DIR / "backups" / "mirror"))
)
VAULT_BACKUP_STAGING_DIR = DATA_DIR / "backups" / "staging"
VAULT_INTEGRITY_OK = "ok"
PARAMETER_CHANGE_LOG_PATH = CONFIG_DIR / "parameter_change_log.csv"
_parameter_commit_lock = threading.RLock()
log = structlog.get_logger()


class ConfigLayer(str, Enum):
    YAML_BASE = "yaml_base"
    AI_POLICY_LIFECYCLE = "ai_policy_lifecycle"
    RUNTIME_OVERRIDE = "runtime_override"


LIFECYCLE_PARAM_KEYS = (
    "ai_policy_execution_state",
    "probation_started_at",
    "probation_clean_trading_days",
    "last_anomaly_session",
    "eviction_lockout_until",
)


@dataclass(frozen=True)
class VaultBackupDrillResult:
    source_path: str
    snapshot_path: str
    compressed_path: str
    checksum_sha256: str
    mirror_path: str | None
    verification_passed: bool
    package_bytes: int


@dataclass(frozen=True)
class ResolvedConfiguration:
    strategy_id: str
    params: dict[str, Any]
    precedence_trace: tuple[tuple[ConfigLayer, tuple[str, ...]], ...]


@dataclass(frozen=True)
class VaultIntegrityReport:
    exists: bool
    readable: bool
    integrity_ok: bool
    table_count: int
    champion_rows: int
    lifecycle_rows: int
    error: str | None


@dataclass(frozen=True)
class BootstrapPlaybookResult:
    action: str
    vault_integrity: VaultIntegrityReport
    auto_seed: dict[str, Any]
    clean_sweep_executed: bool
    trust_auto_seed: bool
    messages: tuple[str, ...]


@dataclass(frozen=True)
class ParameterCommitResult:
    committed_at: str
    strategy_id: str
    symbol: str
    target_path: str
    validation_target_path: str | None
    change_log_path: str
    operator_hash: str
    previous_params: dict[str, Any]
    new_params: dict[str, Any]


@dataclass
class VaultBackupManager:
    """Transactional vault snapshots with checksum verification and mirror drill."""

    vault_path: Path = RESEARCH_VAULT_PATH
    staging_dir: Path = VAULT_BACKUP_STAGING_DIR
    mirror_dir: Path = OFFBOX_MIRROR_DIR

    def execute_vault_backup_drill(self) -> VaultBackupDrillResult:
        return self.execute_sqlite_backup_drill(self.vault_path)

    def execute_sqlite_backup_drill(self, source_path: Path) -> VaultBackupDrillResult:
        if not source_path.exists():
            raise FileNotFoundError(f"sqlite database not found: {source_path}")

        label = source_path.stem
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_path = self.staging_dir / f"{label}_{stamp}.db"
        compressed_path = self.staging_dir / f"{label}_{stamp}.db.gz"
        manifest_path = self.staging_dir / f"{label}_{stamp}.manifest.json"

        self._transactional_snapshot(source_path, snapshot_path)
        checksum = self._sha256_file(snapshot_path)
        package_bytes = self._gzip_file(snapshot_path, compressed_path)
        verify_checksum = self._sha256_file(snapshot_path)
        verification_passed = checksum == verify_checksum and self._verify_sqlite(snapshot_path)

        mirror_path = self.mirror_dir / compressed_path.name
        shutil.copy2(compressed_path, mirror_path)
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source_path),
            "snapshot_path": str(snapshot_path),
            "compressed_path": str(compressed_path),
            "checksum_sha256": checksum,
            "package_bytes": package_bytes,
            "mirror_path": str(mirror_path),
            "verification_passed": verification_passed,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        shutil.copy2(manifest_path, self.mirror_dir / manifest_path.name)

        return VaultBackupDrillResult(
            source_path=str(source_path),
            snapshot_path=str(snapshot_path),
            compressed_path=str(compressed_path),
            checksum_sha256=checksum,
            mirror_path=str(mirror_path),
            verification_passed=verification_passed,
            package_bytes=package_bytes,
        )

    @staticmethod
    def _transactional_snapshot(source: Path, destination: Path) -> None:
        if destination.exists():
            destination.unlink()
        with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
            src.execute("PRAGMA wal_checkpoint(FULL)")
            src.backup(dst)
            dst.execute("PRAGMA integrity_check")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _gzip_file(source: Path, destination: Path) -> int:
        with source.open("rb") as src, gzip.open(destination, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        return destination.stat().st_size

    @staticmethod
    def _verify_sqlite(path: Path) -> bool:
        try:
            with sqlite3.connect(path) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
            return row is not None and str(row[0]).lower() == VAULT_INTEGRITY_OK
        except sqlite3.Error:
            return False


@dataclass(frozen=True)
class ConfigVersionLockResult:
    allowed: bool
    strategy_id: str
    local_version_id: int | None
    remote_version_id: int | None
    remote_updated_at: str | None
    conflict_resolved: bool
    reason: str


@dataclass(frozen=True)
class RemoteConfigWriteResult:
    applied: bool
    strategy_id: str
    version_id: int | None
    reason: str


@dataclass(frozen=True)
class PreflightConfigAlignmentResult:
    aligned: bool
    mismatches: tuple[str, ...]
    reason: str


def _normalize_updated_at_token(value: str) -> str:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class ConfigurationPrecedenceResolver:
    """
    Deterministic configuration merge:
    1. Runtime overrides (highest)
    2. ai_policy_lifecycle_state (DB)
    3. Local config/env.yaml + strategy yaml (lowest)
    """

    def __init__(self, db_path: Path = RESEARCH_VAULT_PATH) -> None:
        self.db_path = db_path
        self._cached_remote_versions: dict[str, int] = {}
        self._cached_remote_updated_at: dict[str, str] = {}
        self._config_fallback_active = False
        self._config_fallback_reason = ""

    def sync_remote_version_cache(self, config_watcher: ConfigWatcher) -> None:
        self._cached_remote_versions = dict(config_watcher._strategy_versions)
        self._cached_remote_updated_at = dict(config_watcher._strategy_updated_at)
        if not config_watcher.config_fetch_degraded:
            self._config_fallback_active = False
            self._config_fallback_reason = ""

    def blocks_policy_promotion(self, config_watcher: ConfigWatcher) -> bool:
        return (
            self._config_fallback_active
            or config_watcher.config_fetch_degraded
            or config_watcher.remote_source_required
            and config_watcher.config_staleness_seconds() > 0.0
        )

    def note_config_fallback(
        self,
        reason: str,
        *,
        config_watcher: ConfigWatcher,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._config_fallback_active = True
        self._config_fallback_reason = reason
        payload = {
            "reason": reason,
            "staleness_seconds": config_watcher.config_staleness_seconds(),
            "remote_source_required": config_watcher.remote_source_required,
            **(dict(metadata) if metadata else {}),
        }
        persistence.log_system_event(
            "CONFIG_FALLBACK_ALERT",
            json.dumps(payload, separators=(",", ":")),
            severity="critical",
            metadata=payload,
        )

    def _fetch_remote_strategy_row(
        self,
        strategy_id: str,
        *,
        config_watcher: ConfigWatcher | None = None,
    ) -> dict[str, Any] | None:
        client = get_supabase()
        if client is None:
            return None
        strategy_uuid = (
            config_watcher.strategy_uuid_for(strategy_id)
            if config_watcher is not None
            else None
        )
        try:
            if strategy_uuid:
                resp = (
                    client.table("strategies")
                    .select("id, name, version_id, updated_at, params")
                    .eq("id", strategy_uuid)
                    .limit(1)
                    .execute()
                )
            else:
                resp = (
                    client.table("strategies")
                    .select("id, name, version_id, updated_at, params")
                    .eq("name", strategy_id)
                    .limit(1)
                    .execute()
                )
            rows = resp.data or []
            return rows[0] if rows else None
        except Exception as exc:
            if config_watcher is not None and config_watcher.remote_source_required:
                self.note_config_fallback(
                    "remote_strategy_row_fetch_failed",
                    config_watcher=config_watcher,
                    metadata={"strategy_id": strategy_id, "error": str(exc)},
                )
            return None

    def verify_version_lock(
        self,
        strategy_id: str,
        *,
        config_watcher: ConfigWatcher,
        on_conflict: Any | None = None,
    ) -> ConfigVersionLockResult:
        local_version = config_watcher.strategy_version_for(strategy_id)
        remote_row = self._fetch_remote_strategy_row(strategy_id, config_watcher=config_watcher)
        if remote_row is None:
            if config_watcher.remote_source_required:
                self.note_config_fallback(
                    "remote_unavailable",
                    config_watcher=config_watcher,
                    metadata={"strategy_id": strategy_id},
                )
                return ConfigVersionLockResult(
                    allowed=False,
                    strategy_id=strategy_id,
                    local_version_id=local_version,
                    remote_version_id=None,
                    remote_updated_at=None,
                    conflict_resolved=False,
                    reason="remote_unreachable",
                )
            return ConfigVersionLockResult(
                allowed=True,
                strategy_id=strategy_id,
                local_version_id=local_version,
                remote_version_id=None,
                remote_updated_at=None,
                conflict_resolved=False,
                reason="remote_unavailable",
            )

        remote_version = int(remote_row.get("version_id") or 0)
        remote_updated_at = str(remote_row.get("updated_at") or "")
        self._cached_remote_versions[strategy_id] = remote_version
        if remote_updated_at:
            self._cached_remote_updated_at[strategy_id] = remote_updated_at

        if local_version is None:
            return ConfigVersionLockResult(
                allowed=True,
                strategy_id=strategy_id,
                local_version_id=None,
                remote_version_id=remote_version,
                remote_updated_at=remote_updated_at or None,
                conflict_resolved=False,
                reason="no_local_version_cache",
            )

        if int(local_version) == remote_version:
            local_updated = config_watcher.strategy_updated_at_for(strategy_id)
            if (
                local_updated
                and remote_updated_at
                and _normalize_updated_at_token(local_updated)
                != _normalize_updated_at_token(remote_updated_at)
            ):
                config_watcher.request_reload()
                config_watcher.get_latest()
                persistence.log_system_event(
                    "CONFIG_VERSION_CONFLICT_RESOLVED",
                    json.dumps(
                        {
                            "strategy_id": strategy_id,
                            "local_version_id": int(local_version),
                            "remote_version_id": remote_version,
                            "local_updated_at": local_updated,
                            "remote_updated_at": remote_updated_at,
                        },
                        separators=(",", ":"),
                    ),
                    severity="warning",
                )
                if on_conflict is not None:
                    on_conflict()
                return ConfigVersionLockResult(
                    allowed=False,
                    strategy_id=strategy_id,
                    local_version_id=int(local_version),
                    remote_version_id=remote_version,
                    remote_updated_at=remote_updated_at or None,
                    conflict_resolved=True,
                    reason="updated_at_mismatch",
                )
            return ConfigVersionLockResult(
                allowed=True,
                strategy_id=strategy_id,
                local_version_id=int(local_version),
                remote_version_id=remote_version,
                remote_updated_at=remote_updated_at or None,
                conflict_resolved=False,
                reason="version_match",
            )

        config_watcher.request_reload()
        config_watcher.get_latest()
        persistence.log_system_event(
            "CONFIG_VERSION_CONFLICT_RESOLVED",
            json.dumps(
                {
                    "strategy_id": strategy_id,
                    "local_version_id": int(local_version),
                    "remote_version_id": remote_version,
                    "remote_updated_at": remote_updated_at,
                },
                separators=(",", ":"),
            ),
            severity="warning",
        )
        if on_conflict is not None:
            on_conflict()
        return ConfigVersionLockResult(
            allowed=False,
            strategy_id=strategy_id,
            local_version_id=int(local_version),
            remote_version_id=remote_version,
            remote_updated_at=remote_updated_at or None,
            conflict_resolved=True,
            reason="version_mismatch",
        )

    def _strategy_version_tokens_aligned(
        self,
        strategy_id: str,
        *,
        config_watcher: ConfigWatcher,
        remote_row: dict[str, Any],
    ) -> bool:
        local_version = config_watcher.strategy_version_for(strategy_id)
        local_updated = config_watcher.strategy_updated_at_for(strategy_id)
        remote_version = int(remote_row.get("version_id") or 0)
        remote_updated = str(remote_row.get("updated_at") or "")
        if local_version is not None and int(local_version) != remote_version:
            return False
        if (
            local_updated
            and remote_updated
            and _normalize_updated_at_token(local_updated)
            != _normalize_updated_at_token(remote_updated)
        ):
            return False
        return True

    def _find_version_mismatches(
        self,
        config_watcher: ConfigWatcher,
        strategy_ids: tuple[str, ...],
    ) -> list[str]:
        mismatches: list[str] = []
        for strategy_id in strategy_ids:
            remote_row = self._fetch_remote_strategy_row(
                strategy_id,
                config_watcher=config_watcher,
            )
            if remote_row is None:
                if config_watcher.remote_source_required:
                    mismatches.append(strategy_id)
                continue
            remote_version = int(remote_row.get("version_id") or 0)
            remote_updated_at = str(remote_row.get("updated_at") or "")
            if remote_updated_at:
                self._cached_remote_updated_at[strategy_id] = remote_updated_at
            self._cached_remote_versions[strategy_id] = remote_version
            if not self._strategy_version_tokens_aligned(
                strategy_id,
                config_watcher=config_watcher,
                remote_row=remote_row,
            ):
                mismatches.append(strategy_id)
        return mismatches

    def enforce_preflight_config_alignment(
        self,
        config_watcher: ConfigWatcher,
        *,
        strategy_ids: tuple[str, ...] | None = None,
    ) -> PreflightConfigAlignmentResult:
        targets = strategy_ids or tuple(config_watcher.strategy_uuids.keys())
        if not targets:
            return PreflightConfigAlignmentResult(
                aligned=True,
                mismatches=(),
                reason="no_remote_strategies",
            )

        mismatches = self._find_version_mismatches(config_watcher, targets)
        if not mismatches:
            return PreflightConfigAlignmentResult(
                aligned=True,
                mismatches=(),
                reason="aligned",
            )

        if config_watcher.config_fetch_degraded:
            self.note_config_fallback(
                "preflight_alignment_blocked_by_degraded_fetch",
                config_watcher=config_watcher,
                metadata={"mismatches": mismatches},
            )
            return PreflightConfigAlignmentResult(
                aligned=False,
                mismatches=tuple(mismatches),
                reason="config_fetch_degraded",
            )

        config_watcher.request_reload()
        config_watcher.get_latest()
        self.sync_remote_version_cache(config_watcher)
        persistence.log_system_event(
            "CONFIG_VERSION_CONFLICT_RESOLVED",
            json.dumps(
                {
                    "scope": "preflight_cycle",
                    "mismatched_strategies": mismatches,
                },
                separators=(",", ":"),
            ),
            severity="warning",
        )

        remaining = self._find_version_mismatches(config_watcher, targets)
        if remaining:
            return PreflightConfigAlignmentResult(
                aligned=False,
                mismatches=tuple(remaining),
                reason="still_misaligned",
            )
        return PreflightConfigAlignmentResult(
            aligned=True,
            mismatches=(),
            reason="realigned_after_reload",
        )

    def write_remote_strategy_update(
        self,
        strategy_id: str,
        updates: Mapping[str, Any],
        *,
        config_watcher: ConfigWatcher,
        expected_version_id: int | None = None,
    ) -> RemoteConfigWriteResult:
        lock = self.verify_version_lock(strategy_id, config_watcher=config_watcher)
        if not lock.allowed and lock.conflict_resolved:
            return RemoteConfigWriteResult(
                applied=False,
                strategy_id=strategy_id,
                version_id=lock.remote_version_id,
                reason="config_version_conflict",
            )

        client = get_supabase()
        if client is None:
            return RemoteConfigWriteResult(
                applied=False,
                strategy_id=strategy_id,
                version_id=None,
                reason="remote_unavailable",
            )

        strategy_uuid = config_watcher.strategy_uuid_for(strategy_id)
        if strategy_uuid is None:
            return RemoteConfigWriteResult(
                applied=False,
                strategy_id=strategy_id,
                version_id=None,
                reason="strategy_uuid_missing",
            )

        local_version = (
            expected_version_id
            if expected_version_id is not None
            else config_watcher.strategy_version_for(strategy_id)
        )
        if local_version is None:
            local_version = lock.remote_version_id or 0

        payload = dict(updates)
        if "version_id" in payload:
            del payload["version_id"]
        if "updated_at" in payload:
            del payload["updated_at"]

        try:
            query = client.table("strategies").update(payload).eq("id", strategy_uuid)
            query = query.eq("version_id", int(local_version))
            resp = query.execute()
            rows = resp.data or []
            if not rows:
                resolved = self.verify_version_lock(strategy_id, config_watcher=config_watcher)
                return RemoteConfigWriteResult(
                    applied=False,
                    strategy_id=strategy_id,
                    version_id=resolved.remote_version_id,
                    reason="optimistic_write_rejected",
                )
            new_version = int(rows[0].get("version_id") or int(local_version) + 1)
            config_watcher._strategy_versions[strategy_id] = new_version
            updated_raw = rows[0].get("updated_at")
            if updated_raw is not None:
                config_watcher._strategy_updated_at[strategy_id] = str(updated_raw)
            return RemoteConfigWriteResult(
                applied=True,
                strategy_id=strategy_id,
                version_id=new_version,
                reason="applied",
            )
        except Exception as exc:
            return RemoteConfigWriteResult(
                applied=False,
                strategy_id=strategy_id,
                version_id=None,
                reason=str(exc),
            )

    def resolve_strategy_params(
        self,
        strategy_id: str,
        *,
        yaml_params: Mapping[str, Any] | None = None,
        config_watcher: ConfigWatcher | None = None,
        symbol: str | None = None,
    ) -> ResolvedConfiguration:
        trace: list[tuple[ConfigLayer, tuple[str, ...]]] = []
        merged: dict[str, Any] = {}

        base = dict(yaml_params) if yaml_params is not None else self._load_yaml_params(strategy_id)
        merged.update(base)
        trace.append((ConfigLayer.YAML_BASE, tuple(sorted(base.keys()))))

        lifecycle = persistence.get_ai_policy_lifecycle_state(strategy_id, self.db_path)
        lifecycle_keys: list[str] = []
        if lifecycle is not None:
            for key in LIFECYCLE_PARAM_KEYS:
                if lifecycle.get(key) is not None:
                    merged[key] = lifecycle[key]
                    lifecycle_keys.append(key)
            if symbol is None and lifecycle.get("symbol"):
                merged["symbol"] = lifecycle["symbol"]
        trace.append((ConfigLayer.AI_POLICY_LIFECYCLE, tuple(lifecycle_keys)))

        override_keys: list[str] = []
        if config_watcher is not None:
            override = config_watcher._runtime_param_overrides.get(strategy_id)
            if override:
                merged.update(override)
                override_keys = sorted(override.keys())
        trace.append((ConfigLayer.RUNTIME_OVERRIDE, tuple(override_keys)))

        return ResolvedConfiguration(
            strategy_id=strategy_id,
            params=merged,
            precedence_trace=tuple(trace),
        )

    def resolve_app_config(
        self,
        config_watcher: ConfigWatcher | None = None,
    ) -> tuple[Any, list[ResolvedConfiguration]]:
        app_config = load_config()
        if config_watcher is not None:
            app_config = config_watcher.get_latest()

        resolved_legs: list[ResolvedConfiguration] = []
        updated_strategies: list[StrategyConfig] = []
        for strat in app_config.strategies:
            resolved = self.resolve_strategy_params(
                strat.strategy_id,
                yaml_params=strat.params,
                config_watcher=config_watcher,
                symbol=strat.symbol,
            )
            resolved_legs.append(resolved)
            updated_strategies.append(
                StrategyConfig(
                    strategy_id=strat.strategy_id,
                    module=strat.module,
                    symbol=strat.symbol,
                    timeframe=strat.timeframe,
                    poll_interval_seconds=strat.poll_interval_seconds,
                    params=dict(resolved.params),
                    enabled=strat.enabled,
                    environment=strat.environment,
                    asset_class=strat.asset_class,
                )
            )

        from dataclasses import replace

        primary = updated_strategies[0] if updated_strategies else app_config.strategy
        merged_app = replace(
            app_config,
            strategy=primary,
            strategies=updated_strategies,
        )
        return merged_app, resolved_legs

    def _load_yaml_params(self, strategy_id: str) -> dict[str, Any]:
        strategy_dir = CONFIG_DIR / "strategies"
        if not strategy_dir.exists():
            return {}
        for path in sorted(strategy_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if str(data.get("strategy_id")) == strategy_id:
                params = data.get("params") or {}
                return dict(params)
        return {}


def _read_yaml_payload(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.stem}_",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _normalize_candidate_payload(
    staged_payload: dict[str, Any],
    active_payload: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(active_payload)
    merged["module"] = staged_payload.get("module", active_payload.get("module"))
    merged["symbol"] = staged_payload.get("symbol", active_payload.get("symbol"))
    merged["timeframe"] = staged_payload.get("timeframe", active_payload.get("timeframe"))
    merged["poll_interval_seconds"] = staged_payload.get(
        "poll_interval_seconds",
        active_payload.get("poll_interval_seconds", 900),
    )
    merged["enabled"] = bool(staged_payload.get("enabled", active_payload.get("enabled", True)))
    merged["environment"] = staged_payload.get(
        "environment",
        active_payload.get("environment", "paper"),
    )
    merged["params"] = dict(staged_payload.get("params") or {})
    merged["strategy_id"] = str(active_payload.get("strategy_id") or merged.get("strategy_id") or "")
    return merged


def _assert_candidate_invariants(
    payload: dict[str, Any],
    *,
    research_path: Path | None = None,
) -> None:
    symbol = str(payload.get("symbol") or "").upper()
    strategy_id = str(payload.get("strategy_id") or "")
    params = dict(payload.get("params") or {})
    record = require_research_validation_row(symbol, path=research_path)
    drifts: list[ParityDrift] = []
    expected_regime = bool(record.constraints.get("regime_filter", False))
    if params.get("regime_filter") is not expected_regime:
        drifts.append(
            ParityDrift(
                missing_gate="regime_filter",
                ticker=symbol,
                strategy_id=strategy_id,
                expected=expected_regime,
                found=params.get("regime_filter"),
            )
        )
    borrow_drag = params.get("borrow_drag_coefficient", params.get("short_borrow_fee_annual"))
    min_borrow = float(record.constraints.get("min_borrow_drag_coefficient", 0.005))
    if borrow_drag is None:
        drifts.append(
            ParityDrift(
                missing_gate="borrow_drag_coefficient",
                ticker=symbol,
                strategy_id=strategy_id,
                expected=f">={min_borrow}",
                found=None,
            )
        )
    elif float(borrow_drag) < min_borrow:
        drifts.append(
            ParityDrift(
                missing_gate="borrow_drag_coefficient",
                ticker=symbol,
                strategy_id=strategy_id,
                expected=min_borrow,
                found=float(borrow_drag),
            )
        )
    if drifts:
        raise ConfigurationParityViolation(drifts)


def _write_change_log_row(
    path: Path,
    *,
    committed_at: str,
    symbol: str,
    strategy_id: str,
    operator_hash: str,
    previous_params: dict[str, Any],
    new_params: dict[str, Any],
    validation_target_path: str | None = None,
) -> None:
    rows: list[dict[str, str]] = []
    fieldnames = [
        "timestamp_utc",
        "symbol",
        "strategy_id",
        "operator_hash",
        "validation_target_path",
        "previous_params_json",
        "new_params_json",
    ]
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            rows.extend(DictReader(handle))
    rows.append(
        {
            "timestamp_utc": committed_at,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "operator_hash": operator_hash,
            "validation_target_path": validation_target_path or "",
            "previous_params_json": json.dumps(previous_params, sort_keys=True, separators=(",", ":")),
            "new_params_json": json.dumps(new_params, sort_keys=True, separators=(",", ":")),
        }
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f"{path.stem}_",
        suffix=".tmp",
        delete=False,
    ) as handle:
        writer = DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def commit_validated_strategy_parameters(
    *,
    staged_strategy_path: Path,
    target_strategy_path: Path,
    operator_hash: str,
    env_path: Path | None = None,
    strategies_dir: Path | None = None,
    research_path: Path | None = None,
    staged_research_path: Path | None = None,
    target_research_path: Path | None = None,
    calibration_path: Path | None = None,
    change_log_path: Path | None = None,
    allow_schema_update: bool = False,
) -> ParameterCommitResult:
    staged_path = Path(staged_strategy_path)
    target_path = Path(target_strategy_path)
    env_yaml = env_path or (CONFIG_DIR / "env.yaml")
    strategy_root = strategies_dir or (CONFIG_DIR / "strategies")
    ledger_path = change_log_path or PARAMETER_CHANGE_LOG_PATH
    audit_research_path = staged_research_path if allow_schema_update and staged_research_path is not None else research_path
    live_research_path = target_research_path or research_path
    if not operator_hash.strip():
        raise ValueError("operator_hash is required")
    if not staged_path.is_file():
        raise FileNotFoundError(f"staged strategy file not found: {staged_path}")
    if not target_path.is_file():
        raise FileNotFoundError(f"target strategy file not found: {target_path}")
    if allow_schema_update and staged_research_path is None:
        raise ValueError("staged_research_path is required when allow_schema_update=True")
    if allow_schema_update and live_research_path is None:
        raise ValueError("target_research_path or research_path is required when allow_schema_update=True")
    with _parameter_commit_lock:
        active_payload = _read_yaml_payload(target_path)
        staged_payload = _read_yaml_payload(staged_path)
        candidate_payload = _normalize_candidate_payload(staged_payload, active_payload)
        staged_research_payload = None
        live_research_payload = None
        if allow_schema_update:
            staged_research_payload = json.loads(Path(staged_research_path).read_text(encoding="utf-8"))
            live_research_payload = json.loads(Path(live_research_path).read_text(encoding="utf-8"))
        try:
            _assert_candidate_invariants(candidate_payload, research_path=audit_research_path)
            with tempfile.TemporaryDirectory(prefix="param_commit_sandbox_") as sandbox_dir:
                sandbox_root = Path(sandbox_dir)
                sandbox_env = sandbox_root / "env.yaml"
                sandbox_env.write_text(env_yaml.read_text(encoding="utf-8"), encoding="utf-8")
                sandbox_strategies = sandbox_root / "strategies"
                sandbox_strategies.mkdir(parents=True, exist_ok=True)
                for source in sorted(Path(strategy_root).glob("*.yaml")):
                    if source.resolve() in {staged_path.resolve(), target_path.resolve()}:
                        continue
                    shutil.copy2(source, sandbox_strategies / source.name)
                _atomic_write_text(
                    sandbox_strategies / target_path.name,
                    yaml.safe_dump(candidate_payload, sort_keys=False),
                )
                candidate_config = load_config(
                    env_path=sandbox_env,
                    strategies_dir=sandbox_strategies,
                )
                auditor = ConfigurationParityAuditor(
                    env_path=sandbox_env,
                    strategies_dir=sandbox_strategies,
                    research_path=audit_research_path,
                    calibration_path=calibration_path,
                )
                auditor.audit_app_config(candidate_config)
        except ConfigurationParityViolation:
            log.warning(
                "config_updater: COMMIT_REJECTED",
                staged_strategy_path=str(staged_path),
                target_strategy_path=str(target_path),
                strategy_id=str(candidate_payload.get("strategy_id") or ""),
                symbol=str(candidate_payload.get("symbol") or "").upper(),
            )
            raise
        previous_params = dict(active_payload.get("params") or {})
        new_params = dict(candidate_payload.get("params") or {})
        previous_strategy_text = target_path.read_text(encoding="utf-8")
        previous_research_text = None
        if allow_schema_update:
            previous_research_text = Path(live_research_path).read_text(encoding="utf-8")
        try:
            _atomic_write_text(
                target_path,
                yaml.safe_dump(candidate_payload, sort_keys=False),
            )
            if allow_schema_update and staged_research_payload is not None:
                _atomic_write_text(
                    Path(live_research_path),
                    json.dumps(staged_research_payload, indent=2),
                )
        except Exception:
            _atomic_write_text(target_path, previous_strategy_text)
            if allow_schema_update and previous_research_text is not None:
                _atomic_write_text(Path(live_research_path), previous_research_text)
            raise
        committed_at = datetime.now(timezone.utc).isoformat()
        _write_change_log_row(
            ledger_path,
            committed_at=committed_at,
            symbol=str(candidate_payload.get("symbol") or "").upper(),
            strategy_id=str(candidate_payload.get("strategy_id") or ""),
            operator_hash=operator_hash.strip(),
            previous_params=previous_params,
            new_params=new_params,
            validation_target_path=str(live_research_path) if allow_schema_update and live_research_path is not None else None,
        )
        result = ParameterCommitResult(
            committed_at=committed_at,
            strategy_id=str(candidate_payload.get("strategy_id") or ""),
            symbol=str(candidate_payload.get("symbol") or "").upper(),
            target_path=str(target_path),
            validation_target_path=(
                str(live_research_path) if allow_schema_update and live_research_path is not None else None
            ),
            change_log_path=str(ledger_path),
            operator_hash=operator_hash.strip(),
            previous_params=previous_params,
            new_params=new_params,
        )
        persistence.log_system_event(
            "CONFIG_PARAMETERS_COMMITTED",
            json.dumps(
                {
                    "symbol": result.symbol,
                    "strategy_id": result.strategy_id,
                    "target_path": result.target_path,
                    "validation_target_path": result.validation_target_path,
                    "change_log_path": result.change_log_path,
                    "operator_hash": result.operator_hash,
                    "allow_schema_update": allow_schema_update,
                },
                sort_keys=True,
            ),
            severity="info",
        )
        log.info(
            "config_updater: PARAMETERS_COMMITTED",
            strategy_id=result.strategy_id,
            symbol=result.symbol,
            target_path=result.target_path,
            validation_target_path=result.validation_target_path,
            change_log_path=result.change_log_path,
            operator_hash=result.operator_hash,
            allow_schema_update=allow_schema_update,
        )
        return result


def assess_vault_integrity(vault_path: Path = RESEARCH_VAULT_PATH) -> VaultIntegrityReport:
    if not vault_path.exists():
        return VaultIntegrityReport(
            exists=False,
            readable=False,
            integrity_ok=False,
            table_count=0,
            champion_rows=0,
            lifecycle_rows=0,
            error="vault_missing",
        )
    try:
        with sqlite3.connect(vault_path) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = integrity is not None and str(integrity[0]).lower() == VAULT_INTEGRITY_OK
            table_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            )
            champion_rows = _safe_count(conn, "regime_champions")
            lifecycle_rows = _safe_count(conn, "ai_policy_lifecycle_state")
        return VaultIntegrityReport(
            exists=True,
            readable=True,
            integrity_ok=integrity_ok,
            table_count=table_count,
            champion_rows=champion_rows,
            lifecycle_rows=lifecycle_rows,
            error=None if integrity_ok else "integrity_check_failed",
        )
    except sqlite3.Error as exc:
        return VaultIntegrityReport(
            exists=True,
            readable=False,
            integrity_ok=False,
            table_count=0,
            champion_rows=0,
            lifecycle_rows=0,
            error=str(exc),
        )


def run_human_bootstrap_playbook(
    *,
    vault_path: Path = RESEARCH_VAULT_PATH,
    force_clean_sweep: bool = False,
    trust_auto_seed: bool = True,
    interactive: bool = True,
) -> BootstrapPlaybookResult:
    """
    Bootstrap empty or corrupted vaults.

    Parameters:
    - force_clean_sweep: delete vault and rerun full tuner initialization.
    - trust_auto_seed: when vault is empty but integrity passes, seed baseline champions.
    - interactive: prompt operator before destructive actions.
    """
    messages: list[str] = []
    persistence.ensure_regime_champions_table(vault_path)
    persistence.ensure_ai_policy_lifecycle_table(vault_path)
    integrity = assess_vault_integrity(vault_path)

    if not integrity.exists:
        messages.append("Vault file missing — creating schema and evaluating seed path.")
        persistence.ensure_regime_champions_table(vault_path)
        integrity = assess_vault_integrity(vault_path)

    clean_sweep = force_clean_sweep
    if integrity.exists and not integrity.integrity_ok:
        messages.append("Vault integrity_check failed.")
        if interactive and not force_clean_sweep:
            answer = input(
                "Vault corrupted. Force clean sweep? [y/N]: "
            ).strip().lower()
            clean_sweep = answer in {"y", "yes"}
        elif not force_clean_sweep:
            clean_sweep = False
            messages.append(
                "Non-interactive mode with corrupted vault — set force_clean_sweep=True."
            )

    auto_seed: dict[str, Any] = {"seeded": False, "symbols": []}
    action = "inspect_only"

    if clean_sweep:
        if interactive and not force_clean_sweep:
            confirm = input(
                "This deletes the research vault. Continue? [y/N]: "
            ).strip().lower()
            if confirm not in {"y", "yes"}:
                return BootstrapPlaybookResult(
                    action="aborted",
                    vault_integrity=integrity,
                    auto_seed=auto_seed,
                    clean_sweep_executed=False,
                    trust_auto_seed=trust_auto_seed,
                    messages=tuple(messages + ["Operator aborted clean sweep."]),
                )
        if vault_path.exists():
            backup = vault_path.with_suffix(".db.corrupt_backup")
            shutil.move(vault_path, backup)
            messages.append(f"Moved corrupted vault to {backup}")
        persistence.ensure_regime_champions_table(vault_path)
        action = "clean_sweep"
        messages.append("Executing full clean sweep tuner initialization.")
        _run_clean_sweep_tuner()
        auto_seed = persistence.auto_seed_baseline_champions_if_needed(vault_path)
        action = "clean_sweep_with_seed"
    elif trust_auto_seed and integrity.integrity_ok and integrity.champion_rows == 0:
        if interactive and not force_clean_sweep:
            answer = input(
                "Vault empty. Trust automated baseline champion seed? [Y/n]: "
            ).strip().lower()
            if answer in {"n", "no"}:
                messages.append("Operator declined automated seed.")
                return BootstrapPlaybookResult(
                    action="seed_declined",
                    vault_integrity=integrity,
                    auto_seed=auto_seed,
                    clean_sweep_executed=False,
                    trust_auto_seed=False,
                    messages=tuple(messages),
                )
        auto_seed = persistence.auto_seed_baseline_champions_if_needed(vault_path)
        action = "auto_seed"
        messages.append(
            f"Automated seed applied symbols={auto_seed.get('symbols', [])}"
        )
    else:
        messages.append(
            "Vault present with champions or auto-seed disabled — no bootstrap mutation."
        )

    integrity = assess_vault_integrity(vault_path)
    return BootstrapPlaybookResult(
        action=action,
        vault_integrity=integrity,
        auto_seed=auto_seed,
        clean_sweep_executed=clean_sweep,
        trust_auto_seed=trust_auto_seed,
        messages=tuple(messages),
    )


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _run_clean_sweep_tuner() -> None:
    import subprocess
    import sys

    script = ROOT / "scripts" / "adaptive_parameter_tuner.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"clean sweep tuner failed exit={completed.returncode}: {completed.stderr[-500:]}"
        )
