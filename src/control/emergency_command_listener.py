"""
Sub-minute emergency command interrupt — polls the command bus outside the 60s cycle tick.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import structlog

from src.control.command_queue import (
    ControlCommand,
    ControlCommandType,
    fetch_and_claim_emergency_commands,
    mark_command_failed,
)
from src.engine.engine_preemption import RiskEscalationLevel

if TYPE_CHECKING:
    from src.engine.orchestrator import TradingOrchestrator

log = structlog.get_logger()

EMERGENCY_COMMAND_POLL_SECONDS = 0.2


class EmergencyCommandInterruptListener:
    """
    Ultra-lightweight background poller that claims and dispatches emergency commands
    immediately via the orchestrator's asyncio event loop.
    """

    def __init__(self, orchestrator: TradingOrchestrator) -> None:
        self._orchestrator = orchestrator
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.is_running:
            return
        self._loop = loop
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="emergency-command-interrupt",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "emergency_command_interrupt_started",
            poll_seconds=EMERGENCY_COMMAND_POLL_SECONDS,
        )

    def stop(self, *, timeout_seconds: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(timeout_seconds, 0.1))
            self._thread = None
        log.info("emergency_command_interrupt_stopped")

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                claimed = fetch_and_claim_emergency_commands()
                for command in claimed:
                    self._dispatch_command(command)
            except Exception as exc:
                log.warning("emergency_command_poll_failed", error=str(exc))
            self._stop_event.wait(EMERGENCY_COMMAND_POLL_SECONDS)

    def _dispatch_command(self, command: ControlCommand) -> None:
        """Dispatch one claimed emergency command.

        TERMINAL-MARKING OWNERSHIP (K1): for FLATTEN_AND_HALT and a preempting
        ENGAGE_KILL_SWITCH this method arms the preemption latch and RETURNS EARLY --
        it does NOT terminal-mark. The single owner of terminal-marking for those is
        the cycle-preemption handler (`orchestrator._handle_cycle_preemption`), which
        completes the command by id after the protective action durably succeeds
        (complete-after-durable-success). For all other command types the async
        `_execute_claimed_control_command` path is the owner. No command is executed
        without exactly one terminal-marking owner; a crash before that owner marks it
        leaves the command in `processing`, re-fired once by the boot reclaim.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            mark_command_failed(command.command_id, "orchestrator event loop unavailable")
            return
        log.critical(
            "emergency_command_interrupt",
            command_id=command.command_id,
            command_type=command.command_type.value,
        )
        self._orchestrator.note_emergency_cycle_interrupt(command)
        if command.command_type == ControlCommandType.FLATTEN_AND_HALT:
            return
        if command.command_type == ControlCommandType.ENGAGE_KILL_SWITCH:
            try:
                from src.engine.engine_preemption import parse_escalation_level

                level = parse_escalation_level(command.payload or {})
            except ValueError:
                level = None
            if level is not None and (
                level.preempts_in_flight_cycle()
                or level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT
            ):
                return
        future = asyncio.run_coroutine_threadsafe(
            self._orchestrator._execute_claimed_control_command(command),
            loop,
        )

        def _log_dispatch_result(done: asyncio.Future[None]) -> None:
            try:
                done.result()
            except Exception as exc:
                log.error(
                    "emergency_command_dispatch_failed",
                    command_id=command.command_id,
                    error=str(exc),
                )

        future.add_done_callback(_log_dispatch_result)
