"""
Dynamic market impact and turnover governor for live execution scale control.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np

from src.persistence.db import RESEARCH_VAULT_PATH

ET = ZoneInfo("America/New_York")

DEFAULT_EQUITY_TURNOVER_MULTIPLIER = 3.0
DEFAULT_MAX_PARTICIPATION_RATE = 0.01
DEFAULT_MIN_PARTICIPATION_RATE = 0.0025
DEFAULT_SQRT_IMPACT_COEFFICIENT = 0.35
DEFAULT_LINEAR_IMPACT_COEFFICIENT = 0.08
IMPACT_COEFFICIENT_EMA_ALPHA = 0.15
MIN_BAR_VOLUME_SHARES = 1.0

CAPACITY_TURNOVER_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS capacity_turnover_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    strategy_id TEXT,
    side TEXT,
    notional REAL NOT NULL,
    cumulative_notional REAL NOT NULL,
    cap_notional REAL NOT NULL,
    capped INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_capacity_turnover_session
    ON capacity_turnover_ledger(session_date, symbol);
"""

CAPACITY_DAILY_TOTALS_DDL = """
CREATE TABLE IF NOT EXISTS capacity_daily_totals (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    realized_turnover_notional REAL NOT NULL DEFAULT 0,
    cap_notional REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_date, symbol)
);
"""

MARKET_IMPACT_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS market_impact_ledger (
    impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    side TEXT NOT NULL,
    expected_impact_bps REAL NOT NULL,
    realized_impact_bps REAL NOT NULL,
    impact_residual_bps REAL NOT NULL,
    sqrt_coefficient REAL NOT NULL,
    linear_coefficient REAL NOT NULL,
    trailing_volume REAL NOT NULL,
    model_used TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_impact_symbol_ts
    ON market_impact_ledger(symbol, timestamp);
"""

IMPACT_COEFFICIENT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS market_impact_coefficients (
    symbol TEXT PRIMARY KEY,
    sqrt_coefficient REAL NOT NULL,
    linear_coefficient REAL NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


class ImpactModel(str, Enum):
    SQRT = "SQRT"
    LINEAR = "LINEAR"
    BLENDED = "BLENDED"


@dataclass(frozen=True)
class TurnoverLimitDecision:
    symbol: str
    session_date: str
    proposed_notional: float
    allowed_notional: float
    daily_turnover_so_far: float
    cap_notional: float
    current_equity: float
    capped: bool
    blocked: bool
    breach: bool
    session_locked: bool
    reason: str


@dataclass(frozen=True)
class ParticipationClampDecision:
    symbol: str
    original_shares: float
    clamped_shares: float
    participation_rate: float
    max_participation_rate: float
    reference_bar_volume: float
    clamped: bool
    reason: str


@dataclass(frozen=True)
class ImpactEstimateResult:
    trade_id: str
    symbol: str
    expected_impact_bps: float
    realized_impact_bps: float
    impact_residual_bps: float
    model_used: ImpactModel
    sqrt_coefficient: float
    linear_coefficient: float
    coefficient_adjustment: float
    trailing_volume: float


@dataclass
class CapacityGovernor:
    """Equity-scaled turnover caps and retail participation limits."""

    db_path: Path = RESEARCH_VAULT_PATH
    equity_turnover_multiplier: float = DEFAULT_EQUITY_TURNOVER_MULTIPLIER
    max_participation_rate: float = DEFAULT_MAX_PARTICIPATION_RATE
    min_participation_rate: float = DEFAULT_MIN_PARTICIPATION_RATE
    _current_equity: float = field(default=0.0, repr=False)
    _session_locked: bool = field(default=False, repr=False)
    _breach_reason: str = field(default="", repr=False)
    _session_cache: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        ensure_capacity_governor_schema(self.db_path)

    @property
    def current_equity(self) -> float:
        return self._current_equity

    @property
    def session_locked(self) -> bool:
        return self._session_locked

    @property
    def breach_reason(self) -> str:
        return self._breach_reason

    def restore_session_state(self, *, locked: bool, reason: str = "") -> None:
        self._session_locked = locked
        self._breach_reason = reason

    def apply_config(
        self,
        *,
        equity_turnover_multiplier: float,
        max_participation_rate: float,
        min_participation_rate: float,
    ) -> None:
        self.equity_turnover_multiplier = float(equity_turnover_multiplier)
        self.max_participation_rate = float(max_participation_rate)
        self.min_participation_rate = float(min_participation_rate)

    def update_account_equity(self, equity: float) -> float:
        self._current_equity = max(float(equity), 0.0)
        return self._current_equity

    def resolve_daily_turnover_cap(self, current_equity: float | None = None) -> float:
        equity = self._current_equity if current_equity is None else max(float(current_equity), 0.0)
        if equity <= 0.0:
            return 0.0
        return equity * float(self.equity_turnover_multiplier)

    def enforce_daily_turnover_limits(
        self,
        symbol: str,
        proposed_size_notional: float,
        current_equity: float,
        *,
        strategy_id: str = "",
        session_date: str | None = None,
    ) -> TurnoverLimitDecision:
        sym = symbol.upper()
        proposed = max(float(proposed_size_notional), 0.0)
        equity = max(float(current_equity), 0.0)
        if equity > 0.0:
            self._current_equity = equity
        session = session_date or _current_session_date()
        cap = self.resolve_daily_turnover_cap(equity)
        realized = self._load_session_turnover_total(session)
        remaining = max(cap - realized, 0.0)

        if self._session_locked:
            return TurnoverLimitDecision(
                symbol=sym,
                session_date=session,
                proposed_notional=proposed,
                allowed_notional=0.0,
                daily_turnover_so_far=realized,
                cap_notional=cap,
                current_equity=equity,
                capped=True,
                blocked=True,
                breach=True,
                session_locked=True,
                reason=self._breach_reason or "turnover_breach_session_lock",
            )

        if proposed <= 0.0:
            return TurnoverLimitDecision(
                symbol=sym,
                session_date=session,
                proposed_notional=proposed,
                allowed_notional=0.0,
                daily_turnover_so_far=realized,
                cap_notional=cap,
                current_equity=equity,
                capped=False,
                blocked=True,
                breach=False,
                session_locked=False,
                reason="non_positive_proposed_notional",
            )

        if cap <= 0.0:
            return TurnoverLimitDecision(
                symbol=sym,
                session_date=session,
                proposed_notional=proposed,
                allowed_notional=0.0,
                daily_turnover_so_far=realized,
                cap_notional=cap,
                current_equity=equity,
                capped=True,
                blocked=True,
                breach=False,
                session_locked=False,
                reason="equity_unavailable_for_turnover_cap",
            )

        if realized >= cap:
            self._engage_session_breach(
                sym,
                strategy_id=strategy_id,
                realized=realized,
                cap=cap,
                session=session,
                equity=equity,
            )
            return TurnoverLimitDecision(
                symbol=sym,
                session_date=session,
                proposed_notional=proposed,
                allowed_notional=0.0,
                daily_turnover_so_far=realized,
                cap_notional=cap,
                current_equity=equity,
                capped=True,
                blocked=True,
                breach=True,
                session_locked=True,
                reason="daily_turnover_cap_exhausted",
            )

        allowed = min(proposed, remaining)
        capped = allowed < proposed
        blocked = allowed <= 0.0
        reason = "within_cap"
        if capped:
            reason = "proposed_notional_trimmed_to_remaining_cap"

        if capped or blocked:
            self._persist_turnover_check(
                sym,
                session,
                strategy_id=strategy_id,
                proposed=proposed,
                allowed=allowed,
                cumulative=realized + allowed,
                cap=cap,
                equity=equity,
                capped=capped,
                breach=False,
            )

        return TurnoverLimitDecision(
            symbol=sym,
            session_date=session,
            proposed_notional=proposed,
            allowed_notional=allowed,
            daily_turnover_so_far=realized,
            cap_notional=cap,
            current_equity=equity,
            capped=capped,
            blocked=blocked,
            breach=False,
            session_locked=False,
            reason=reason,
        )

    def record_realized_turnover(
        self,
        symbol: str,
        notional: float,
        *,
        strategy_id: str = "",
        side: str = "",
        session_date: str | None = None,
        current_equity: float | None = None,
    ) -> float:
        sym = symbol.upper()
        amount = max(float(notional), 0.0)
        session = session_date or _current_session_date()
        equity = (
            self._current_equity
            if current_equity is None
            else max(float(current_equity), 0.0)
        )
        if current_equity is not None:
            self._current_equity = equity

        if amount <= 0.0:
            return self._load_session_turnover_total(session)

        cap = self.resolve_daily_turnover_cap(equity)
        now = datetime.now(timezone.utc).isoformat()
        prior = self._load_session_turnover_total(session)
        cumulative = prior + amount
        self._session_cache[_session_total_key(session)] = cumulative

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO capacity_daily_totals (
                    session_date, symbol, realized_turnover_notional,
                    cap_notional, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_date, symbol) DO UPDATE SET
                    realized_turnover_notional = excluded.realized_turnover_notional,
                    cap_notional = excluded.cap_notional,
                    updated_at = excluded.updated_at
                """,
                (session, _SESSION_TOTAL_SYMBOL, cumulative, cap, now),
            )
            conn.execute(
                """
                INSERT INTO capacity_turnover_ledger (
                    session_date, symbol, timestamp, strategy_id, side,
                    notional, cumulative_notional, cap_notional, capped, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    session,
                    sym,
                    now,
                    strategy_id,
                    side,
                    amount,
                    cumulative,
                    cap,
                    json.dumps(
                        {
                            "event": "realized_fill",
                            "current_equity": equity,
                            "session_total": cumulative,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )

        if cap > 0.0 and cumulative > cap:
            self._engage_session_breach(
                sym,
                strategy_id=strategy_id,
                realized=cumulative,
                cap=cap,
                session=session,
                equity=equity,
            )
        return cumulative

    def clamp_participation_rate(
        self,
        symbol: str,
        order_size_shares: float,
        trailing_volume_bars: list[float] | np.ndarray,
    ) -> ParticipationClampDecision:
        sym = symbol.upper()
        original = max(float(order_size_shares), 0.0)
        volumes = np.asarray(trailing_volume_bars, dtype=np.float64)
        volumes = volumes[np.isfinite(volumes) & (volumes > 0.0)]

        if original <= 0.0:
            return ParticipationClampDecision(
                symbol=sym,
                original_shares=original,
                clamped_shares=0.0,
                participation_rate=0.0,
                max_participation_rate=self.max_participation_rate,
                reference_bar_volume=0.0,
                clamped=True,
                reason="zero_order_size",
            )

        if volumes.size == 0:
            return ParticipationClampDecision(
                symbol=sym,
                original_shares=original,
                clamped_shares=original,
                participation_rate=0.0,
                max_participation_rate=self.max_participation_rate,
                reference_bar_volume=0.0,
                clamped=False,
                reason="no_volume_profile",
            )

        reference_volume = float(np.median(volumes))
        current_volume = float(volumes[-1])
        bar_volume = max(current_volume, reference_volume, MIN_BAR_VOLUME_SHARES)
        participation_cap = self._resolve_participation_rate(original, bar_volume)
        # Cap is derived from max_participation_rate × bar_volume.  For equity the
        # integer floor is appropriate (whole-share constraint); for fractional assets
        # (crypto) the floor would truncate sub-1.0 orders to 0, so we keep it as a
        # float and let min() decide whether the order fits under the cap.
        max_allowed = bar_volume * self.max_participation_rate
        clamped_shares = min(original, max_allowed)
        realized_rate = clamped_shares / bar_volume if bar_volume > 0 else 0.0
        clamped = clamped_shares < original
        reason = "within_participation_limit"
        if clamped and clamped_shares <= 0.0:
            reason = "participation_limit_blocks_order"
        elif clamped:
            reason = "order_shares_trimmed_to_participation_cap"

        return ParticipationClampDecision(
            symbol=sym,
            original_shares=original,
            clamped_shares=clamped_shares,
            participation_rate=float(realized_rate),
            max_participation_rate=participation_cap,
            reference_bar_volume=bar_volume,
            clamped=clamped,
            reason=reason,
        )

    def _resolve_participation_rate(self, order_shares: float, bar_volume: float) -> float:
        if bar_volume <= 0.0:
            return self.max_participation_rate
        order_fraction = float(order_shares) / bar_volume
        return min(self.max_participation_rate, order_fraction)

    def _engage_session_breach(
        self,
        symbol: str,
        *,
        strategy_id: str,
        realized: float,
        cap: float,
        session: str,
        equity: float,
    ) -> None:
        if self._session_locked:
            return
        self._session_locked = True
        self._breach_reason = "turnover_breach_session_lock"
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO capacity_turnover_ledger (
                    session_date, symbol, timestamp, strategy_id, side,
                    notional, cumulative_notional, cap_notional, capped, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    session,
                    symbol,
                    now,
                    strategy_id,
                    "",
                    max(realized - cap, 0.0),
                    realized,
                    cap,
                    json.dumps(
                        {
                            "event": "turnover_breach",
                            "current_equity": equity,
                            "breach": True,
                            "session_locked": True,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )

    def _load_session_turnover_total(self, session_date: str) -> float:
        cache_key = _session_total_key(session_date)
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT realized_turnover_notional
                FROM capacity_daily_totals
                WHERE session_date = ? AND symbol = ?
                """,
                (session_date, _SESSION_TOTAL_SYMBOL),
            ).fetchone()
        value = float(row[0]) if row is not None else 0.0
        self._session_cache[cache_key] = value
        return value

    def _load_daily_turnover(self, symbol: str, session_date: str) -> float:
        return self._load_session_turnover_total(session_date)

    def calculate_expected_vs_realized_impact(
        self,
        fill_data: Mapping[str, Any],
    ) -> ImpactEstimateResult:
        trade_id = str(fill_data.get("trade_id") or "")
        symbol = str(fill_data.get("symbol") or "").upper()
        qty = max(float(fill_data.get("qty") or 0.0), 0.0)
        side = str(fill_data.get("side") or "")
        trailing_volume = max(float(fill_data.get("trailing_volume") or 0.0), 0.0)

        sqrt_coeff, linear_coeff = self._load_impact_coefficients(symbol)
        expected_sqrt = _estimate_sqrt_impact_bps(qty, trailing_volume, sqrt_coeff)
        expected_linear = _estimate_linear_impact_bps(qty, trailing_volume, linear_coeff)
        expected_impact_bps = _blend_impact_estimates(
            expected_sqrt,
            expected_linear,
            sqrt_coeff,
            linear_coeff,
        )
        model_used = _select_impact_model(sqrt_coeff, linear_coeff)

        realized_impact_bps = _resolve_realized_impact_bps(fill_data)
        residual = realized_impact_bps - expected_impact_bps
        adjustment = 0.0
        if expected_impact_bps > 1e-6:
            ratio = realized_impact_bps / expected_impact_bps
            adjustment = IMPACT_COEFFICIENT_EMA_ALPHA * (ratio - 1.0)
            sqrt_coeff = max(0.01, sqrt_coeff * (1.0 + adjustment))
            linear_coeff = max(0.01, linear_coeff * (1.0 + adjustment))
            self._persist_impact_coefficients(symbol, sqrt_coeff, linear_coeff)

        result = ImpactEstimateResult(
            trade_id=trade_id,
            symbol=symbol,
            expected_impact_bps=expected_impact_bps,
            realized_impact_bps=realized_impact_bps,
            impact_residual_bps=residual,
            model_used=model_used,
            sqrt_coefficient=sqrt_coeff,
            linear_coefficient=linear_coeff,
            coefficient_adjustment=adjustment,
            trailing_volume=trailing_volume,
        )
        self._persist_impact_estimate(fill_data, result)
        return result

    def _persist_turnover_check(
        self,
        symbol: str,
        session_date: str,
        *,
        strategy_id: str,
        proposed: float,
        allowed: float,
        cumulative: float,
        cap: float,
        equity: float,
        capped: bool,
        breach: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO capacity_turnover_ledger (
                    session_date, symbol, timestamp, strategy_id, side,
                    notional, cumulative_notional, cap_notional, capped, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_date,
                    symbol,
                    now,
                    strategy_id,
                    "",
                    proposed - allowed,
                    cumulative,
                    cap,
                    1 if capped else 0,
                    json.dumps(
                        {
                            "event": "pre_trade_cap_check",
                            "proposed": proposed,
                            "allowed": allowed,
                            "current_equity": equity,
                            "breach": breach,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )

    def _load_impact_coefficients(self, symbol: str) -> tuple[float, float]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT sqrt_coefficient, linear_coefficient
                FROM market_impact_coefficients
                WHERE symbol = ?
                """,
                (symbol.upper(),),
            ).fetchone()
        if row is None:
            return DEFAULT_SQRT_IMPACT_COEFFICIENT, DEFAULT_LINEAR_IMPACT_COEFFICIENT
        return float(row[0]), float(row[1])

    def _persist_impact_coefficients(
        self,
        symbol: str,
        sqrt_coeff: float,
        linear_coeff: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO market_impact_coefficients (
                    symbol, sqrt_coefficient, linear_coefficient,
                    sample_count, updated_at
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    sqrt_coefficient = excluded.sqrt_coefficient,
                    linear_coefficient = excluded.linear_coefficient,
                    sample_count = market_impact_coefficients.sample_count + 1,
                    updated_at = excluded.updated_at
                """,
                (symbol.upper(), sqrt_coeff, linear_coeff, now),
            )

    def _persist_impact_estimate(
        self,
        fill_data: Mapping[str, Any],
        result: ImpactEstimateResult,
    ) -> None:
        timestamp = fill_data.get("timestamp")
        if isinstance(timestamp, datetime):
            ts = timestamp.astimezone(timezone.utc).isoformat()
        else:
            ts = str(timestamp or datetime.now(timezone.utc).isoformat())

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO market_impact_ledger (
                    trade_id, timestamp, symbol, qty, side,
                    expected_impact_bps, realized_impact_bps, impact_residual_bps,
                    sqrt_coefficient, linear_coefficient, trailing_volume,
                    model_used, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.trade_id,
                    ts,
                    result.symbol,
                    float(fill_data.get("qty") or 0.0),
                    str(fill_data.get("side") or ""),
                    result.expected_impact_bps,
                    result.realized_impact_bps,
                    result.impact_residual_bps,
                    result.sqrt_coefficient,
                    result.linear_coefficient,
                    result.trailing_volume,
                    result.model_used.value,
                    json.dumps(dict(fill_data.get("metadata") or {}), separators=(",", ":")),
                ),
            )


def ensure_capacity_governor_schema(db_path: Path = RESEARCH_VAULT_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            CAPACITY_TURNOVER_LEDGER_DDL
            + CAPACITY_DAILY_TOTALS_DDL
            + MARKET_IMPACT_LEDGER_DDL
            + IMPACT_COEFFICIENT_STATE_DDL
        )


def _current_session_date() -> str:
    return datetime.now(timezone.utc).astimezone(ET).date().isoformat()


_SESSION_TOTAL_SYMBOL = "__SESSION_TOTAL__"


def _session_total_key(session_date: str) -> str:
    return f"{session_date}:{_SESSION_TOTAL_SYMBOL}"


def _estimate_sqrt_impact_bps(
    qty: float,
    trailing_volume: float,
    sqrt_coeff: float,
) -> float:
    volume = max(trailing_volume, MIN_BAR_VOLUME_SHARES)
    participation = qty / volume
    return float(sqrt_coeff * np.sqrt(participation) * 10_000.0)


def _estimate_linear_impact_bps(
    qty: float,
    trailing_volume: float,
    linear_coeff: float,
) -> float:
    volume = max(trailing_volume, MIN_BAR_VOLUME_SHARES)
    participation = qty / volume
    return float(linear_coeff * participation * 10_000.0)


def _blend_impact_estimates(
    sqrt_bps: float,
    linear_bps: float,
    sqrt_coeff: float,
    linear_coeff: float,
) -> float:
    total = sqrt_coeff + linear_coeff
    if total <= 0.0:
        return 0.5 * (sqrt_bps + linear_bps)
    sqrt_weight = sqrt_coeff / total
    return sqrt_weight * sqrt_bps + (1.0 - sqrt_weight) * linear_bps


def _select_impact_model(sqrt_coeff: float, linear_coeff: float) -> ImpactModel:
    if abs(sqrt_coeff - linear_coeff) < 1e-6:
        return ImpactModel.BLENDED
    if sqrt_coeff >= linear_coeff:
        return ImpactModel.SQRT
    return ImpactModel.LINEAR


def _resolve_realized_impact_bps(fill_data: Mapping[str, Any]) -> float:
    for key in ("markout_5m", "markout_1m", "realized_slippage_bps"):
        if fill_data.get(key) is not None:
            return float(fill_data[key])

    expected_price = float(fill_data.get("expected_price") or 0.0)
    filled_price = float(fill_data.get("filled_price") or 0.0)
    if expected_price > 0.0 and filled_price > 0.0:
        side = str(fill_data.get("side") or "buy").lower()
        if side == "buy":
            slip = (filled_price - expected_price) / expected_price
        else:
            slip = (expected_price - filled_price) / expected_price
        return slip * 10_000.0
    return 0.0
