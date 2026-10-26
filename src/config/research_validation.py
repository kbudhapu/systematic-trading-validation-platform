from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import DATA_DIR

RESEARCH_VALIDATION_PATH = DATA_DIR / "validate_production_24m.json"
DEFAULT_MIN_BORROW_DRAG_COEFFICIENT = 0.005


@dataclass(frozen=True)
class ResearchValidationRecord:
    symbol: str
    params: dict[str, Any]
    constraints: dict[str, Any]


def _parse_constraints(entry: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("constraints")
    if isinstance(raw, dict):
        min_borrow = raw.get("min_borrow_drag_coefficient", DEFAULT_MIN_BORROW_DRAG_COEFFICIENT)
        return {
            "regime_filter": bool(raw.get("regime_filter", False)),
            "allow_short": bool(raw.get("allow_short", False)),
            "min_borrow_drag_coefficient": float(min_borrow),
            "cold_start_policy": str(
                raw.get("cold_start_policy", "FAIL_SAFE_CLOSE")
            ).strip().upper(),
        }
    return {
        "regime_filter": True,
        "allow_short": "short_threshold_sigma" in params,
        "min_borrow_drag_coefficient": DEFAULT_MIN_BORROW_DRAG_COEFFICIENT,
        "cold_start_policy": "FAIL_SAFE_CLOSE",
    }


def load_research_validation_index(
    path: Path | None = None,
) -> dict[str, ResearchValidationRecord]:
    validation_path = path or RESEARCH_VALIDATION_PATH
    if not validation_path.exists():
        return {}
    with validation_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return {}
    records: dict[str, ResearchValidationRecord] = {}
    for symbol, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        params = entry.get("params")
        if not isinstance(params, dict):
            continue
        symbol_key = str(entry.get("symbol", symbol)).upper()
        records[symbol_key] = ResearchValidationRecord(
            symbol=symbol_key,
            params=dict(params),
            constraints=_parse_constraints(entry, params),
        )
    return records


def require_research_validation_row(
    symbol: str,
    *,
    path: Path | None = None,
) -> ResearchValidationRecord:
    index = load_research_validation_index(path)
    symbol_key = str(symbol).upper()
    record = index.get(symbol_key)
    if record is None:
        raise ValueError(
            f"symbol {symbol_key} has no research validation row in "
            f"{path or RESEARCH_VALIDATION_PATH}"
        )
    return record
