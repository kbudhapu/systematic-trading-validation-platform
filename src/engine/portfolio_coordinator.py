"""
Portfolio coordinator — integration harness above leg loops.

Collects per-leg signals, applies PortfolioBrain governors, scales risk budgets,
and persists constraint decisions before order routing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from src.config import StrategyConfig
from src.core.rolling_window import RollingWindow
from src.engine.portfolio_brain import (
    FORCE_LIQUIDATION_EXECUTE,
    FORCED_DURATION_EXHAUSTION,
    GLOBAL_ENTRY_LOCKOUT,
    QQQ_STRATEGY_ID,
    SPY_STRATEGY_ID,
    ActivePositionManifest,
    ExposureValidation,
    ForceLiquidationTarget,
    InventoryPathDependencyVerdict,
    LegMetrics,
    PortfolioBrain,
    compute_return_correlation,
)
from src.engine.execution_adaptor import ROUTING_POSTURE_AGGRESSIVE_TAKER
from src.models import Account, PortfolioState, Position, Signal, SignalAction
from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable
from src.router.default import OrderRouter
from src.router.risk_manager import RiskManager

PORTFOLIO_CONSTRAINT_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_constraint_ledger (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    constraint_type TEXT NOT NULL,
    strategy_id TEXT,
    symbol TEXT,
    action_taken TEXT NOT NULL,
    sizing_multiplier REAL,
    metadata_json TEXT NOT NULL,
    multi_day_net_inventory REAL,
    directional_entry_lockout TEXT,
    current_equity REAL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_constraint_cycle
    ON portfolio_constraint_ledger(cycle_id);
CREATE TABLE IF NOT EXISTS portfolio_inventory_registry (
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    opened_session_date TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    net_directional_exposure REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (strategy_id, symbol)
);
"""

_LEDGER_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("multi_day_net_inventory", "REAL"),
    ("directional_entry_lockout", "TEXT"),
    ("current_equity", "REAL"),
)


@dataclass
class LegCyclePayload:
    """Deferred leg evaluation awaiting portfolio coordination."""

    strategy_id: str
    symbol: str
    signal: Signal | None
    routing_params: dict[str, Any]
    base_risk_fraction: float
    window: RollingWindow
    enabled: bool = True
    health_score: float = 1.0
    champion_score: float = 0.0
    asset_context: Any | None = None
    bars_in_trade: int = 0
    position_side: str = "flat"


@dataclass(frozen=True)
class CoordinatedLegPlan:
    strategy_id: str
    signal: Signal | None
    routing_params: dict[str, Any]
    risk_fraction: float
    sizing_multiplier: float
    window: RollingWindow
    asset_context: Any | None
    blocked: bool
    block_reason: str
    force_liquidation: bool = False
    cancel_pending_orders: bool = False
    force_liquidation_reason: str = ""


@dataclass
class CycleCoordinationResult:
    cycle_id: str
    risk_budgets: dict[str, float]
    plans: dict[str, CoordinatedLegPlan]
    mode_reason: str
    exposure_allowed: bool
    # FR-2 (task_52e9e1d0): the exposure verdict's OWN reason — mode_reason above is the
    # PORTFOLIO-MODE label (e.g. spy_config_disabled) and has nothing to do with exposure;
    # logging them side by side without this field misled two audits.
    exposure_block_reason: str | None = None


def ensure_portfolio_constraint_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(PORTFOLIO_CONSTRAINT_LEDGER_DDL)
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(portfolio_constraint_ledger)")
        }
        for column_name, column_type in _LEDGER_COLUMN_MIGRATIONS:
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE portfolio_constraint_ledger "
                    f"ADD COLUMN {column_name} {column_type}"
                )


def log_portfolio_constraint(
    *,
    cycle_id: str,
    constraint_type: str,
    action_taken: str,
    strategy_id: str | None = None,
    symbol: str | None = None,
    sizing_multiplier: float | None = None,
    metadata: Mapping[str, Any] | None = None,
    multi_day_net_inventory: float | None = None,
    directional_entry_lockout: str | None = None,
    current_equity: float | None = None,
    db_path: Path = RESEARCH_VAULT_PATH,
    use_async_writer: bool = True,
) -> None:
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id,
        "constraint_type": constraint_type,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "action_taken": action_taken,
        "sizing_multiplier": sizing_multiplier,
        "metadata_json": json.dumps(dict(metadata or {}), separators=(",", ":")),
        "multi_day_net_inventory": multi_day_net_inventory,
        "directional_entry_lockout": directional_entry_lockout,
        "current_equity": current_equity,
    }
    if use_async_writer:
        from src.persistence.db_queue import enqueue_portfolio_constraint, get_async_db_writer

        writer = get_async_db_writer()
        if writer.is_running:
            enqueue_portfolio_constraint(row, db_path=str(db_path))
            return

    ensure_portfolio_constraint_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO portfolio_constraint_ledger (
                timestamp, cycle_id, constraint_type, strategy_id, symbol,
                action_taken, sizing_multiplier, metadata_json,
                multi_day_net_inventory, directional_entry_lockout, current_equity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["timestamp"],
                row["cycle_id"],
                row["constraint_type"],
                row["strategy_id"],
                row["symbol"],
                row["action_taken"],
                row["sizing_multiplier"],
                row["metadata_json"],
                row["multi_day_net_inventory"],
                row["directional_entry_lockout"],
                row["current_equity"],
            ),
        )


class PortfolioCoordinator:
    """
    Sits above individual leg loops: batches signals, applies portfolio brain,
    and emits coordinated execution plans for the order router.
    """

    def __init__(
        self,
        router: OrderRouter,
        *,
        db_path: Path = RESEARCH_VAULT_PATH,
        single_leg_symbol: str | None = None,
        static_risk_split: bool = False,
    ) -> None:
        self.router = router
        self.db_path = db_path
        # static_risk_split (default OFF) pins an equal commissioning split — see PortfolioBrain.
        self.brain = PortfolioBrain(single_leg_symbol=single_leg_symbol,
                                    static_equal_split=static_risk_split)
        self._cycle_id: str = ""
        self._pending: dict[str, LegCyclePayload] = {}
        self._windows: dict[str, RollingWindow] = {}
        self._last_result: CycleCoordinationResult | None = None

    def begin_cycle(self, *, account: Account) -> str:
        self._cycle_id = uuid.uuid4().hex
        self._pending.clear()
        self._windows.clear()
        self.brain.equity = max(account.equity, 1.0)
        return self._cycle_id

    def register_leg_evaluation(self, payload: LegCyclePayload) -> None:
        self._pending[payload.strategy_id] = payload
        self._windows[payload.strategy_id] = payload.window

    def _build_leg_metrics(
        self,
        enabled_configs: list[StrategyConfig],
        positions: list[Position],
        account: Account,
    ) -> dict[str, LegMetrics]:
        pos_by_symbol = {p.symbol.upper(): p for p in positions}
        total_dd_stress = 0.0
        leg_dd: dict[str, float] = {}

        for cfg in enabled_configs:
            realized = self._leg_realized_pnl(cfg.strategy_id)
            stress = max(0.0, -realized)
            leg_dd[cfg.strategy_id] = stress
            total_dd_stress += stress

        metrics: dict[str, LegMetrics] = {}
        for cfg in enabled_configs:
            payload = self._pending.get(cfg.strategy_id)
            window = payload.window if payload is not None else self._windows.get(cfg.strategy_id)
            closes = window.closes_array() if window is not None else np.asarray([], dtype=np.float64)
            realized_vol = self.brain.annualized_vol_from_closes(closes)
            marginal_sharpe = self._marginal_sharpe(cfg.strategy_id, cfg.symbol)
            dd_contrib = (
                leg_dd.get(cfg.strategy_id, 0.0) / total_dd_stress
                if total_dd_stress > 0.0
                else 0.0
            )
            pos = pos_by_symbol.get(cfg.symbol.upper())
            notional = 0.0
            if pos is not None:
                notional = abs(pos.qty) * max(pos.avg_entry_price, 0.0)
            health_score = payload.health_score if payload is not None else 1.0
            champion_score = payload.champion_score if payload is not None else 0.0
            metrics[cfg.strategy_id] = LegMetrics(
                strategy_id=cfg.strategy_id,
                symbol=cfg.symbol.upper(),
                realized_vol=realized_vol,
                drawdown_contribution=dd_contrib,
                marginal_sharpe=marginal_sharpe,
                notional_exposure=notional,
                health_score=health_score,
                champion_score=champion_score,
                enabled=cfg.enabled,
            )
        return metrics

    @staticmethod
    def _leg_realized_pnl(strategy_id: str) -> float:
        from src.persistence import db as persistence

        return float(persistence.get_leg_realized_pnl(strategy_id))

    def _marginal_sharpe(self, strategy_id: str, symbol: str) -> float:
        leg_state = self.router.risk_manager.get_leg_state(strategy_id, symbol)
        if not leg_state.promotion_pnls:
            return 0.25
        series = np.asarray(leg_state.promotion_pnls, dtype=np.float64)
        std = float(np.std(series))
        if std < 1e-9:
            return 0.25
        return max(float(np.mean(series) / std), 0.05)

    def _qqq_spy_correlation(self) -> float:
        qqq_window = None
        spy_window = None
        for sid, window in self._windows.items():
            if sid == QQQ_STRATEGY_ID:
                qqq_window = window
            elif sid == SPY_STRATEGY_ID:
                spy_window = window
        if qqq_window is None or spy_window is None:
            return 0.0
        return compute_return_correlation(
            qqq_window.closes_array(),
            spy_window.closes_array(),
        )

    def _symbol_correlation_map(self) -> dict[tuple[str, str], float]:
        symbols: dict[str, np.ndarray] = {}
        for payload in self._pending.values():
            symbols[payload.symbol.upper()] = payload.window.closes_array()
        keys = sorted(symbols)
        correlations: dict[tuple[str, str], float] = {}
        for i, sym_a in enumerate(keys):
            for sym_b in keys[i + 1 :]:
                corr = compute_return_correlation(symbols[sym_a], symbols[sym_b])
                correlations[(sym_a, sym_b)] = corr
                correlations[(sym_b, sym_a)] = corr
        return correlations

    def _sync_inventory_registry(
        self,
        enabled_configs: list[StrategyConfig],
        positions: list[Position],
    ) -> None:
        """Persist overnight carryover metadata for cross-session inventory memory."""
        today = datetime.now(timezone.utc).date().isoformat()
        pos_by_symbol = {p.symbol.upper(): p for p in positions}
        strategy_by_symbol = {cfg.symbol.upper(): cfg for cfg in enabled_configs}
        ensure_portfolio_constraint_schema(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            for symbol, pos in pos_by_symbol.items():
                cfg = strategy_by_symbol.get(symbol)
                if cfg is None:
                    continue
                side = "long" if pos.qty > 0 else "short"
                notional = abs(pos.qty) * max(pos.avg_entry_price, 0.0)
                signed_exposure = notional if side == "long" else -notional
                row = conn.execute(
                    """
                    SELECT opened_session_date
                    FROM portfolio_inventory_registry
                    WHERE strategy_id = ? AND symbol = ?
                    """,
                    (cfg.strategy_id, symbol),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO portfolio_inventory_registry (
                            strategy_id, symbol, side, opened_session_date,
                            last_seen_at, net_directional_exposure
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cfg.strategy_id,
                            symbol,
                            side,
                            today,
                            today,
                            signed_exposure,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE portfolio_inventory_registry
                        SET side = ?, last_seen_at = ?, net_directional_exposure = ?
                        WHERE strategy_id = ? AND symbol = ?
                        """,
                        (side, today, signed_exposure, cfg.strategy_id, symbol),
                    )

            for cfg in enabled_configs:
                symbol = cfg.symbol.upper()
                if symbol not in pos_by_symbol:
                    conn.execute(
                        """
                        DELETE FROM portfolio_inventory_registry
                        WHERE strategy_id = ? AND symbol = ?
                        """,
                        (cfg.strategy_id, symbol),
                    )

    def _build_active_positions_manifest(
        self,
        enabled_configs: list[StrategyConfig],
        positions: list[Position],
    ) -> list[ActivePositionManifest]:
        pos_by_symbol = {p.symbol.upper(): p for p in positions}
        manifests: list[ActivePositionManifest] = []
        ensure_portfolio_constraint_schema(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            for cfg in enabled_configs:
                symbol = cfg.symbol.upper()
                pos = pos_by_symbol.get(symbol)
                if pos is None:
                    continue
                row = conn.execute(
                    """
                    SELECT opened_session_date
                    FROM portfolio_inventory_registry
                    WHERE strategy_id = ? AND symbol = ?
                    """,
                    (cfg.strategy_id, symbol),
                ).fetchone()
                opened_session_date = (
                    row[0]
                    if row is not None
                    else datetime.now(timezone.utc).date().isoformat()
                )
                side = "long" if pos.qty > 0 else "short"
                notional = abs(pos.qty) * max(pos.avg_entry_price, 0.0)
                payload = self._pending.get(cfg.strategy_id)
                manifests.append(
                    ActivePositionManifest(
                        strategy_id=cfg.strategy_id,
                        symbol=symbol,
                        side=side,
                        notional=notional,
                        calendar_days_held=self.brain.calendar_days_between(
                            opened_session_date
                        ),
                        bars_in_trade=payload.bars_in_trade if payload is not None else 0,
                        opened_session_date=opened_session_date,
                    )
                )
        return manifests

    @staticmethod
    def _apply_inventory_entry_lockouts(
        active_signals: dict[str, Signal | None],
        verdict: InventoryPathDependencyVerdict,
    ) -> tuple[dict[str, Signal | None], dict[str, str]]:
        if not verdict.has_directive(GLOBAL_ENTRY_LOCKOUT):
            return active_signals, {}

        locked_signals = dict(active_signals)
        reasons: dict[str, str] = {}
        for strategy_id, signal in active_signals.items():
            if signal is None or signal.action not in (
                SignalAction.LONG,
                SignalAction.SHORT,
            ):
                continue
            direction = (
                "long" if signal.action == SignalAction.LONG else "short"
            )
            if not verdict.is_entry_locked(signal.symbol, direction):
                continue
            locked_signals[strategy_id] = None
            reasons[strategy_id] = (
                f"{GLOBAL_ENTRY_LOCKOUT}:{direction}:{signal.symbol.upper()}"
            )
        return locked_signals, reasons

    @staticmethod
    def _format_directional_entry_lockout(
        verdict: InventoryPathDependencyVerdict,
    ) -> str | None:
        if not verdict.blocked_entry_by_symbol:
            return None
        payload = {
            symbol: sorted(directions)
            for symbol, directions in verdict.blocked_entry_by_symbol.items()
        }
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _force_liquidation_map(
        verdict: InventoryPathDependencyVerdict,
    ) -> dict[str, ForceLiquidationTarget]:
        if not verdict.has_directive(FORCE_LIQUIDATION_EXECUTE):
            return {}
        return {target.strategy_id: target for target in verdict.force_liquidation_targets}

    @staticmethod
    def _build_forced_exit_signal(
        target: ForceLiquidationTarget,
        *,
        reference_price: float,
        timestamp: datetime,
    ) -> Signal:
        return Signal(
            symbol=target.symbol,
            action=SignalAction.EXIT,
            price=max(reference_price, 0.0),
            timestamp=timestamp,
            strategy_id=target.strategy_id,
            metadata={
                "force_liquidation": True,
                "transition_reason": target.transition_reason,
                "calendar_days_held": target.calendar_days_held,
            },
        )

    @staticmethod
    def _apply_force_liquidation_routing(
        routing_params: dict[str, Any],
        *,
        target: ForceLiquidationTarget,
    ) -> dict[str, Any]:
        updated = dict(routing_params)
        updated["force_terminal_liquidation"] = True
        updated["bypass_exit_thresholds"] = True
        updated["routing_posture_override"] = ROUTING_POSTURE_AGGRESSIVE_TAKER
        updated["force_liquidation_reason"] = target.transition_reason
        updated["calendar_days_held"] = target.calendar_days_held
        updated["symbol"] = target.symbol
        return updated

    def coordinate_cycle(
        self,
        *,
        enabled_configs: list[StrategyConfig],
        account: Account,
        positions: list[Position],
        capital_winner_strategy_id: str | None = None,
        gross_exposure_cap_multiplier: float = 1.0,
        macro_block_new_entries: bool = False,
    ) -> CycleCoordinationResult:
        if not self._cycle_id:
            self.begin_cycle(account=account)

        leg_metrics = self._build_leg_metrics(enabled_configs, positions, account)
        risk_budgets = self.brain.allocate_risk_budgets(leg_metrics)
        log_portfolio_constraint(
            cycle_id=self._cycle_id,
            constraint_type="risk_budget_allocation",
            action_taken="allocated",
            metadata={"budgets": risk_budgets},
            db_path=self.db_path,
        )

        correlation = self._qqq_spy_correlation()
        enabled_ids = frozenset(cfg.strategy_id for cfg in enabled_configs if cfg.enabled)
        mode = self.brain.resolve_portfolio_mode(
            leg_metrics,
            qqq_spy_correlation=correlation,
            capital_winner_strategy_id=capital_winner_strategy_id,
            enabled_strategy_ids=enabled_ids,
        )
        log_portfolio_constraint(
            cycle_id=self._cycle_id,
            constraint_type="portfolio_mode",
            action_taken=mode.mode,
            metadata={
                "reason": mode.reason,
                "spy_leg_enabled": mode.spy_leg_enabled,
                "single_leg_mode": mode.single_leg_mode,
                "dominant_strategy_id": mode.dominant_strategy_id,
                "qqq_spy_correlation": correlation,
            },
            db_path=self.db_path,
        )

        self._sync_inventory_registry(enabled_configs, positions)
        inventory_manifest = self._build_active_positions_manifest(
            enabled_configs,
            positions,
        )
        inventory_verdict = self.brain.check_inventory_path_dependency(
            inventory_manifest,
            account.equity,
        )
        log_portfolio_constraint(
            cycle_id=self._cycle_id,
            constraint_type="cross_session_inventory",
            action_taken=inventory_verdict.directive or "allowed",
            multi_day_net_inventory=inventory_verdict.multiday_net_exposure,
            directional_entry_lockout=self._format_directional_entry_lockout(
                inventory_verdict
            ),
            current_equity=account.equity,
            metadata={
                "breach_codes": list(inventory_verdict.breach_codes),
                "net_long_exposure": inventory_verdict.net_long_exposure,
                "net_short_exposure": inventory_verdict.net_short_exposure,
                "multiday_exposure_ratio": inventory_verdict.multiday_exposure_ratio,
                "cap_notional": inventory_verdict.cap_notional,
                "reasons": inventory_verdict.reasons,
                "manifest_count": len(inventory_manifest),
            },
            db_path=self.db_path,
        )

        force_liquidation_by_strategy = self._force_liquidation_map(inventory_verdict)
        for target in force_liquidation_by_strategy.values():
            log_portfolio_constraint(
                cycle_id=self._cycle_id,
                constraint_type="forced_duration_liquidation",
                strategy_id=target.strategy_id,
                symbol=target.symbol,
                action_taken=FORCE_LIQUIDATION_EXECUTE,
                multi_day_net_inventory=inventory_verdict.multiday_net_exposure,
                directional_entry_lockout=self._format_directional_entry_lockout(
                    inventory_verdict
                ),
                current_equity=account.equity,
                metadata={
                    "transition_event": FORCED_DURATION_EXHAUSTION,
                    "calendar_days_held": target.calendar_days_held,
                    "opened_session_date": target.opened_session_date,
                    "side": target.side,
                    "notional": target.notional,
                    "max_calendar_days": self.brain.max_multiday_holding_calendar_days,
                },
                db_path=self.db_path,
            )

        active_signals = {
            sid: payload.signal for sid, payload in self._pending.items()
        }
        active_signals, inventory_block_reasons = self._apply_inventory_entry_lockouts(
            active_signals,
            inventory_verdict,
        )
        for sid, reason in inventory_block_reasons.items():
            payload = self._pending.get(sid)
            log_portfolio_constraint(
                cycle_id=self._cycle_id,
                constraint_type="inventory_entry_lockout",
                strategy_id=sid,
                symbol=payload.symbol if payload else None,
                action_taken=GLOBAL_ENTRY_LOCKOUT,
                multi_day_net_inventory=inventory_verdict.multiday_net_exposure,
                directional_entry_lockout=self._format_directional_entry_lockout(
                    inventory_verdict
                ),
                current_equity=account.equity,
                metadata={"reason": reason},
                db_path=self.db_path,
            )

        conflict_resolution = self.brain.resolve_signal_conflicts(
            active_signals,
            leg_metrics=leg_metrics,
            symbol_correlations=self._symbol_correlation_map(),
        )
        for sid, reason in conflict_resolution.reasons.items():
            payload = self._pending.get(sid)
            log_portfolio_constraint(
                cycle_id=self._cycle_id,
                constraint_type="signal_conflict",
                strategy_id=sid,
                symbol=payload.symbol if payload else None,
                action_taken="blocked",
                sizing_multiplier=0.0,
                metadata={"reason": reason},
                db_path=self.db_path,
            )

        proposed = self.brain.estimate_proposed_positions(
            conflict_resolution.approved_signals,
            positions,
            leg_metrics,
            equity=account.equity,
        )
        exposure = self.brain.validate_global_exposure(
            proposed,
            equity=account.equity,
        )
        if gross_exposure_cap_multiplier < 1.0:
            scaled_multipliers = {
                sid: mult * gross_exposure_cap_multiplier
                for sid, mult in exposure.sizing_multipliers.items()
            }
            breach = (
                not exposure.allowed
                or gross_exposure_cap_multiplier < 0.65
            )
            exposure = ExposureValidation(
                allowed=not breach,
                sizing_multipliers=scaled_multipliers,
                # FR-2 (task_52e9e1d0): the code is appended ONLY when the cap causes a breach —
                # previously it decorated every cap<1.0 row (incl. zero-proposal 'allowed' cycles),
                # reading as a phantom block in the ledger.
                breach_codes=exposure.breach_codes
                + (("macro_risk_off_cap",) if breach else ()),
                net_beta=exposure.net_beta,
                gross_exposure_ratio=exposure.gross_exposure_ratio,
                sector_concentration=exposure.sector_concentration,
                factor_crowding=exposure.factor_crowding,
            )
        log_portfolio_constraint(
            cycle_id=self._cycle_id,
            constraint_type="global_exposure",
            action_taken="allowed" if exposure.allowed else "breach",
            sizing_multiplier=min(exposure.sizing_multipliers.values(), default=1.0),
            metadata={
                "breach_codes": list(exposure.breach_codes),
                "net_beta": exposure.net_beta,
                "gross_exposure_ratio": exposure.gross_exposure_ratio,
                "sector_concentration": exposure.sector_concentration,
                "factor_crowding": exposure.factor_crowding,
                "multipliers": exposure.sizing_multipliers,
            },
            db_path=self.db_path,
        )

        plans: dict[str, CoordinatedLegPlan] = {}
        liquidation_ts = datetime.now(timezone.utc)
        for strategy_id, payload in self._pending.items():
            approved_signal = conflict_resolution.approved_signals.get(strategy_id)
            blocked = strategy_id in conflict_resolution.blocked_strategy_ids
            block_reason = conflict_resolution.reasons.get(strategy_id, "")
            if strategy_id in inventory_block_reasons:
                blocked = True
                block_reason = block_reason or inventory_block_reasons[strategy_id]

            force_target = force_liquidation_by_strategy.get(strategy_id)
            force_liquidation = force_target is not None
            cancel_pending_orders = force_liquidation
            force_liquidation_reason = (
                force_target.transition_reason if force_target is not None else ""
            )
            if force_target is not None:
                latest_bar = payload.window.latest()
                reference_price = (
                    float(latest_bar.close) if latest_bar is not None else 0.0
                )
                approved_signal = self._build_forced_exit_signal(
                    force_target,
                    reference_price=reference_price,
                    timestamp=liquidation_ts,
                )
                blocked = False
                block_reason = FORCE_LIQUIDATION_EXECUTE

            if (
                not force_liquidation
                and not exposure.allowed
                and approved_signal is not None
                and approved_signal.action in (SignalAction.LONG, SignalAction.SHORT)
            ):
                blocked = True
                block_reason = block_reason or "|".join(exposure.breach_codes)

            # Y5: honour the portfolio risk mode's block_new_entries flag DIRECTLY, independent of the
            # gross-exposure cap. Previously RISK_OFF only blocked because RISK_OFF_GROSS_EXPOSURE_CAP
            # (0.55) happened to sit below the coordinator's 0.65 breach line — a latent trap: raising
            # the cap above 0.65 would silently stop the block while the decision still reported
            # block_new_entries=True. The flag is now ENFORCED, not decorative (B1: enforced or absent).
            if (
                not force_liquidation
                and macro_block_new_entries
                and approved_signal is not None
                and approved_signal.action in (SignalAction.LONG, SignalAction.SHORT)
            ):
                blocked = True
                block_reason = block_reason or "macro_risk_off_block"

            budget = risk_budgets.get(strategy_id, payload.base_risk_fraction)
            conflict_scale = conflict_resolution.scale_multipliers.get(strategy_id, 1.0)
            exposure_scale = exposure.sizing_multipliers.get(strategy_id, 1.0)
            sizing_multiplier = conflict_scale * exposure_scale
            if blocked:
                sizing_multiplier = 0.0
                approved_signal = None if (
                    approved_signal is not None
                    and approved_signal.action in (SignalAction.LONG, SignalAction.SHORT)
                ) else approved_signal

            routing_params = dict(payload.routing_params)
            if force_target is not None:
                routing_params = self._apply_force_liquidation_routing(
                    routing_params,
                    target=force_target,
                )
            elif sizing_multiplier < 1.0 and sizing_multiplier > 0.0:
                base_max = float(
                    routing_params.get(
                        "max_position_pct",
                        routing_params.get("effective_max_position_pct", 0.95),
                    )
                    or 0.95
                )
                routing_params["max_position_pct"] = base_max * sizing_multiplier
                routing_params["portfolio_coordination_scale"] = sizing_multiplier

            plans[strategy_id] = CoordinatedLegPlan(
                strategy_id=strategy_id,
                signal=approved_signal,
                routing_params=routing_params,
                risk_fraction=budget * max(sizing_multiplier, 0.0),
                sizing_multiplier=sizing_multiplier,
                window=payload.window,
                asset_context=payload.asset_context,
                blocked=blocked,
                block_reason=block_reason,
                force_liquidation=force_liquidation,
                cancel_pending_orders=cancel_pending_orders,
                force_liquidation_reason=force_liquidation_reason,
            )

        result = CycleCoordinationResult(
            cycle_id=self._cycle_id,
            risk_budgets=risk_budgets,
            plans=plans,
            mode_reason=mode.reason,
            exposure_allowed=exposure.allowed,
            exposure_block_reason=(
                "|".join(exposure.breach_codes) or "gross_cap_below_breach_line"
                if not exposure.allowed
                else None
            ),
        )
        self._last_result = result
        return result

    def route_coordinated_plan(
        self,
        plan: CoordinatedLegPlan,
        account: Account,
        positions: list[Position],
        portfolio_state: PortfolioState,
        active_windows: dict[str, RollingWindow] | None = None,
    ) -> tuple[list, PortfolioState]:
        if (
            plan.blocked
            and plan.signal is not None
            and plan.signal.action in (SignalAction.LONG, SignalAction.SHORT)
            and not plan.force_liquidation
        ):
            return [], portfolio_state

        routing_params = dict(plan.routing_params)
        if plan.force_liquidation:
            routing_params.setdefault("force_terminal_liquidation", True)
            routing_params.setdefault("bypass_exit_thresholds", True)
            routing_params.setdefault(
                "routing_posture_override",
                ROUTING_POSTURE_AGGRESSIVE_TAKER,
            )

        signal = plan.signal
        if signal is None:
            return [], portfolio_state

        return self.router.route(
            signal,
            plan.window,
            account,
            positions,
            portfolio_state,
            risk_budget_fraction=plan.risk_fraction,
            strategy_params=routing_params,
            asset_context=plan.asset_context,
            active_windows=active_windows,
        )

    @property
    def risk_manager(self) -> RiskManager:
        return self.router.risk_manager
