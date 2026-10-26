"""Liveness watchdogs for the long-running services that currently have none (P3.2).

Confirmed blind spots (droplet forensic 2026-07-12):
  * ``mbappe-soak`` writes no heartbeat at all — it could die silently.
  * the mirror refresh (P2) has no liveness signal.

These factories build ``HeartbeatWatchdog`` instances for those components, following
the existing ``component="main_loop"`` pattern WITHOUT touching that instance or its
tests. Each new watchdog is given the shipped composite notifier (durable log row +
email page).

Two escalation policies, deliberately different:
  * ``soak`` — the paper/live trading loop. A stale soak heartbeat during RTH is a real
    trading-safety event, so it uses the real RiskEscalationEngine (block-new-entries),
    exactly like main_loop.
  * ``mirror_sync`` — an OBSERVABILITY signal (dashboard/Ryzen mirror freshness). A
    lagging mirror must ALERT but MUST NOT halt trading: halting live trading because a
    read-only mirror is behind is precisely the false-P0 over-reaction that motivated
    this whole work (a stale mirror was mistaken for a dead collector for five turns).
    So it is wired to :class:`NullEscalation` — notify-only.

The outcome resolver deliberately gets NO watchdog: P4 confirmed it does not exist as a
scheduled job (no resolver function, no caller, no timer). You cannot monitor the
liveness of a job that has no code — a heartbeat for it would be a permanent false red.
"""

from __future__ import annotations

from pathlib import Path

from src.control.heartbeat_watchdog import HeartbeatWatchdog, Notifier
from src.engine.engine_preemption import RiskEscalationEngine

# soak cycles ~every 60s (paper_soak poll_seconds=60); 2x = 120s before it's "stale".
SOAK_STALE_THRESHOLD_S = 120.0
# the mirror is a coarser, lower-frequency refresh; a short lag is normal. Page only
# when it is far enough behind to matter (well beyond any intraday refresh).
MIRROR_SYNC_STALE_THRESHOLD_S = 3600.0


class NullEscalation:
    """No-op escalation for observability-only watchdogs. Satisfies the duck-typed
    ``.transition(...)`` the watchdog calls, and does nothing — so a stale signal
    ALERTS without ever touching the trading risk state."""

    def transition(self, *args, **kwargs) -> None:  # noqa: D401 - intentional no-op
        return None


def build_soak_watchdog(
    *,
    db_path: str | Path,
    escalation: RiskEscalationEngine,
    notifier: Notifier,
    stale_threshold_seconds: float = SOAK_STALE_THRESHOLD_S,
    environment: str = "paper",
    auto_deescalation_enabled: bool = True,
) -> HeartbeatWatchdog:
    """Watchdog for the ``soak`` trading loop — real escalation (block-new-entries).

    R2 (INCIDENT-20260722 FINDING-7): `environment` gates bounded auto-de-escalation of a
    watchdog-commanded ENTRY_GATE_HALT — active in paper, always OFF in live."""
    return HeartbeatWatchdog(
        db_path=db_path,
        escalation=escalation,
        notifier=notifier,
        stale_threshold_seconds=stale_threshold_seconds,
        component="soak",
        environment=environment,
        auto_deescalation_enabled=auto_deescalation_enabled,
    )


def build_mirror_sync_watchdog(
    *,
    db_path: str | Path,
    notifier: Notifier,
    stale_threshold_seconds: float = MIRROR_SYNC_STALE_THRESHOLD_S,
) -> HeartbeatWatchdog:
    """Watchdog for the ``mirror_sync`` refresh — notify-only (NullEscalation), never
    halts trading. A lagging mirror is an observability alert, not a P0."""
    return HeartbeatWatchdog(
        db_path=db_path,
        escalation=NullEscalation(),  # type: ignore[arg-type]  # duck-typed .transition
        notifier=notifier,
        stale_threshold_seconds=stale_threshold_seconds,
        component="mirror_sync",
    )
