from __future__ import annotations

from pathlib import Path

import structlog

from src.engine.metrics_exporter import CycleMetricsExporter, CycleMetricsPayload
from src.persistence.cycle_metrics_store import load_recent_cycle_metrics
from src.persistence.db_queue import get_async_db_writer, stop_async_db_writer


def test_export_cycle_metrics_emits_telemetry_signature() -> None:
    events: list[dict] = []

    def _capture(_logger, _method_name, event_dict):
        events.append(dict(event_dict))
        return event_dict

    structlog.configure(
        processors=[_capture, structlog.processors.KeyValueRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    exporter = CycleMetricsExporter()
    exporter.export_cycle_metrics(
        CycleMetricsPayload(
            phase_a_ms=12.5,
            phase_b_ms=8.0,
            phase_c_ms=20.25,
            total_cycle_ms=41.0,
            sieve_backlog_qty=3,
        ),
        db_path=Path("unused.db"),
    )
    matched = [
        event
        for event in events
        if event.get("event") == "telemetry_snapshot: CYCLE_METRICS"
    ]
    assert len(matched) == 1
    row = matched[0]
    assert row["phase_a_ms"] == 12.5
    assert row["phase_b_ms"] == 8.0
    assert row["phase_c_ms"] == 20.25
    assert row["total_cycle_ms"] == 41.0
    assert row["sieve_backlog_qty"] == 3


def test_export_cycle_metrics_persists_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "trading.db"
    exporter = CycleMetricsExporter()
    exporter.export_cycle_metrics(
        CycleMetricsPayload(
            phase_a_ms=5.0,
            phase_b_ms=6.0,
            phase_c_ms=7.0,
            total_cycle_ms=18.0,
            sieve_backlog_qty=2,
        ),
        db_path=db_path,
    )
    writer = get_async_db_writer()
    writer.start()
    writer.stop(timeout_seconds=2.0)
    stop_async_db_writer()

    rows = load_recent_cycle_metrics(limit=5, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["phase_a_ms"] == 5.0
    assert rows[0]["phase_b_ms"] == 6.0
    assert rows[0]["phase_c_ms"] == 7.0
    assert rows[0]["total_cycle_ms"] == 18.0
    assert rows[0]["sieve_backlog_qty"] == 2
