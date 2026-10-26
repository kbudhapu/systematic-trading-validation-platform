"""
Broker telemetry bridge and pre-trade capital gate feed.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.models import Account
from src.router.risk_manager import (
    API_RATE_LOW_WATERMARK,
    BUYING_POWER_FRAGMENTATION_FLOOR,
    MARGIN_UTILIZATION_CEILING,
    RiskManager,
    compute_buying_power_fragmentation,
    compute_margin_utilization,
)

REJECT_STORM_WINDOW_SECONDS = 120.0
REJECT_STORM_THRESHOLD = 3
MARGIN_ALERT_UTILIZATION = 0.85
EMERGENCY_POSITION_MULT = 0.35
EMERGENCY_BLOCK_ENTRIES_MULT = 0.0
REJECT_STORM_POSITION_MULT = 0.50


@dataclass(frozen=True)
class CapitalGateSnapshot:
    margin_utilization: float
    buying_power_per_leg: float
    api_budget_remaining: int
    connectivity_ok: bool
    reject_count_window: int
    constraint_active: bool
    emergency_active: bool
    position_size_multiplier: float
    block_new_entries: bool
    reason: str


@dataclass(frozen=True)
class EmergencyTelemetrySignal:
    emergency_active: bool
    position_size_multiplier: float
    block_new_entries: bool
    reason: str
    snapshot: CapitalGateSnapshot


@dataclass
class CapitalGate:
    """Pre-trade capital and broker health gate."""

    risk_manager: RiskManager
    leg_count: int = 1
    connectivity_ok: bool = True
    reject_timestamps: deque[float] = field(
        default_factory=lambda: deque(maxlen=32)
    )
    _emergency_active: bool = False
    _emergency_reason: str = ""
    _position_size_multiplier: float = 1.0
    _block_new_entries: bool = False
    _last_account: Account | None = None

    def reset_cycle(self, account: Account, leg_count: int) -> CapitalGateSnapshot:
        self.leg_count = max(leg_count, 1)
        self._last_account = account
        self.risk_manager.reset_capital_constraint_cycle(account, self.leg_count)
        return self.apply_account_telemetry(
            {
                "event": "account_sync",
                "account": account,
                "connectivity_ok": self.connectivity_ok,
            }
        )

    def apply_account_telemetry(self, broker_update: Mapping[str, Any]) -> CapitalGateSnapshot:
        account = broker_update.get("account")
        if not isinstance(account, Account):
            account = self._last_account
        if isinstance(account, Account):
            margin_util = compute_margin_utilization(account)
            bp_per_leg = compute_buying_power_fragmentation(account, self.leg_count)
        else:
            margin_util = float(broker_update.get("margin_utilization") or 0.0)
            bp_per_leg = float(broker_update.get("buying_power_per_leg") or 1.0)

        connectivity_ok = bool(broker_update.get("connectivity_ok", self.connectivity_ok))
        self.connectivity_ok = connectivity_ok
        event = str(broker_update.get("event") or "")

        capital_snapshot = self.risk_manager._capital_snapshot
        api_remaining = (
            capital_snapshot.api_budget_remaining
            if capital_snapshot is not None
            else 0
        )

        reasons: list[str] = []
        constraint_active = False

        if not connectivity_ok:
            reasons.append("connectivity_down")
            constraint_active = True
        if margin_util >= MARGIN_UTILIZATION_CEILING:
            reasons.append("margin_utilization")
            constraint_active = True
        if margin_util >= MARGIN_ALERT_UTILIZATION:
            reasons.append("margin_alert")
            constraint_active = True
            self._activate_emergency(
                reason="margin_alert",
                position_mult=EMERGENCY_POSITION_MULT,
                block_entries=False,
            )
        if bp_per_leg < BUYING_POWER_FRAGMENTATION_FLOOR:
            reasons.append("buying_power_fragmentation")
            constraint_active = True
        if api_remaining <= API_RATE_LOW_WATERMARK:
            reasons.append("api_rate_limit")
            constraint_active = True

        reject_count = self._prune_reject_window()
        if reject_count >= REJECT_STORM_THRESHOLD:
            reasons.append("reject_storm")
            constraint_active = True
            self._activate_emergency(
                reason="reject_storm",
                position_mult=REJECT_STORM_POSITION_MULT,
                block_entries=True,
            )
        elif (
            event == "account_sync"
            and margin_util < MARGIN_ALERT_UTILIZATION
            and reject_count < REJECT_STORM_THRESHOLD
            and self._emergency_active
            and self._emergency_reason in {"margin_alert", "reject_storm"}
        ):
            self._emergency_active = False
            self._position_size_multiplier = 1.0
            self._block_new_entries = False
            self._emergency_reason = ""
        elif event != "order_reject" and not self._emergency_active:
            self._position_size_multiplier = 1.0
            self._block_new_entries = False
            self._emergency_reason = ""

        if constraint_active and not self._emergency_active:
            self._position_size_multiplier = min(self._position_size_multiplier, 0.75)

        return CapitalGateSnapshot(
            margin_utilization=margin_util,
            buying_power_per_leg=bp_per_leg,
            api_budget_remaining=api_remaining,
            connectivity_ok=connectivity_ok,
            reject_count_window=reject_count,
            constraint_active=constraint_active or self._emergency_active,
            emergency_active=self._emergency_active,
            position_size_multiplier=self._position_size_multiplier,
            block_new_entries=self._block_new_entries,
            reason="|".join(reasons),
        )

    def note_order_reject(self, reason_code: str = "") -> CapitalGateSnapshot:
        self.reject_timestamps.append(time.monotonic())
        snapshot = self.apply_account_telemetry(
            {
                "event": "order_reject",
                "connectivity_ok": self.connectivity_ok,
                "reject_reason": reason_code,
            }
        )
        return snapshot

    def apply_emergency_to_max_position(self, base_max_position_pct: float) -> float:
        if self._block_new_entries:
            return base_max_position_pct * EMERGENCY_BLOCK_ENTRIES_MULT
        return base_max_position_pct * self._position_size_multiplier

    def override_strategy_params(self, strategy_params: dict[str, Any]) -> dict[str, Any]:
        if not self._emergency_active and self._position_size_multiplier >= 1.0:
            return strategy_params
        adjusted = dict(strategy_params)
        base_pct = float(adjusted.get("max_position_pct", 0.95))
        adjusted["max_position_pct"] = self.apply_emergency_to_max_position(base_pct)
        adjusted["capital_gate_emergency"] = self._emergency_active
        adjusted["capital_gate_reason"] = self._emergency_reason
        return adjusted

    def snapshot(self) -> CapitalGateSnapshot:
        capital_snapshot = self.risk_manager._capital_snapshot
        margin_util = (
            capital_snapshot.margin_utilization if capital_snapshot is not None else 0.0
        )
        bp_per_leg = (
            capital_snapshot.buying_power_per_leg if capital_snapshot is not None else 1.0
        )
        api_remaining = (
            capital_snapshot.api_budget_remaining if capital_snapshot is not None else 0
        )
        return CapitalGateSnapshot(
            margin_utilization=margin_util,
            buying_power_per_leg=bp_per_leg,
            api_budget_remaining=api_remaining,
            connectivity_ok=self.connectivity_ok,
            reject_count_window=self._prune_reject_window(),
            constraint_active=self._emergency_active,
            emergency_active=self._emergency_active,
            position_size_multiplier=self._position_size_multiplier,
            block_new_entries=self._block_new_entries,
            reason=self._emergency_reason,
        )

    def _activate_emergency(
        self,
        *,
        reason: str,
        position_mult: float,
        block_entries: bool,
    ) -> None:
        self._emergency_active = True
        self._emergency_reason = reason
        self._position_size_multiplier = min(self._position_size_multiplier, position_mult)
        self._block_new_entries = self._block_new_entries or block_entries

    def _prune_reject_window(self) -> int:
        now = time.monotonic()
        cutoff = now - REJECT_STORM_WINDOW_SECONDS
        while self.reject_timestamps and self.reject_timestamps[0] < cutoff:
            self.reject_timestamps.popleft()
        return len(self.reject_timestamps)


@dataclass
class BrokerTelemetryBridge:
    """Captures broker connectivity, margin, and reject telemetry."""

    capital_gate: CapitalGate

    def ingest_connectivity(self, *, connected: bool) -> CapitalGateSnapshot:
        self.capital_gate.connectivity_ok = connected
        return self.capital_gate.apply_account_telemetry(
            {"event": "connectivity", "connectivity_ok": connected}
        )

    def ingest_account(self, account: Account, *, leg_count: int) -> CapitalGateSnapshot:
        self.capital_gate.leg_count = max(leg_count, 1)
        return self.capital_gate.reset_cycle(account, leg_count)

    def ingest_order_reject(self, reject_payload: Mapping[str, Any]) -> EmergencyTelemetrySignal:
        reason = str(
            reject_payload.get("reason")
            or reject_payload.get("reject_reason")
            or "order_rejected"
        )
        snapshot = self.capital_gate.note_order_reject(reason)
        return self._to_emergency_signal(snapshot)

    def feed_capital_gate_telemetry(
        self,
        broker_update: Mapping[str, Any],
    ) -> EmergencyTelemetrySignal:
        event = str(broker_update.get("event") or "telemetry")

        if event == "order_reject":
            return self.ingest_order_reject(broker_update)

        if event == "connectivity":
            snapshot = self.ingest_connectivity(
                connected=bool(broker_update.get("connectivity_ok", True))
            )
            return self._to_emergency_signal(snapshot)

        account = broker_update.get("account")
        if isinstance(account, Account):
            leg_count = int(broker_update.get("leg_count") or self.capital_gate.leg_count)
            snapshot = self.ingest_account(account, leg_count=leg_count)
            return self._to_emergency_signal(snapshot)

        snapshot = self.capital_gate.apply_account_telemetry(broker_update)
        return self._to_emergency_signal(snapshot)

    def _to_emergency_signal(self, snapshot: CapitalGateSnapshot) -> EmergencyTelemetrySignal:
        return EmergencyTelemetrySignal(
            emergency_active=snapshot.emergency_active,
            position_size_multiplier=snapshot.position_size_multiplier,
            block_new_entries=snapshot.block_new_entries,
            reason=snapshot.reason,
            snapshot=snapshot,
        )
