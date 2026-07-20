from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from src.config import DB_PATH

log = structlog.get_logger()


@dataclass(frozen=True)
class CycleMetricsPayload:
    phase_a_ms: float
    phase_b_ms: float
    phase_c_ms: float
    total_cycle_ms: float
    sieve_backlog_qty: int


class CycleMetricsExporter:
    def export_cycle_metrics(
        self,
        payload: CycleMetricsPayload,
        *,
        db_path: Path = DB_PATH,
    ) -> None:
        log.info(
            "telemetry_snapshot: CYCLE_METRICS",
            phase_a_ms=float(payload.phase_a_ms),
            phase_b_ms=float(payload.phase_b_ms),
            phase_c_ms=float(payload.phase_c_ms),
            total_cycle_ms=float(payload.total_cycle_ms),
            sieve_backlog_qty=int(payload.sieve_backlog_qty),
        )
        from src.persistence.db_queue import enqueue_cycle_metrics

        enqueue_cycle_metrics(
            {
                "phase_a_ms": float(payload.phase_a_ms),
                "phase_b_ms": float(payload.phase_b_ms),
                "phase_c_ms": float(payload.phase_c_ms),
                "total_cycle_ms": float(payload.total_cycle_ms),
                "sieve_backlog_qty": int(payload.sieve_backlog_qty),
            },
            db_path=str(db_path),
        )
