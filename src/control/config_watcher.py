"""
Poll Supabase for strategy + reporting config; hot-reload without restart.

Falls back to YAML when Supabase is not configured.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import structlog

from src.config import AppConfig, StrategyConfig, load_config
from src.strategies.registry import MODULE_REGISTRY
from src.ingestor.assets import infer_asset_class
from src.control.supabase_client import get_supabase, run_with_supabase_retry

log = structlog.get_logger()

PORTFOLIO_NAME = "portfolio"



def _resolve_strategy_module(row: dict[str, Any]) -> str | None:
    module = str(row.get("module") or "").strip()
    name = str(row.get("name") or "").strip()
    symbol = str(row.get("symbol") or "").strip().lower()

    if module in MODULE_REGISTRY:
        return module
    if name in MODULE_REGISTRY:
        return name
    if module == "mean_reversion" and symbol:
        candidate = f"mean_reversion_{symbol}"
        if candidate in MODULE_REGISTRY:
            return candidate
    if name.startswith("mean_reversion_") and name in MODULE_REGISTRY:
        return name
    return None


class ConfigWatcher:
    """
    Cached config refreshed every ``poll_interval`` seconds.

    Merges Supabase strategy/reporting rows with secrets from .env.
    """

    def __init__(self, poll_interval: float = 30.0) -> None:
        self.poll_interval = poll_interval
        self._config: AppConfig | None = None
        self._last_fetch: float = 0.0
        self._strategy_uuids: dict[str, str] = {}
        self._strategy_versions: dict[str, int] = {}
        self._strategy_updated_at: dict[str, str] = {}
        self._portfolio_uuid: str | None = None
        self._reload_requested = False
        self._runtime_param_overrides: dict[str, dict[str, Any]] = {}
        self._runtime_override_meta: dict[str, dict[str, Any]] = {}
        self._remote_source_required = False
        self._config_fetch_degraded = False
        self._config_degraded_since: float | None = None

    @property
    def remote_source_required(self) -> bool:
        return self._remote_source_required

    @property
    def config_fetch_degraded(self) -> bool:
        return self._config_fetch_degraded

    def config_staleness_seconds(self) -> float:
        if self._config_degraded_since is None:
            return 0.0
        return max(0.0, time.monotonic() - self._config_degraded_since)

    @property
    def strategy_uuid(self) -> str | None:
        """Primary strategy UUID (first trading leg) — backward compat."""
        if not self._strategy_uuids:
            return None
        return next(iter(self._strategy_uuids.values()))

    @property
    def portfolio_uuid(self) -> str | None:
        return self._portfolio_uuid

    @property
    def strategy_uuids(self) -> dict[str, str]:
        """Map strategy name → Supabase UUID (trading legs only)."""
        return dict(self._strategy_uuids)

    def strategy_uuid_for(self, name: str) -> str | None:
        return self._strategy_uuids.get(name)

    def strategy_version_for(self, name: str) -> int | None:
        return self._strategy_versions.get(name)

    def strategy_updated_at_for(self, name: str) -> str | None:
        return self._strategy_updated_at.get(name)

    def request_reload(self) -> None:
        """Force refresh on next ``get_latest`` call."""
        self._reload_requested = True

    def set_runtime_params_override(
        self,
        strategy_id: str,
        params: dict[str, Any],
        *,
        source: str,
        regime: str | None = None,
    ) -> None:
        """Hot-swap params for a leg in memory without mutating YAML/Supabase."""
        self._runtime_param_overrides[strategy_id] = dict(params)
        self._runtime_override_meta[strategy_id] = {
            "source": source,
            "regime": regime,
            "updated_at_monotonic": time.monotonic(),
        }
        if self._config is not None:
            for strat in self._config.strategies:
                if strat.strategy_id == strategy_id:
                    strat.params = dict(params)
                    break
            if self._config.strategy.strategy_id == strategy_id:
                self._config.strategy.params = dict(params)
        log.warning(
            "runtime_param_override_set",
            strategy_id=strategy_id,
            source=source,
            regime=regime,
        )

    def clear_runtime_params_override(self, strategy_id: str) -> None:
        self._runtime_param_overrides.pop(strategy_id, None)
        self._runtime_override_meta.pop(strategy_id, None)

    def runtime_override_meta(self, strategy_id: str) -> dict[str, Any] | None:
        meta = self._runtime_override_meta.get(strategy_id)
        return dict(meta) if meta is not None else None

    def _apply_runtime_overrides(
        self, strategies: list[StrategyConfig]
    ) -> list[StrategyConfig]:
        if not self._runtime_param_overrides:
            return strategies
        updated: list[StrategyConfig] = []
        for strat in strategies:
            override = self._runtime_param_overrides.get(strat.strategy_id)
            if override is None:
                updated.append(strat)
            else:
                updated.append(replace(strat, params=dict(override)))
        return updated

    def get_latest(self) -> AppConfig:
        """Return cached config, refreshing from Supabase if stale."""
        now = time.monotonic()
        stale = (now - self._last_fetch) > self.poll_interval
        if self._config is None or stale or self._reload_requested:
            self._config = self._fetch()
            self._last_fetch = now
            self._reload_requested = False
        return self._config

    def _row_to_strategy(self, row: dict, template: StrategyConfig) -> StrategyConfig:
        symbol = str(row.get("symbol") or template.symbol)
        resolved_module = _resolve_strategy_module(row) or str(
            row.get("module") or template.module
        )
        return replace(
            template,
            strategy_id=str(row.get("name") or template.strategy_id),
            module=resolved_module,
            symbol=symbol,
            timeframe=row.get("timeframe", template.timeframe),
            poll_interval_seconds=row.get(
                "poll_interval_seconds", template.poll_interval_seconds
            ),
            params=row.get("params") or template.params,
            enabled=bool(row.get("enabled", False)),
            environment=row.get("environment", template.environment),
            asset_class=row.get("asset_class")
            or infer_asset_class(symbol),
        )

    def _template_for_row(
        self, base: AppConfig, row: dict[str, Any]
    ) -> StrategyConfig:
        symbol = str(row.get("symbol") or "").upper()
        for strat in base.strategies:
            if strat.symbol.upper() == symbol:
                return strat
        return base.strategy

    def _mark_config_fetch_success(self) -> None:
        self._config_fetch_degraded = False
        self._config_degraded_since = None

    def _mark_config_fetch_failure(self, error: Exception | str) -> None:
        self._config_fetch_degraded = True
        if self._config_degraded_since is None:
            self._config_degraded_since = time.monotonic()
        log.error("config_fetch_failed", error=str(error))

    def _fetch(self) -> AppConfig:
        base = load_config()
        client = get_supabase()
        if client is None:
            self._remote_source_required = False
            self._mark_config_fetch_success()
            if base.strategies:
                strategies = self._apply_runtime_overrides(base.strategies)
                return replace(base, strategy=strategies[0], strategies=strategies)
            return base

        self._remote_source_required = True
        try:
            resp = run_with_supabase_retry(
                lambda c: c.table("strategies").select("*").order("name").execute(),
                label="config_fetch_strategies",
            )
            if resp is None:
                raise RuntimeError("supabase strategies fetch failed")
            rows = resp.data or []
            if not rows:
                raise RuntimeError("supabase returned zero strategy rows")

            self._strategy_uuids = {}
            self._strategy_versions = {}
            self._strategy_updated_at = {}
            self._portfolio_uuid = None
            strategies: list[StrategyConfig] = []

            for row in rows:
                name = row.get("name", "")
                if name == PORTFOLIO_NAME or row.get("module") == "portfolio":
                    self._portfolio_uuid = row["id"]
                    continue
                resolved_module = _resolve_strategy_module(row)
                if resolved_module is None:
                    log.warning(
                        "unknown_strategy_module",
                        name=name,
                        module=row.get("module"),
                    )
                    continue
                row = {**row, "module": resolved_module}
                self._strategy_uuids[name] = row["id"]
                self._strategy_versions[name] = int(row.get("version_id") or 0)
                updated_raw = row.get("updated_at")
                if updated_raw is not None:
                    self._strategy_updated_at[name] = str(updated_raw)
                strategies.append(
                    self._row_to_strategy(row, self._template_for_row(base, row))
                )

            if not strategies:
                raise RuntimeError("supabase returned zero trading legs")

            reporting = {}
            rep_resp = run_with_supabase_retry(
                lambda c: c.table("reporting_settings").select("*").limit(1).execute(),
                label="config_fetch_reporting",
            )
            if rep_resp is not None and rep_resp.data:
                reporting = rep_resp.data[0]

            strategies = self._apply_runtime_overrides(strategies)
            primary = strategies[0]
            env = primary.environment or base.environment
            self._mark_config_fetch_success()
            return replace(
                base,
                environment=env,
                strategy=primary,
                strategies=strategies,
                email_from=reporting.get("email_from") or base.email_from,
                email_to=reporting.get("email_to") or base.email_to,
            )
        except Exception as exc:
            self._mark_config_fetch_failure(exc)
            if self._config is not None:
                log.warning(
                    "config_fetch_using_last_good_cache",
                    staleness_seconds=self.config_staleness_seconds(),
                )
                return self._config
            from src.persistence import db as persistence

            persistence.log_system_event(
                "CONFIG_FALLBACK_ALERT",
                str(exc),
                severity="critical",
                metadata={"fallback": "yaml_bootstrap", "remote_required": True},
            )
            if base.strategies:
                strategies = self._apply_runtime_overrides(base.strategies)
                primary = strategies[0]
                return replace(base, strategy=primary, strategies=strategies)
            return base

    async def poll_loop(self) -> None:
        """Background task to keep config cache warm."""
        while True:
            try:
                self.get_latest()
            except Exception as e:
                log.error("config_poll_failed", error=str(e))
            await asyncio.sleep(self.poll_interval)
