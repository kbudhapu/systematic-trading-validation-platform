from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

from src.config import AppConfig, CONFIG_DIR, StrategyConfig
from src.config.research_validation import (
    DEFAULT_MIN_BORROW_DRAG_COEFFICIENT,
    RESEARCH_VALIDATION_PATH,
    ResearchValidationRecord,
    load_research_validation_index,
)
from src.engine.slippage_calibration import (
    DEFAULT_CALIBRATION_PATH,
    SESSION_SLIPPAGE_MULTIPLIERS,
    load_asymmetric_slippage_multipliers,
)

log = structlog.get_logger()

PRODUCTION_ENVIRONMENTS = frozenset({"paper", "live"})
CHAMPION_SLIPPAGE_PCT = 0.0005
PARAM_FLOAT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ParityDrift:
    missing_gate: str
    ticker: str | None = None
    strategy_id: str | None = None
    expected: Any = None
    found: Any = None


class ConfigurationParityViolation(Exception):
    def __init__(self, drifts: list[ParityDrift]) -> None:
        self.drifts = list(drifts)
        preview = "; ".join(
            f"{d.missing_gate} ticker={d.ticker} expected={d.expected!r} found={d.found!r}"
            for d in self.drifts[:5]
        )
        super().__init__(f"configuration parity violation: {preview}")


class ConfigurationParityAuditor:
    def __init__(
        self,
        *,
        env_path: Path | None = None,
        strategies_dir: Path | None = None,
        research_path: Path | None = None,
        calibration_path: Path | None = None,
    ) -> None:
        self._env_path = env_path or (CONFIG_DIR / "env.yaml")
        self._strategies_dir = strategies_dir or (CONFIG_DIR / "strategies")
        self._research_path = research_path or RESEARCH_VALIDATION_PATH
        self._calibration_path = calibration_path or DEFAULT_CALIBRATION_PATH
        self._env_data = self._load_yaml(self._env_path)
        self._research_validations = load_research_validation_index(self._research_path)

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        with path.open(encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        return payload if isinstance(payload, dict) else {}

    def audit_and_commit(self, config: AppConfig) -> AppConfig:
        self.audit_app_config(config)
        return config

    def audit_app_config(self, config: AppConfig) -> None:
        drifts = self._collect_drifts(config)
        if drifts:
            for drift in drifts:
                log.warning(
                    "config_parity_audit: CRITICAL_DRIFT",
                    missing_gate=drift.missing_gate,
                    ticker=drift.ticker,
                    strategy_id=drift.strategy_id,
                    expected=drift.expected,
                    found=drift.found,
                )
            raise ConfigurationParityViolation(drifts)
        log.info(
            "config_parity_audit: SUCCESS",
            environment=config.environment,
            validated_legs=[
                strategy.strategy_id
                for strategy in config.strategies
                if strategy.enabled
            ],
        )

    def _collect_drifts(self, config: AppConfig) -> list[ParityDrift]:
        drifts: list[ParityDrift] = []
        drifts.extend(self._audit_slippage_multipliers(config))
        for strategy in config.strategies:
            if not strategy.enabled:
                continue
            symbol = strategy.symbol.upper()
            record = self._research_validations.get(symbol)
            if record is None:
                drifts.append(
                    ParityDrift(
                        missing_gate="research_validation_artifact",
                        ticker=symbol,
                        strategy_id=strategy.strategy_id,
                        expected="validation_row",
                        found=None,
                    )
                )
                continue
            drifts.extend(self._audit_runtime_structure(strategy, config, record))
            drifts.extend(self._audit_loop_settings(strategy, record))
        return drifts

    @staticmethod
    def _is_production_capital(environment: str) -> bool:
        return str(environment).strip().lower() in PRODUCTION_ENVIRONMENTS

    def _audit_slippage_multipliers(self, config: AppConfig) -> list[ParityDrift]:
        if not self._is_production_capital(config.environment):
            return []
        drifts: list[ParityDrift] = []
        env_slippage = float(
            self._env_data.get("backtest", {}).get("slippage_pct", config.backtest.slippage_pct)
        )
        if abs(env_slippage - CHAMPION_SLIPPAGE_PCT) > PARAM_FLOAT_TOLERANCE:
            drifts.append(
                ParityDrift(
                    missing_gate="slippage_multipliers",
                    expected=CHAMPION_SLIPPAGE_PCT,
                    found=env_slippage,
                )
            )
        if abs(float(config.backtest.slippage_pct) - env_slippage) > PARAM_FLOAT_TOLERANCE:
            drifts.append(
                ParityDrift(
                    missing_gate="slippage_multipliers",
                    expected=env_slippage,
                    found=config.backtest.slippage_pct,
                )
            )
        grid = load_asymmetric_slippage_multipliers(self._calibration_path)
        if not grid:
            drifts.append(
                ParityDrift(
                    missing_gate="slippage_multipliers",
                    expected="non_empty_session_grid",
                    found=None,
                )
            )
            return drifts
        for session in SESSION_SLIPPAGE_MULTIPLIERS:
            session_grid = grid.get(session)
            if not isinstance(session_grid, dict) or not session_grid:
                drifts.append(
                    ParityDrift(
                        missing_gate="slippage_multipliers",
                        expected=f"session:{session}",
                        found=session_grid,
                    )
                )
                continue
            for direction, multiplier in session_grid.items():
                if float(multiplier) <= 0.0:
                    drifts.append(
                        ParityDrift(
                            missing_gate="slippage_multipliers",
                            expected=f"positive_multiplier:{session}:{direction}",
                            found=multiplier,
                        )
                    )
        return drifts

    def _audit_runtime_structure(
        self,
        strategy: StrategyConfig,
        config: AppConfig,
        record: ResearchValidationRecord,
    ) -> list[ParityDrift]:
        drifts: list[ParityDrift] = []
        symbol = strategy.symbol.upper()
        params = strategy.params or {}
        constraints = record.constraints
        expected_regime_filter = bool(constraints.get("regime_filter", False))
        regime_filter = params.get("regime_filter")
        if regime_filter is not expected_regime_filter:
            drifts.append(
                ParityDrift(
                    missing_gate="regime_filter",
                    ticker=symbol,
                    strategy_id=strategy.strategy_id,
                    expected=expected_regime_filter,
                    found=regime_filter,
                )
            )
        if params.get("regime_filter_bypass") is True or params.get("disable_regime_filter") is True:
            drifts.append(
                ParityDrift(
                    missing_gate="regime_filter",
                    ticker=symbol,
                    strategy_id=strategy.strategy_id,
                    expected=False,
                    found=True,
                )
            )
        allows_short = bool(constraints.get("allow_short", False))
        min_borrow = float(
            constraints.get("min_borrow_drag_coefficient", DEFAULT_MIN_BORROW_DRAG_COEFFICIENT)
        )
        if allows_short and not config.risk.long_only:
            borrow_drag = self._resolve_borrow_drag_coefficient(params)
            if borrow_drag is None or borrow_drag <= 0.0:
                drifts.append(
                    ParityDrift(
                        missing_gate="borrow_drag_coefficient",
                        ticker=symbol,
                        strategy_id=strategy.strategy_id,
                        expected=f">={min_borrow}",
                        found=borrow_drag,
                    )
                )
            elif borrow_drag + PARAM_FLOAT_TOLERANCE < min_borrow:
                drifts.append(
                    ParityDrift(
                        missing_gate="borrow_drag_coefficient",
                        ticker=symbol,
                        strategy_id=strategy.strategy_id,
                        expected=min_borrow,
                        found=borrow_drag,
                    )
                )
        poll_interval = int(strategy.poll_interval_seconds or 0)
        if poll_interval <= 0:
            drifts.append(
                ParityDrift(
                    missing_gate="poll_interval_seconds",
                    ticker=symbol,
                    strategy_id=strategy.strategy_id,
                    expected=">0",
                    found=poll_interval,
                )
            )
        if not str(strategy.timeframe or "").strip():
            drifts.append(
                ParityDrift(
                    missing_gate="timeframe",
                    ticker=symbol,
                    strategy_id=strategy.strategy_id,
                    expected="non_empty",
                    found=strategy.timeframe,
                )
            )
        return drifts

    def _audit_loop_settings(
        self,
        strategy: StrategyConfig,
        record: ResearchValidationRecord,
    ) -> list[ParityDrift]:
        symbol = strategy.symbol.upper()
        drifts: list[ParityDrift] = []
        runtime_params = strategy.params or {}
        for key, expected in record.params.items():
            if key not in runtime_params:
                drifts.append(
                    ParityDrift(
                        missing_gate="loop_settings",
                        ticker=symbol,
                        strategy_id=strategy.strategy_id,
                        expected=key,
                        found=None,
                    )
                )
                continue
            found = runtime_params[key]
            if isinstance(expected, (int, float)) and isinstance(found, (int, float)):
                if abs(float(found) - float(expected)) > PARAM_FLOAT_TOLERANCE:
                    drifts.append(
                        ParityDrift(
                            missing_gate="loop_settings",
                            ticker=symbol,
                            strategy_id=strategy.strategy_id,
                            expected=expected,
                            found=found,
                        )
                    )
            elif found != expected:
                drifts.append(
                    ParityDrift(
                        missing_gate="loop_settings",
                        ticker=symbol,
                        strategy_id=strategy.strategy_id,
                        expected=expected,
                        found=found,
                    )
                )
        return drifts

    @staticmethod
    def _resolve_borrow_drag_coefficient(params: dict) -> float | None:
        if "borrow_drag_coefficient" in params:
            return float(params["borrow_drag_coefficient"])
        if "short_borrow_fee_annual" in params:
            return float(params["short_borrow_fee_annual"])
        return None
