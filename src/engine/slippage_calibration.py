"""
Asymmetric session slippage calibration — load, resolve, and default multipliers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

SESSION_OPENING_CROSS = "OPENING_CROSS"
SESSION_MIDDAY_DOLDRUMS = "MIDDAY_DOLDRUMS"
SESSION_CLOSING_IMBALANCE = "CLOSING_IMBALANCE"

SESSION_SLIPPAGE_MULTIPLIERS: dict[str, float] = {
    SESSION_OPENING_CROSS: 2.0,
    SESSION_MIDDAY_DOLDRUMS: 1.0,
    SESSION_CLOSING_IMBALANCE: 1.5,
}

EXEC_DIRECTION_LONG_ENTRY = "long_entry"
EXEC_DIRECTION_LONG_EXIT = "long_exit"
EXEC_DIRECTION_SHORT_ENTRY = "short_entry"
EXEC_DIRECTION_SHORT_EXIT = "short_exit"

EXECUTION_DIRECTION_TYPES: tuple[str, ...] = (
    EXEC_DIRECTION_LONG_ENTRY,
    EXEC_DIRECTION_LONG_EXIT,
    EXEC_DIRECTION_SHORT_ENTRY,
    EXEC_DIRECTION_SHORT_EXIT,
)

SESSION_TYPES: tuple[str, ...] = (
    SESSION_OPENING_CROSS,
    SESSION_MIDDAY_DOLDRUMS,
    SESSION_CLOSING_IMBALANCE,
)

STRESS_SESSION_TYPES: tuple[str, ...] = (
    SESSION_OPENING_CROSS,
    SESSION_CLOSING_IMBALANCE,
)

from src.config import DATA_DIR  # single data root (env-aware; test-suite redirected)

DEFAULT_CALIBRATION_PATH = (
    DATA_DIR / "calibration" / "session_slippage_multipliers.json"
)

_EXIT_DIRECTIONS: tuple[str, ...] = (
    EXEC_DIRECTION_LONG_EXIT,
    EXEC_DIRECTION_SHORT_EXIT,
)
_ENTRY_DIRECTIONS: tuple[str, ...] = (
    EXEC_DIRECTION_LONG_ENTRY,
    EXEC_DIRECTION_SHORT_ENTRY,
)


def default_asymmetric_multipliers() -> dict[str, dict[str, float]]:
    """Baseline session × direction multiplier grid from static session defaults."""
    grid: dict[str, dict[str, float]] = {}
    for session, session_mult in SESSION_SLIPPAGE_MULTIPLIERS.items():
        exit_bias = 1.35 if session in STRESS_SESSION_TYPES else 1.15
        entry_bias = 0.90 if session == SESSION_MIDDAY_DOLDRUMS else 1.0
        grid[session] = {
            EXEC_DIRECTION_LONG_ENTRY: session_mult * entry_bias,
            EXEC_DIRECTION_LONG_EXIT: session_mult * exit_bias,
            EXEC_DIRECTION_SHORT_ENTRY: session_mult * entry_bias,
            EXEC_DIRECTION_SHORT_EXIT: session_mult * exit_bias,
        }
    return grid


def flatten_multiplier_map(
    nested: Mapping[str, Mapping[str, float]],
) -> dict[str, float]:
    flat: dict[str, float] = {}
    for session, directions in nested.items():
        for direction, multiplier in directions.items():
            flat[f"{session}::{direction}"] = float(multiplier)
    return flat


def load_asymmetric_slippage_multipliers(
    path: Path | None = None,
) -> dict[str, dict[str, float]]:
    """Load nested session × direction multipliers; fall back to defaults."""
    calibration_path = path or DEFAULT_CALIBRATION_PATH
    defaults = default_asymmetric_multipliers()
    if not calibration_path.is_file():
        return defaults

    try:
        payload = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults

    raw = payload.get("session_slippage_multipliers")
    if not isinstance(raw, dict):
        return defaults

    resolved = {session: dict(directions) for session, directions in defaults.items()}
    for session, directions in raw.items():
        if session not in resolved or not isinstance(directions, dict):
            continue
        for direction in EXECUTION_DIRECTION_TYPES:
            value = directions.get(direction)
            if value is not None:
                resolved[session][direction] = float(value)
    return resolved


def resolve_directional_slippage_multiplier(
    multipliers: Mapping[str, Mapping[str, float]],
    *,
    session: str,
    direction: str,
) -> float:
    session_map = multipliers.get(session, {})
    value = session_map.get(direction)
    if value is not None:
        return float(value)
    return float(SESSION_SLIPPAGE_MULTIPLIERS.get(session, 1.0))


def resolve_directional_slippage_pct(
    base_slippage_pct: float,
    *,
    session: str,
    direction: str,
    multipliers: Mapping[str, Mapping[str, float]] | None = None,
) -> float:
    grid = multipliers or default_asymmetric_multipliers()
    multiplier = resolve_directional_slippage_multiplier(
        grid,
        session=session,
        direction=direction,
    )
    return max(float(base_slippage_pct), 0.0) * multiplier


def estimate_exit_stress_execution_bps(
    base_slippage_pct: float,
    *,
    multipliers: Mapping[str, Mapping[str, float]] | None = None,
    max_bars_in_trade: float = 40.0,
    short_bias: float = 1.0,
) -> float:
    """
    Conservative execution bps estimate penalizing urgent liquidation-heavy configs.

    Weights stressed-session exit multipliers more heavily than passive entries.
    """
    grid = multipliers or load_asymmetric_slippage_multipliers()
    urgency = min(1.0, 25.0 / max(float(max_bars_in_trade), 1.0))

    exit_mults: list[float] = []
    for session in STRESS_SESSION_TYPES:
        for direction in _EXIT_DIRECTIONS:
            exit_mults.append(
                resolve_directional_slippage_multiplier(
                    grid,
                    session=session,
                    direction=direction,
                )
            )
    stress_exit_mult = max(exit_mults) if exit_mults else 1.0

    entry_mults: list[float] = []
    for session in SESSION_TYPES:
        for direction in _ENTRY_DIRECTIONS:
            entry_mults.append(
                resolve_directional_slippage_multiplier(
                    grid,
                    session=session,
                    direction=direction,
                )
            )
    entry_mult = float(sum(entry_mults) / len(entry_mults)) if entry_mults else 1.0

    short_exit_mult = max(
        resolve_directional_slippage_multiplier(
            grid,
            session=SESSION_OPENING_CROSS,
            direction=EXEC_DIRECTION_SHORT_EXIT,
        ),
        resolve_directional_slippage_multiplier(
            grid,
            session=SESSION_CLOSING_IMBALANCE,
            direction=EXEC_DIRECTION_SHORT_EXIT,
        ),
    )
    if short_bias >= 1.05:
        stress_exit_mult = max(stress_exit_mult, short_exit_mult * 1.10)

    blended_multiplier = entry_mult * 0.30 + stress_exit_mult * 0.70
    blended_multiplier *= 1.0 + urgency * 0.60
    return max(float(base_slippage_pct), 0.0) * 10_000.0 * blended_multiplier


def calibration_metadata(path: Path | None = None) -> dict[str, Any]:
    calibration_path = path or DEFAULT_CALIBRATION_PATH
    if not calibration_path.is_file():
        return {"loaded": False, "path": str(calibration_path)}
    try:
        payload = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"loaded": False, "path": str(calibration_path)}
    return {
        "loaded": True,
        "path": str(calibration_path),
        "generated_at": payload.get("generated_at"),
        "base_slippage_pct": payload.get("base_slippage_pct"),
    }
