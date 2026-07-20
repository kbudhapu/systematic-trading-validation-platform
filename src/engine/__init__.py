"""Engine package — avoid eager orchestrator import for fast unit-test collection."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.engine.orchestrator import TradingOrchestrator

__all__ = ["TradingOrchestrator"]


def __getattr__(name: str):
    if name == "TradingOrchestrator":
        from src.engine.orchestrator import TradingOrchestrator

        return TradingOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
