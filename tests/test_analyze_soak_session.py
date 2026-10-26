from __future__ import annotations

from pathlib import Path

from scripts.analyze_soak_session import (
    CLAMP_ENGAGED_SIGNATURE,
    LATENCY_VIOLATION_SIGNATURE,
    analyze_soak_session,
    render_markdown_report,
    scan_log_signatures,
)
from src.persistence.cycle_metrics_store import (
    ensure_cycle_metrics_schema,
    load_session_cycle_metrics,
    persist_cycle_metrics_row,
)


def _seed_rows(db_path: Path) -> None:
    ensure_cycle_metrics_schema(db_path)
    samples = [
        (10.0, 8.0, 12.0, 35.0, 1),
        (15.0, 9.0, 11.0, 42.0, 2),
        (20.0, 10.0, 14.0, 55.0, 3),
        (12.0, 7.0, 9.0, 30.0, 0),
    ]
    for phase_a, phase_b, phase_c, total, backlog in samples:
        persist_cycle_metrics_row(
            phase_a_ms=phase_a,
            phase_b_ms=phase_b,
            phase_c_ms=phase_c,
            total_cycle_ms=total,
            sieve_backlog_qty=backlog,
            db_path=db_path,
        )


def test_analyze_soak_session_passes_clean_metrics(tmp_path: Path) -> None:
    db_path = tmp_path / "trading.db"
    _seed_rows(db_path)
    rows = load_session_cycle_metrics(db_path=db_path)
    analysis = analyze_soak_session(rows=rows)
    assert analysis.passed is True
    assert analysis.phase_a.peak_ms == 20.0
    assert analysis.max_sieve_backlog_qty == 3
    assert analysis.latency_violation_count == 0


def test_analyze_soak_session_fails_on_latency_and_backlog(tmp_path: Path) -> None:
    db_path = tmp_path / "trading.db"
    ensure_cycle_metrics_schema(db_path)
    persist_cycle_metrics_row(
        phase_a_ms=100.0,
        phase_b_ms=200.0,
        phase_c_ms=800.0,
        total_cycle_ms=1200.0,
        sieve_backlog_qty=6,
        db_path=db_path,
    )
    rows = load_session_cycle_metrics(db_path=db_path)
    analysis = analyze_soak_session(rows=rows)
    assert analysis.passed is False
    assert analysis.latency_violation_count == 1
    assert len(analysis.failures) == 2


def test_scan_log_signatures_counts_events(tmp_path: Path) -> None:
    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "\n".join(
            [
                "slo_verdict: LATENCY_VIOLATION total_cycle_ms=1200",
                "risk_governor: CLAMP_ENGAGED sizing_multipliers={'a': 0.6}",
                "risk_governor: CLAMP_ENGAGED sizing_multipliers={'b': 0.6}",
            ]
        ),
        encoding="utf-8",
    )
    counts = scan_log_signatures(log_path)
    assert counts[LATENCY_VIOLATION_SIGNATURE] == 1
    assert counts[CLAMP_ENGAGED_SIGNATURE] == 2


def test_render_markdown_report_contains_verdict(tmp_path: Path) -> None:
    db_path = tmp_path / "trading.db"
    _seed_rows(db_path)
    from src.persistence.cycle_metrics_store import load_session_cycle_metrics

    analysis = analyze_soak_session(rows=load_session_cycle_metrics(db_path=db_path))
    report = render_markdown_report(analysis, db_path=db_path, log_path=None)
    assert "## Go-Live Verdict: PASS" in report
    assert "Phase Latency Summary" in report
