"""
Cooperative engine preemption and uniform hierarchical risk escalation.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

import structlog

if TYPE_CHECKING:
    from src.control.command_queue import ControlCommand

log = structlog.get_logger()


class RiskEscalationLevel(str, Enum):
    NOMINAL = "NOMINAL"
    ENTRY_GATE_HALT = "ENTRY_GATE_HALT"
    STRATEGY_LIQUIDATE = "STRATEGY_LIQUIDATE"
    GLOBAL_FLATTEN_AND_HALT = "GLOBAL_FLATTEN_AND_HALT"

    def preempts_in_flight_cycle(self) -> bool:
        return self in {
            RiskEscalationLevel.STRATEGY_LIQUIDATE,
            RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
        }

    def commands_portfolio_liquidation(self) -> bool:
        return self == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT


@dataclass(frozen=True)
class RiskEscalationSnapshot:
    level: RiskEscalationLevel
    scope_key: str = "GLOBAL"
    strategy_id: str | None = None
    symbol: str | None = None
    portfolio_liquidation_commanded: bool = False
    engine_shutdown_latched: bool = False
    # R2 (INCIDENT-20260722 FINDING-7): who commanded the current level. The heartbeat watchdog may
    # auto-de-escalate ONLY a level it itself commanded (never an operator/kill/liquidation level).
    commanded_by: str = "system"


@dataclass(frozen=True)
class PreemptionCheckpoint:
    phase: str
    step: str
    strategy_id: str | None = None
    symbol: str | None = None


def parse_escalation_level(payload: Mapping[str, Any]) -> RiskEscalationLevel:
    raw = payload.get("escalation_level")
    if raw is None:
        raise ValueError("escalation_level is required")
    normalized = str(raw).strip().upper()
    return RiskEscalationLevel(normalized)


class RiskEscalationEngine:
    """Thread-safe uniform risk escalation state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot = RiskEscalationSnapshot(level=RiskEscalationLevel.NOMINAL)

    def snapshot(self) -> RiskEscalationSnapshot:
        with self._lock:
            return self._snapshot

    def transition(
        self,
        new_level: RiskEscalationLevel,
        *,
        scope_key: str = "GLOBAL",
        strategy_id: str | None = None,
        symbol: str | None = None,
        commanded_by: str = "system",
    ) -> RiskEscalationSnapshot:
        with self._lock:
            prior = self._snapshot
            portfolio_liquidation = new_level.commands_portfolio_liquidation()
            engine_shutdown = prior.engine_shutdown_latched
            if new_level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT:
                engine_shutdown = True
            self._snapshot = RiskEscalationSnapshot(
                level=new_level,
                scope_key=str(scope_key or "GLOBAL"),
                strategy_id=str(strategy_id) if strategy_id else None,
                symbol=str(symbol).upper() if symbol else None,
                portfolio_liquidation_commanded=portfolio_liquidation,
                engine_shutdown_latched=engine_shutdown,
                commanded_by=str(commanded_by or "system"),
            )
            updated = self._snapshot
        log.critical(
            "emergency_interrupt: ESCALATION_LEVEL_CHANGED",
            prior_level=prior.level.value,
            new_level=updated.level.value,
            prior_scope_key=prior.scope_key,
            new_scope_key=updated.scope_key,
            strategy_id=updated.strategy_id,
            symbol=updated.symbol,
            portfolio_liquidation_commanded=updated.portfolio_liquidation_commanded,
            engine_shutdown_latched=updated.engine_shutdown_latched,
            commanded_by=commanded_by,
        )
        return updated

    def is_engine_shutdown_latched(self) -> bool:
        return self.snapshot().engine_shutdown_latched

    def should_flatten_portfolio(self) -> bool:
        snap = self.snapshot()
        return (
            snap.level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT
            or snap.portfolio_liquidation_commanded
        )

    def blocks_entries_for_strategy(self, strategy_id: str) -> bool:
        snap = self.snapshot()
        if snap.level == RiskEscalationLevel.NOMINAL:
            return False
        if snap.level == RiskEscalationLevel.STRATEGY_LIQUIDATE:
            return snap.strategy_id == strategy_id
        return snap.level in {
            RiskEscalationLevel.ENTRY_GATE_HALT,
            RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
        }

    def blocks_all_entries(self) -> bool:
        snap = self.snapshot()
        return snap.level in {
            RiskEscalationLevel.ENTRY_GATE_HALT,
            RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
        }

    def requires_preemption(
        self,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> bool:
        snap = self.snapshot()
        if snap.level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT:
            return True
        if snap.level != RiskEscalationLevel.STRATEGY_LIQUIDATE:
            return False
        if strategy_id and snap.strategy_id and strategy_id == snap.strategy_id:
            return True
        if symbol and snap.symbol and symbol.upper() == snap.symbol:
            return True
        if snap.strategy_id is None and snap.symbol is None:
            return True
        return False


class EnginePreemptedException(Exception):
    """Raised when an armed escalation level aborts an in-flight orchestrator cycle."""

    def __init__(
        self,
        checkpoint: PreemptionCheckpoint,
        *,
        escalation_level: RiskEscalationLevel,
        command_id: str | None = None,
        command_type: str | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self.escalation_level = escalation_level
        self.command_id = command_id
        self.command_type = command_type
        super().__init__(
            f"engine preempted at {checkpoint.phase}:{checkpoint.step} "
            f"level={escalation_level.value}"
        )


class EnginePreemptionLatch:
    """Thread-safe escalation latch with asyncio cycle wake support."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._escalation = RiskEscalationEngine()
        self._preemption_armed = threading.Event()
        self._pending_command: ControlCommand | None = None
        self._checkpoint = PreemptionCheckpoint(phase="idle", step="start")
        self._cycle_wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def escalation(self) -> RiskEscalationEngine:
        return self._escalation

    def bind_cycle_wake(
        self,
        event: asyncio.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        with self._lock:
            self._cycle_wake = event
            self._loop = loop

    def arm_command(
        self,
        command: ControlCommand,
        level: RiskEscalationLevel,
        *,
        scope_key: str = "GLOBAL",
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        self._escalation.transition(
            level,
            scope_key=scope_key,
            strategy_id=strategy_id,
            symbol=symbol,
            commanded_by=command.command_type.value,
        )
        with self._lock:
            self._pending_command = command
        if level.preempts_in_flight_cycle() or level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT:
            self._preemption_armed.set()
        self.wake_cycle()

    def arm_flatten_and_halt(self, command: ControlCommand) -> None:
        self.arm_command(
            command,
            RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
            scope_key="GLOBAL",
        )

    def wake_cycle(self) -> None:
        with self._lock:
            wake = self._cycle_wake
            loop = self._loop
        if wake is None or loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(wake.set)

    def is_preemption_armed(self) -> bool:
        return self._preemption_armed.is_set()

    def is_flatten_halt_armed(self) -> bool:
        return self.is_preemption_armed()

    def should_preempt(
        self,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> bool:
        if not self.is_preemption_armed():
            return False
        return self._escalation.requires_preemption(
            strategy_id=strategy_id,
            symbol=symbol,
        )

    def set_checkpoint(
        self,
        phase: str,
        step: str,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        with self._lock:
            self._checkpoint = PreemptionCheckpoint(
                phase=phase,
                step=step,
                strategy_id=strategy_id,
                symbol=symbol,
            )

    def snapshot_checkpoint(self) -> PreemptionCheckpoint:
        with self._lock:
            return self._checkpoint

    def take_pending_command(self) -> ControlCommand | None:
        with self._lock:
            command = self._pending_command
            self._pending_command = None
            return command

    def peek_pending_command(self) -> ControlCommand | None:
        with self._lock:
            return self._pending_command

    def clear_preemption_arm(self) -> None:
        self._preemption_armed.clear()
        with self._lock:
            self._pending_command = None
            self._checkpoint = PreemptionCheckpoint(phase="idle", step="cleared")

    def clear(self) -> None:
        self.clear_preemption_arm()
