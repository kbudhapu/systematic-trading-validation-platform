"""Leg lifecycle state machine (LLD section 1).

Seven states on a one-directional ladder for promotions and an always-available
demotion path. Only LEGAL transitions are permitted; the operator may demote at
any time (any state -> SAFE_MODE / RETIRED) but may NEVER promote past a gate
(promotions into VALIDATED / PAPER / ACTIVE are gate-driven, not operator fiat).
Every transition emits a DiagnosticReport (the durable record, via the existing
writer -- LLD section 1 / VTD section 4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class LifecycleState(str, Enum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    SAFE_MODE = "SAFE_MODE"
    RETIRED = "RETIRED"


# SAFE_MODE and RETIRED are reachable from every non-terminal state (the operator
# kill switch outranks everything). Promotions move toward deployment and are
# gate-driven only.
LEGAL_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.CANDIDATE: {LifecycleState.VALIDATED, LifecycleState.SAFE_MODE, LifecycleState.RETIRED},
    LifecycleState.VALIDATED: {LifecycleState.PAPER, LifecycleState.SAFE_MODE, LifecycleState.RETIRED},
    LifecycleState.PAPER: {LifecycleState.ACTIVE, LifecycleState.SAFE_MODE, LifecycleState.RETIRED},
    LifecycleState.ACTIVE: {LifecycleState.WATCH, LifecycleState.SAFE_MODE, LifecycleState.RETIRED},
    LifecycleState.WATCH: {LifecycleState.ACTIVE, LifecycleState.SAFE_MODE, LifecycleState.RETIRED},
    LifecycleState.SAFE_MODE: {LifecycleState.RETIRED},  # revival only as a NEW leg version (LLD 5)
    LifecycleState.RETIRED: set(),
}

# transitions INTO these are promotions (gate-driven; operator cannot force them)
PROMOTION_TARGETS = {LifecycleState.VALIDATED, LifecycleState.PAPER, LifecycleState.ACTIVE}

# WATCH applies a x0.5 sizing factor; all trading states otherwise full size
_SIZING = {LifecycleState.WATCH: 0.5}


def sizing_factor(state: LifecycleState) -> float:
    """Position-sizing multiplier for a state (WATCH -> 0.5, per LLD section 4)."""
    return _SIZING.get(state, 1.0)


class IllegalTransition(Exception):
    pass


@dataclass
class TransitionRecord:
    leg_id: str
    from_state: LifecycleState
    to_state: LifecycleState
    reason: str
    actor: str


class LifecycleStateMachine:
    """Per-leg lifecycle state with legal-transition enforcement + DiagnosticReport
    emission on every change."""

    def __init__(
        self,
        leg_id: str,
        *,
        state: LifecycleState = LifecycleState.CANDIDATE,
        report_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self.leg_id = leg_id
        self.state = state
        self._report_sink = report_sink or (lambda _r: None)
        self._safe_mode_episodes = 0

    @property
    def safe_mode_episodes(self) -> int:
        return self._safe_mode_episodes

    def can_transition(self, to_state: LifecycleState, *, actor: str) -> bool:
        if to_state not in LEGAL_TRANSITIONS.get(self.state, set()):
            return False
        if actor == "operator" and to_state in PROMOTION_TARGETS:
            return False   # operator may never promote past a gate
        return True

    def transition(self, to_state: LifecycleState, *, reason: str, actor: str = "system") -> TransitionRecord:
        if to_state == self.state:
            raise IllegalTransition(f"{self.leg_id}: already in {to_state.value}")
        if not self.can_transition(to_state, actor=actor):
            raise IllegalTransition(
                f"{self.leg_id}: illegal transition {self.state.value} -> {to_state.value} "
                f"(actor={actor})")
        record = TransitionRecord(self.leg_id, self.state, to_state, reason, actor)
        if to_state == LifecycleState.SAFE_MODE:
            self._safe_mode_episodes += 1
        self.state = to_state
        # the DiagnosticReport IS the durable transition record (via the writer)
        self._report_sink({
            "kind": "lifecycle_transition", "leg_id": self.leg_id,
            "from_state": record.from_state.value, "to_state": to_state.value,
            "reason": reason, "actor": actor,
            "safe_mode_episodes": self._safe_mode_episodes,
        })
        return record
