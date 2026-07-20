"""
Champion staleness and model drift evaluation — distinguishes chronologically
old but stable models from old-and-wrong production parameter sets.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable

STATE_DRIFT_ALERT = "STATE_DRIFT_ALERT"

CLASS_STABLE = "STABLE"
CLASS_OLD_BUT_STABLE = "OLD_BUT_STABLE"
CLASS_OLD_AND_WRONG = "OLD_AND_WRONG"

DIRECTIVE_NONE = "NONE"
DIRECTIVE_MONITOR = "MONITOR"
DIRECTIVE_STAGED_ROLLBACK = "STAGED_ROLLBACK"
DIRECTIVE_FORCE_MINI_SWEEP = "FORCE_MINI_SWEEP"

CHAMPION_AGE_STALE_DAYS = 14
MIN_TRADES_FOR_DRIFT = 8
CONSECUTIVE_MISS_THRESHOLD = 4
WIN_RATE_DECAY_THRESHOLD = -0.15
TRACKING_ERROR_DECAY_THRESHOLD = -0.20
BACKTEST_HIT_RATE_DEFAULT = 0.55
BACKTEST_SHARPE_DEFAULT = 0.80

STATE_DRIFT_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS state_drift_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    strategy_id TEXT,
    alert_type TEXT NOT NULL DEFAULT 'STATE_DRIFT_ALERT',
    classification TEXT NOT NULL,
    directive TEXT NOT NULL,
    champion_age_days INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_state_drift_symbol
    ON state_drift_alerts(symbol, timestamp);
"""


@dataclass(frozen=True)
class DriftMetrics:
    champion_age_days: int
    consecutive_misses: int
    consecutive_miss_rate: float
    live_win_rate: float
    backtest_expected_win_rate: float
    win_rate_decay: float
    tracking_error: float
    tracking_error_baseline: float
    tracking_error_decay: float
    sample_trades: int
    live_sharpe: float
    backtest_expected_sharpe: float


@dataclass(frozen=True)
class DriftVerdict:
    symbol: str
    regime: str
    strategy_id: str | None
    classification: str
    directive: str
    metrics: DriftMetrics
    alert_id: int | None
    reason: str


def ensure_state_drift_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(STATE_DRIFT_ALERTS_DDL)


def register_state_drift_alert(
    *,
    symbol: str,
    regime: str,
    strategy_id: str | None,
    classification: str,
    directive: str,
    champion_age_days: int,
    metrics: Mapping[str, Any],
    db_path: Path = RESEARCH_VAULT_PATH,
) -> int:
    ensure_state_drift_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO state_drift_alerts (
                timestamp, symbol, regime, strategy_id, alert_type,
                classification, directive, champion_age_days, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                symbol.upper(),
                regime,
                strategy_id,
                STATE_DRIFT_ALERT,
                classification,
                directive,
                int(champion_age_days),
                json.dumps(dict(metrics), separators=(",", ":")),
            ),
        )
        return int(cur.lastrowid)


def fetch_recent_drift_alerts(
    *,
    symbol: str | None = None,
    limit: int = 20,
    db_path: Path = RESEARCH_VAULT_PATH,
) -> list[dict[str, Any]]:
    ensure_state_drift_schema(db_path)
    query = """
        SELECT *
        FROM state_drift_alerts
        WHERE alert_type = ?
    """
    params: list[Any] = [STATE_DRIFT_ALERT]
    if symbol is not None:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY alert_id DESC LIMIT ?"
    params.append(int(limit))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _load_champion_row(
    symbol: str,
    regime: str,
    db_path: Path,
) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT params_json, composite_score, promoted_at, run_id
            FROM regime_champions
            WHERE symbol = ? AND regime = ?
            """,
            (symbol.upper(), regime),
        ).fetchone()
    if row is None:
        return None
    params_raw, score, promoted_at, run_id = row
    return {
        "params": json.loads(params_raw),
        "composite_score": float(score),
        "promoted_at": str(promoted_at),
        "run_id": run_id,
    }


def _champion_age_days(promoted_at: str, now: datetime) -> int:
    promoted = datetime.fromisoformat(promoted_at)
    if promoted.tzinfo is None:
        promoted = promoted.replace(tzinfo=timezone.utc)
    return max(0, (now.astimezone(timezone.utc) - promoted.astimezone(timezone.utc)).days)


def _fetch_live_trade_pnls(
    strategy_id: str | None,
    symbol: str,
    db_path: Path,
    *,
    limit: int = 30,
) -> list[float]:
    pnls: list[float] = []
    ensure_state_drift_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        if strategy_id:
            rows = conn.execute(
                """
                SELECT trade_pnl
                FROM active_promotion_attribution
                WHERE strategy_id = ? AND symbol = ?
                ORDER BY log_id DESC
                LIMIT ?
                """,
                (strategy_id, symbol.upper(), limit),
            ).fetchall()
            pnls.extend(float(r[0]) for r in rows)
        if len(pnls) < limit:
            try:
                rows = conn.execute(
                    """
                    SELECT pnl
                    FROM live_attribution_ledger
                    WHERE symbol = ?
                    ORDER BY attribution_id DESC
                    LIMIT ?
                    """,
                    (symbol.upper(), limit),
                ).fetchall()
                if not pnls:
                    pnls.extend(float(r[0]) for r in rows)
            except sqlite3.OperationalError:
                pass
    return pnls


def _consecutive_misses(pnls: list[float]) -> int:
    streak = 0
    for pnl in pnls:
        if pnl < 0.0:
            streak += 1
        else:
            break
    return streak


def _compute_tracking_error(pnls: list[float], expected_mean: float) -> float:
    if len(pnls) < 2:
        return 0.0
    arr = np.asarray(pnls, dtype=np.float64)
    return float(np.std(arr - expected_mean))


class ModelDriftEvaluator:
    """Evaluates production champions using live drift metrics, not age alone."""

    def __init__(
        self,
        *,
        stale_age_days: int = CHAMPION_AGE_STALE_DAYS,
        db_path: Path = RESEARCH_VAULT_PATH,
    ) -> None:
        self.stale_age_days = stale_age_days
        self.db_path = db_path

    def _backtest_expectations(self, params: dict[str, Any]) -> tuple[float, float]:
        win_rate = float(
            params.get("holdout_hit_rate")
            or params.get("backtest_hit_rate")
            or BACKTEST_HIT_RATE_DEFAULT
        )
        sharpe = float(
            params.get("holdout_sharpe")
            or params.get("backtest_sharpe")
            or BACKTEST_SHARPE_DEFAULT
        )
        return win_rate, sharpe

    def _build_metrics(
        self,
        *,
        champion_age_days: int,
        pnls: list[float],
        params: dict[str, Any],
    ) -> DriftMetrics:
        expected_win, expected_sharpe = self._backtest_expectations(params)
        sample = len(pnls)
        if sample == 0:
            return DriftMetrics(
                champion_age_days=champion_age_days,
                consecutive_misses=0,
                consecutive_miss_rate=0.0,
                live_win_rate=expected_win,
                backtest_expected_win_rate=expected_win,
                win_rate_decay=0.0,
                tracking_error=0.0,
                tracking_error_baseline=0.0,
                tracking_error_decay=0.0,
                sample_trades=0,
                live_sharpe=expected_sharpe,
                backtest_expected_sharpe=expected_sharpe,
            )

        arr = np.asarray(pnls, dtype=np.float64)
        wins = (arr > 0.0).astype(np.float64)
        live_win_rate = float(np.mean(wins))
        win_rate_decay = live_win_rate - expected_win
        misses = _consecutive_misses(pnls)
        miss_rate = misses / max(sample, 1)
        expected_mean = float(np.mean(arr)) if expected_sharpe <= 0 else expected_sharpe * float(
            np.std(arr) if float(np.std(arr)) > 1e-9 else 1.0
        )
        tracking_error = _compute_tracking_error(pnls, expected_mean)
        baseline_window = pnls[-min(sample, 20) :]
        baseline_te = _compute_tracking_error(
            baseline_window,
            expected_mean,
        )
        if len(pnls) >= 10:
            recent_te = _compute_tracking_error(pnls[:10], expected_mean)
            te_decay = (recent_te - baseline_te) / max(baseline_te, 1e-9)
        else:
            recent_te = tracking_error
            te_decay = 0.0
        std = float(np.std(arr))
        live_sharpe = float(np.mean(arr) / std) if std > 1e-9 else 0.0

        return DriftMetrics(
            champion_age_days=champion_age_days,
            consecutive_misses=misses,
            consecutive_miss_rate=float(miss_rate),
            live_win_rate=live_win_rate,
            backtest_expected_win_rate=expected_win,
            win_rate_decay=float(win_rate_decay),
            tracking_error=float(recent_te),
            tracking_error_baseline=float(baseline_te),
            tracking_error_decay=float(te_decay),
            sample_trades=sample,
            live_sharpe=live_sharpe,
            backtest_expected_sharpe=expected_sharpe,
        )

    def _classify(self, metrics: DriftMetrics) -> tuple[str, str, str]:
        if metrics.champion_age_days < self.stale_age_days:
            return CLASS_STABLE, DIRECTIVE_NONE, "champion_age_within_window"

        if metrics.sample_trades < MIN_TRADES_FOR_DRIFT:
            return (
                CLASS_OLD_BUT_STABLE,
                DIRECTIVE_MONITOR,
                "stale_age_insufficient_live_sample",
            )

        wrong_signals = 0
        if metrics.consecutive_misses >= CONSECUTIVE_MISS_THRESHOLD:
            wrong_signals += 1
        if metrics.win_rate_decay <= WIN_RATE_DECAY_THRESHOLD:
            wrong_signals += 1
        if metrics.tracking_error_decay <= TRACKING_ERROR_DECAY_THRESHOLD:
            wrong_signals += 1

        if wrong_signals >= 2 or (
            metrics.consecutive_misses >= CONSECUTIVE_MISS_THRESHOLD
            and metrics.win_rate_decay <= WIN_RATE_DECAY_THRESHOLD
        ):
            directive = (
                DIRECTIVE_FORCE_MINI_SWEEP
                if metrics.champion_age_days >= self.stale_age_days * 2
                else DIRECTIVE_STAGED_ROLLBACK
            )
            return CLASS_OLD_AND_WRONG, directive, "live_drift_breaches"

        return CLASS_OLD_BUT_STABLE, DIRECTIVE_MONITOR, "stale_age_metrics_stable"

    def evaluate_champion(
        self,
        *,
        symbol: str,
        regime: str,
        strategy_id: str | None = None,
        register_alert: bool = True,
    ) -> DriftVerdict:
        champion = _load_champion_row(symbol, regime, self.db_path)
        now = datetime.now(timezone.utc)
        if champion is None:
            metrics = DriftMetrics(
                champion_age_days=999,
                consecutive_misses=0,
                consecutive_miss_rate=0.0,
                live_win_rate=0.0,
                backtest_expected_win_rate=BACKTEST_HIT_RATE_DEFAULT,
                win_rate_decay=0.0,
                tracking_error=0.0,
                tracking_error_baseline=0.0,
                tracking_error_decay=0.0,
                sample_trades=0,
                live_sharpe=0.0,
                backtest_expected_sharpe=BACKTEST_SHARPE_DEFAULT,
            )
            return DriftVerdict(
                symbol=symbol.upper(),
                regime=regime,
                strategy_id=strategy_id,
                classification=CLASS_OLD_AND_WRONG,
                directive=DIRECTIVE_FORCE_MINI_SWEEP,
                metrics=metrics,
                alert_id=None,
                reason="missing_champion",
            )

        age_days = _champion_age_days(champion["promoted_at"], now)
        pnls = _fetch_live_trade_pnls(strategy_id, symbol, self.db_path)
        metrics = self._build_metrics(
            champion_age_days=age_days,
            pnls=pnls,
            params=champion["params"],
        )
        classification, directive, reason = self._classify(metrics)
        alert_id: int | None = None
        if classification == CLASS_OLD_AND_WRONG and register_alert:
            alert_id = register_state_drift_alert(
                symbol=symbol,
                regime=regime,
                strategy_id=strategy_id,
                classification=classification,
                directive=directive,
                champion_age_days=age_days,
                metrics={
                    "consecutive_misses": metrics.consecutive_misses,
                    "win_rate_decay": metrics.win_rate_decay,
                    "tracking_error_decay": metrics.tracking_error_decay,
                    "live_win_rate": metrics.live_win_rate,
                    "live_sharpe": metrics.live_sharpe,
                    "sample_trades": metrics.sample_trades,
                    "promoted_at": champion["promoted_at"],
                },
                db_path=self.db_path,
            )
        return DriftVerdict(
            symbol=symbol.upper(),
            regime=regime,
            strategy_id=strategy_id,
            classification=classification,
            directive=directive,
            metrics=metrics,
            alert_id=alert_id,
            reason=reason,
        )

    def evaluate_portfolio(
        self,
        legs: list[tuple[str, str, str | None]],
        *,
        register_alerts: bool = True,
    ) -> list[DriftVerdict]:
        return [
            self.evaluate_champion(
                symbol=symbol,
                regime=regime,
                strategy_id=strategy_id,
                register_alert=register_alerts,
            )
            for symbol, regime, strategy_id in legs
        ]
