"""
Dual-policy challenger registry and matched-capital counterfactual shadow logger.
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

from src.persistence.db import RESEARCH_VAULT_PATH
from src.persistence.ownership_guard import ensure_db_writable
from src.router.risk_manager import resolve_trading_session

CHALLENGER_STATUS_UNREGISTERED = "UNREGISTERED"
CHALLENGER_STATUS_REGISTERED = "REGISTERED"
CHALLENGER_STATUS_ACTIVE_SHADOW = "ACTIVE_SHADOW"
CHALLENGER_STATUS_PROMOTED = "PROMOTED"
CHALLENGER_STATUS_RETIRED = "RETIRED"

SHADOW_ALLOCATION = {
    "STAND_DOWN": 0.0,
    "ALLOCATION_HALF": 0.5,
    "ALLOCATION_MAX": 1.0,
}
RULES_ALLOCATION = {
    "NO_TRADE_FALLBACK": 0.0,
    "DEFENSIVE_FALLBACK": 0.25,
    "BASELINE_FALLBACK": 0.5,
    "UNSAFE_DISABLED": 0.0,
    "BEAR_DEFENSIVE": 0.25,
    "HIGH_VOL_MR": 1.0,
    "CALM_MR": 1.0,
    "UNKNOWN": 0.75,
}

CHAMPION_DEFAULT_ID = "champion:GLOBAL:SHADOW_ML_OPTIMIZED_WEIGHTS"

CHALLENGER_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS challenger_registry (
    challenger_id TEXT PRIMARY KEY,
    champion_id TEXT NOT NULL,
    status TEXT NOT NULL,
    model_metadata_json TEXT NOT NULL,
    registered_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_challenger_champion
    ON challenger_registry(champion_id, status);
"""

DUAL_POLICY_SHADOW_LOG_DDL = """
CREATE TABLE IF NOT EXISTS dual_policy_shadow_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    champion_id TEXT NOT NULL,
    challenger_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    session_type TEXT,
    regime_id TEXT,
    market_state_json TEXT NOT NULL,
    champion_action TEXT NOT NULL,
    challenger_action TEXT NOT NULL,
    champion_capital REAL NOT NULL,
    challenger_capital REAL NOT NULL,
    champion_pnl REAL NOT NULL,
    challenger_pnl REAL NOT NULL,
    rules_baseline_pnl REAL NOT NULL,
    matched_capital_notional REAL NOT NULL,
    execution_path_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dual_shadow_ts
    ON dual_policy_shadow_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_dual_shadow_pair
    ON dual_policy_shadow_log(champion_id, challenger_id);
"""


@dataclass(frozen=True)
class ChallengerRecord:
    challenger_id: str
    champion_id: str
    status: str
    model_metadata: dict[str, Any]
    registered_at: str | None
    updated_at: str


@dataclass(frozen=True)
class CounterfactualLogResult:
    log_id: int
    champion_id: str
    challenger_id: str
    champion_pnl: float
    challenger_pnl: float
    rules_baseline_pnl: float
    matched_capital_notional: float


def ensure_challenger_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    ensure_db_writable(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(CHALLENGER_REGISTRY_DDL)
        conn.executescript(DUAL_POLICY_SHADOW_LOG_DDL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rules_allocation(action: str) -> float:
    return float(RULES_ALLOCATION.get(action, RULES_ALLOCATION["UNKNOWN"]))


def _shadow_allocation(action: str) -> float:
    return float(SHADOW_ALLOCATION.get(action, 0.75))


def _policy_action(policy: Mapping[str, Any], market_state: Mapping[str, Any]) -> str:
    if "action" in policy:
        return str(policy["action"])
    outputs = policy.get("raw_policy_outputs")
    if isinstance(outputs, dict):
        action_idx = int(outputs.get("action_idx", 2))
        actions = ("STAND_DOWN", "ALLOCATION_HALF", "ALLOCATION_MAX")
        if 0 <= action_idx < len(actions):
            return actions[action_idx]
    state_vector = market_state.get("state_vector")
    if isinstance(state_vector, list) and state_vector:
        score = float(np.mean(state_vector))
        if score < -0.5:
            return "STAND_DOWN"
        if score < 0.0:
            return "ALLOCATION_HALF"
        return "ALLOCATION_MAX"
    return "ALLOCATION_HALF"


def _expected_reward(market_state: Mapping[str, Any]) -> float:
    for key in ("expected_reward", "realized_reward_24h", "realized_reward_1h"):
        if key in market_state and market_state[key] is not None:
            return float(market_state[key])
    return float(market_state.get("price_return_proxy", 0.0) or 0.0)


class ChallengerRegistry:
    """Catalog and dual-policy counterfactual shadow logger."""

    def __init__(self, db_path: Path = RESEARCH_VAULT_PATH) -> None:
        self.db_path = db_path
        ensure_challenger_schema(db_path)

    def register_challenger(
        self,
        *,
        challenger_id: str,
        champion_id: str,
        model_metadata: Mapping[str, Any],
        status: str = CHALLENGER_STATUS_REGISTERED,
    ) -> ChallengerRecord:
        now = _now_iso()
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT registered_at FROM challenger_registry WHERE challenger_id = ?",
                (challenger_id,),
            ).fetchone()
            registered_at = str(existing[0]) if existing and existing[0] else now
            conn.execute(
                """
                INSERT INTO challenger_registry (
                    challenger_id, champion_id, status, model_metadata_json,
                    registered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(challenger_id) DO UPDATE SET
                    champion_id = excluded.champion_id,
                    status = excluded.status,
                    model_metadata_json = excluded.model_metadata_json,
                    registered_at = COALESCE(challenger_registry.registered_at, excluded.registered_at),
                    updated_at = excluded.updated_at
                """,
                (
                    challenger_id,
                    champion_id,
                    status,
                    json.dumps(dict(model_metadata), separators=(",", ":")),
                    registered_at,
                    now,
                ),
            )
        return ChallengerRecord(
            challenger_id=challenger_id,
            champion_id=champion_id,
            status=status,
            model_metadata=dict(model_metadata),
            registered_at=registered_at,
            updated_at=now,
        )

    def create_unregistered_challenger(
        self,
        *,
        champion_id: str,
        model_metadata: Mapping[str, Any] | None = None,
    ) -> ChallengerRecord:
        challenger_id = f"challenger:{uuid.uuid4().hex[:12]}"
        metadata = dict(model_metadata or {})
        metadata.setdefault("seed_source", "runtime_shadow")
        return self.register_challenger(
            challenger_id=challenger_id,
            champion_id=champion_id,
            model_metadata=metadata,
            status=CHALLENGER_STATUS_UNREGISTERED,
        )

    def get_challenger(self, challenger_id: str) -> ChallengerRecord | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT challenger_id, champion_id, status, model_metadata_json,
                       registered_at, updated_at
                FROM challenger_registry
                WHERE challenger_id = ?
                """,
                (challenger_id,),
            ).fetchone()
        if row is None:
            return None
        return ChallengerRecord(
            challenger_id=str(row[0]),
            champion_id=str(row[1]),
            status=str(row[2]),
            model_metadata=json.loads(row[3]),
            registered_at=row[4],
            updated_at=str(row[5]),
        )

    def list_active_shadow_challengers(self, champion_id: str) -> list[ChallengerRecord]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT challenger_id, champion_id, status, model_metadata_json,
                       registered_at, updated_at
                FROM challenger_registry
                WHERE champion_id = ?
                  AND status IN (?, ?, ?)
                ORDER BY updated_at DESC
                """,
                (
                    champion_id,
                    CHALLENGER_STATUS_REGISTERED,
                    CHALLENGER_STATUS_ACTIVE_SHADOW,
                    CHALLENGER_STATUS_UNREGISTERED,
                ),
            ).fetchall()
        return [
            ChallengerRecord(
                challenger_id=str(r[0]),
                champion_id=str(r[1]),
                status=str(r[2]),
                model_metadata=json.loads(r[3]),
                registered_at=r[4],
                updated_at=str(r[5]),
            )
            for r in rows
        ]

    def activate_shadow(self, challenger_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE challenger_registry
                SET status = ?, updated_at = ?
                WHERE challenger_id = ?
                """,
                (CHALLENGER_STATUS_ACTIVE_SHADOW, _now_iso(), challenger_id),
            )

    def load_champion_policy(self, champion_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT params_json
                FROM regime_champions
                WHERE symbol = ? AND regime = ?
                """,
                ("GLOBAL", "SHADOW_ML_OPTIMIZED_WEIGHTS"),
            ).fetchone()
        if row is None:
            return {"policy_id": champion_id, "action": "ALLOCATION_MAX"}
        payload = json.loads(row[0])
        if not isinstance(payload, dict):
            return {"policy_id": champion_id, "action": "ALLOCATION_MAX"}
        payload["policy_id"] = champion_id
        return payload

    def log_counterfactual_state(
        self,
        champion_id: str,
        challenger_id: str,
        market_state: Mapping[str, Any],
    ) -> CounterfactualLogResult:
        ensure_challenger_schema(self.db_path)
        champion = self.load_champion_policy(champion_id)
        challenger_row = self.get_challenger(challenger_id)
        challenger_policy = (
            challenger_row.model_metadata
            if challenger_row is not None
            else {"policy_id": challenger_id}
        )

        champion_action = _policy_action(champion, market_state)
        challenger_action = _policy_action(challenger_policy, market_state)
        rules_action = str(market_state.get("rules_engine_action", "CALM_MR"))
        expected_reward = _expected_reward(market_state)

        equity = float(market_state.get("equity", 100_000.0) or 100_000.0)
        matched_notional = float(
            market_state.get("matched_capital_notional", equity * 0.01) or equity * 0.01
        )

        champion_capital = matched_notional * _shadow_allocation(champion_action)
        challenger_capital = matched_notional * _shadow_allocation(challenger_action)
        rules_capital = matched_notional * _rules_allocation(rules_action)

        champion_pnl = champion_capital * expected_reward
        challenger_pnl = challenger_capital * expected_reward
        rules_pnl = rules_capital * expected_reward

        ts_raw = market_state.get("timestamp")
        if isinstance(ts_raw, datetime):
            ts = ts_raw.astimezone(timezone.utc)
        else:
            ts = datetime.fromisoformat(str(ts_raw)) if ts_raw else datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

        session_type = str(
            market_state.get("session_type") or resolve_trading_session(ts)
        )
        regime_id = str(market_state.get("regime_id", "UNKNOWN"))
        symbol = str(market_state.get("symbol", "UNKNOWN")).upper()

        execution_path = {
            "champion_action": champion_action,
            "challenger_action": challenger_action,
            "rules_action": rules_action,
            "expected_reward": expected_reward,
            "champion_allocation": _shadow_allocation(champion_action),
            "challenger_allocation": _shadow_allocation(challenger_action),
            "rules_allocation": _rules_allocation(rules_action),
        }

        row = {
            "timestamp": ts.isoformat(),
            "champion_id": champion_id,
            "challenger_id": challenger_id,
            "symbol": symbol,
            "session_type": session_type,
            "regime_id": regime_id,
            "market_state_json": json.dumps(dict(market_state), default=str, separators=(",", ":")),
            "champion_action": champion_action,
            "challenger_action": challenger_action,
            "champion_capital": champion_capital,
            "challenger_capital": challenger_capital,
            "champion_pnl": champion_pnl,
            "challenger_pnl": challenger_pnl,
            "rules_baseline_pnl": rules_pnl,
            "matched_capital_notional": matched_notional,
            "execution_path_json": json.dumps(execution_path, separators=(",", ":")),
        }

        from src.persistence.db_queue import enqueue_counterfactual_state, get_async_db_writer

        writer = get_async_db_writer()
        if writer.is_running:
            enqueue_counterfactual_state(row, db_path=str(self.db_path))
            log_id = 0
        else:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO dual_policy_shadow_log (
                        timestamp, champion_id, challenger_id, symbol, session_type,
                        regime_id, market_state_json, champion_action, challenger_action,
                        champion_capital, challenger_capital, champion_pnl, challenger_pnl,
                        rules_baseline_pnl, matched_capital_notional, execution_path_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["timestamp"],
                        row["champion_id"],
                        row["challenger_id"],
                        row["symbol"],
                        row["session_type"],
                        row["regime_id"],
                        row["market_state_json"],
                        row["champion_action"],
                        row["challenger_action"],
                        row["champion_capital"],
                        row["challenger_capital"],
                        row["champion_pnl"],
                        row["challenger_pnl"],
                        row["rules_baseline_pnl"],
                        row["matched_capital_notional"],
                        row["execution_path_json"],
                    ),
                )
                log_id = int(cur.lastrowid)

        if challenger_row is not None and challenger_row.status == CHALLENGER_STATUS_REGISTERED:
            self.activate_shadow(challenger_id)

        return CounterfactualLogResult(
            log_id=log_id,
            champion_id=champion_id,
            challenger_id=challenger_id,
            champion_pnl=champion_pnl,
            challenger_pnl=challenger_pnl,
            rules_baseline_pnl=rules_pnl,
            matched_capital_notional=matched_notional,
        )

    def ensure_default_challenger(self, champion_id: str = CHAMPION_DEFAULT_ID) -> ChallengerRecord:
        challengers = self.list_active_shadow_challengers(champion_id)
        if challengers:
            return challengers[0]
        return self.register_challenger(
            challenger_id=f"challenger:default:{champion_id}",
            champion_id=champion_id,
            model_metadata={"seed_source": "default_shadow_pair", "action": "ALLOCATION_HALF"},
            status=CHALLENGER_STATUS_ACTIVE_SHADOW,
        )

    def fetch_counterfactual_history(
        self,
        *,
        champion_id: str,
        challenger_id: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM dual_policy_shadow_log
                WHERE champion_id = ? AND challenger_id = ?
                ORDER BY log_id DESC
                LIMIT ?
                """,
                (champion_id, challenger_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]
