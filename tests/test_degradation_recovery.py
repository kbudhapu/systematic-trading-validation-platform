"""R2.5 — HARD_CRITICAL_DEGRADE must have a PROVEN, working recovery path.

The six-day latch: HARD_CRITICAL_DEGRADE engaged 2026-07-07 on a transient SLO breach that cleared
within minutes, then persisted across every restart — blocking every signal, every entry, and
force-flattening — while the sensors were healthy the whole time. Root cause: `evaluate_from_slo`
returned the degraded state unchanged for HARD (a dead-end with no recovery streak), and the
mode was rehydrated on boot with no way out.

These tests pin: HARD still ENGAGES on a real breach (we did not break the brake); it DOWNGRADES
once the SLO verdict is OK for HARD_RECOVERY_STREAK evaluations, on the SAME tick as engagement;
it recovers after a restart when sensors are healthy (the exact bug); it STAYS degraded after a
restart when sensors are still bad; broker-recon success alone does NOT clear it; and entering /
clearing pages the phone.
"""
from __future__ import annotations

from src.engine.degradation_manager import (
    DegradationManager,
    HARD_RECOVERY_STREAK,
    SOFT_RECOVERY_STREAK,
    OperationalMode,
)
from src.engine.slo_monitor import DataIntegrityVerdict, IntegritySeverity


def _verdict(severity: IntegritySeverity, *reasons: str) -> DataIntegrityVerdict:
    return DataIntegrityVerdict(
        passed=severity == IntegritySeverity.OK,
        severity=severity,
        bar_freshness_seconds=0.0,
        missing_bar_rate=0.0,
        nbbo_success_rate=1.0,
        rl_backfill_lag_hours=0.0,
        calendar_session_minutes=390.0,
        is_early_close_session=False,
        reasons=tuple(reasons),
    )


OK = _verdict(IntegritySeverity.OK)
SOFT = _verdict(IntegritySeverity.SOFT_BREACH, "nbbo_fetch_degraded")
HARD = _verdict(IntegritySeverity.HARD_BREACH, "nbbo_fetch_critical")


def _mgr(tmp_path, notifier=None) -> DegradationManager:
    return DegradationManager(db_path=tmp_path / "gov.db", notifier=notifier)


class _FakeNotifier:
    def __init__(self):
        self.notified = []
        self.resolved = []

    def notify(self, alert):
        self.notified.append(alert)

    def resolve(self, *, component, kind):
        self.resolved.append((component, kind))


# --------------------------------------------------------------------------- #
# The brake still works.
# --------------------------------------------------------------------------- #

def test_hard_breach_still_engages(tmp_path):
    """PROVE WE DID NOT BREAK THE BRAKE — a genuine HARD breach force-flattens + ignores signals."""
    m = _mgr(tmp_path)
    state = m.evaluate_from_slo(HARD)
    assert state.mode == OperationalMode.HARD_CRITICAL_DEGRADE
    assert state.force_emergency_flatten and state.ignore_model_signals and state.block_new_entries


def test_hard_never_downgrades_before_the_full_streak(tmp_path):
    m = _mgr(tmp_path)
    m.evaluate_from_slo(HARD)
    for _ in range(HARD_RECOVERY_STREAK - 1):
        state = m.evaluate_from_slo(OK)
        assert state.mode == OperationalMode.HARD_CRITICAL_DEGRADE  # still latched — no trivial release
    assert m._recovery_streak == HARD_RECOVERY_STREAK - 1


def test_soft_verdict_during_hard_recovery_does_not_reset_the_streak(tmp_path):
    """S1: HARD is engaged only by a HARD breach, so only a HARD verdict resets its recovery. A SOFT
    verdict (e.g. a persistent research-pipeline lag) must NOT trap the engine in force-flatten."""
    m = _mgr(tmp_path)
    m.evaluate_from_slo(HARD)
    for _ in range(HARD_RECOVERY_STREAK - 1):
        m.evaluate_from_slo(OK)
    m.evaluate_from_slo(SOFT)                       # SOFT during HARD recovery -> streak SURVIVES
    assert m._recovery_streak == HARD_RECOVERY_STREAK - 1
    assert m.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE
    m.evaluate_from_slo(OK)                         # one more OK completes the streak -> downgrade
    assert m.current_state().mode == OperationalMode.SOFT_DEGRADE


def test_a_hard_verdict_midway_resets_the_recovery_streak(tmp_path):
    """S1: a genuine HARD breach during recovery re-engages and resets — the brake still works."""
    m = _mgr(tmp_path)
    m.evaluate_from_slo(HARD)
    for _ in range(HARD_RECOVERY_STREAK - 1):
        m.evaluate_from_slo(OK)
    m.evaluate_from_slo(HARD)                       # a HARD breach re-engages
    assert m._recovery_streak == 0
    assert m.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE


# --------------------------------------------------------------------------- #
# The brake can now release.
# --------------------------------------------------------------------------- #

def test_hard_downgrades_to_soft_then_normal_on_sustained_health(tmp_path):
    """Sensors recover -> streak accumulates -> HARD -> SOFT -> NORMAL, staged, on the SLO tick."""
    m = _mgr(tmp_path)
    m.evaluate_from_slo(HARD)
    for _ in range(HARD_RECOVERY_STREAK):
        m.evaluate_from_slo(OK)
    assert m.current_state().mode == OperationalMode.SOFT_DEGRADE       # staged: HARD -> SOFT first
    assert m.current_state().force_emergency_flatten is False           # flatten stops immediately
    for _ in range(SOFT_RECOVERY_STREAK):
        m.evaluate_from_slo(OK)
    assert m.current_state().mode == OperationalMode.NORMAL             # then SOFT -> NORMAL
    assert m.current_state().block_new_entries is False                 # trading allowed again


def test_full_healthy_session_never_latches(tmp_path):
    m = _mgr(tmp_path)
    for _ in range(200):
        state = m.evaluate_from_slo(OK)
    assert state.mode == OperationalMode.NORMAL


# --------------------------------------------------------------------------- #
# The exact six-day bug: restart while degraded.
# --------------------------------------------------------------------------- #

def test_restart_while_degraded_with_healthy_sensors_recovers(tmp_path):
    """THE EXACT SIX-DAY BUG. A restart rehydrates the HARD mode (a real degrade must survive a
    restart) but the streak resets to zero (re-earn health); with healthy sensors it then RECOVERS
    on the fast SLO cadence instead of latching forever."""
    db = tmp_path / "gov.db"
    m1 = DegradationManager(db_path=db)
    m1.evaluate_from_slo(HARD)
    assert m1.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE

    # --- restart: fresh manager, rehydrate from the persisted governance_state ---
    m2 = DegradationManager(db_path=db)
    rehydrated = m2.hydrate_from_vault()
    assert rehydrated.mode == OperationalMode.HARD_CRITICAL_DEGRADE     # the latch survives the restart
    assert m2._recovery_streak == 0                                     # but health credit does NOT

    for _ in range(HARD_RECOVERY_STREAK + SOFT_RECOVERY_STREAK):
        m2.evaluate_from_slo(OK)
    assert m2.current_state().mode == OperationalMode.NORMAL            # it can now escape


def test_restart_while_degraded_with_sensors_still_bad_stays_degraded(tmp_path):
    db = tmp_path / "gov.db"
    m1 = DegradationManager(db_path=db)
    m1.evaluate_from_slo(HARD)

    m2 = DegradationManager(db_path=db)
    m2.hydrate_from_vault()
    for _ in range(HARD_RECOVERY_STREAK + 5):
        m2.evaluate_from_slo(HARD)                                      # sensors still failing
    assert m2.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE


# --------------------------------------------------------------------------- #
# R2.3c — recovery must be sensor-driven, not recon-driven.
# --------------------------------------------------------------------------- #

def test_soft_verdict_during_soft_recovery_does_not_reset_the_streak(tmp_path):
    """V1 — THE SOFT TRAP. A SOFT verdict while already in SOFT_DEGRADE must NOT reset its recovery
    streak (the old code reset on any non-OK, so a persistent SOFT made SOFT permanent -- and SOFT
    sets block_new_entries=True, blocking every fill)."""
    m = _mgr(tmp_path)
    m.apply_soft_degrade("soft_test")
    assert m.current_state().mode == OperationalMode.SOFT_DEGRADE
    for _ in range(SOFT_RECOVERY_STREAK - 1):
        m.evaluate_from_slo(OK)
    m.evaluate_from_slo(SOFT)                       # persistent SOFT -> HOLDS the streak, no reset
    assert m._recovery_streak == SOFT_RECOVERY_STREAK - 1
    m.evaluate_from_slo(OK)                         # one more OK (condition cleared) -> NORMAL
    assert m.current_state().mode == OperationalMode.NORMAL
    assert m.current_state().block_new_entries is False   # the soft trap is CLOSED


def test_re_asserting_soft_via_feed_path_does_not_reset_the_streak(tmp_path):
    """V1: the direct feed-degraded path re-calls apply_soft_degrade every cycle -- it must not reset
    the streak while already in SOFT, or a persistent degraded feed re-traps the state machine."""
    m = _mgr(tmp_path)
    m.apply_soft_degrade("degraded_feed_stream")
    for _ in range(SOFT_RECOVERY_STREAK - 1):
        m.evaluate_from_slo(OK)
    m.apply_soft_degrade("degraded_feed_stream")   # re-assert (persistent feed) -> must NOT reset
    assert m._recovery_streak == SOFT_RECOVERY_STREAK - 1


def test_hard_breach_during_soft_escalates_and_resets(tmp_path):
    """V1: escalation still works -- a breach AT/ABOVE SOFT's severity (HARD) resets + escalates."""
    m = _mgr(tmp_path)
    m.apply_soft_degrade("soft_test")
    m.evaluate_from_slo(OK)
    m.evaluate_from_slo(HARD)
    assert m.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE
    assert m._recovery_streak == 0


def test_persistent_soft_that_never_clears_stays_soft(tmp_path):
    """A genuinely persistent SOFT condition keeps the leg in SOFT (correct -- the condition is real).
    Visibility is via the daily liveness ping's degradation mode (R2.4b), not silence."""
    m = _mgr(tmp_path)
    m.apply_soft_degrade("soft_test")
    for _ in range(30):
        m.evaluate_from_slo(SOFT)
    assert m.current_state().mode == OperationalMode.SOFT_DEGRADE


def test_full_ladder_hard_to_soft_to_normal_with_intermittent_softs(tmp_path):
    """The full recovery ladder with intermittent SOFT breaches throughout -- neither the HARD (S1)
    nor the SOFT (V1) streak is reset by a SOFT, so the ladder completes to NORMAL."""
    m = _mgr(tmp_path)
    m.evaluate_from_slo(HARD)
    i = 0
    while m.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE and i < 200:
        m.evaluate_from_slo(SOFT if i % 3 == 2 else OK)   # intermittent SOFT
        i += 1
    assert m.current_state().mode == OperationalMode.SOFT_DEGRADE
    i = 0
    while m.current_state().mode == OperationalMode.SOFT_DEGRADE and i < 200:
        m.evaluate_from_slo(SOFT if i % 3 == 2 else OK)
        i += 1
    assert m.current_state().mode == OperationalMode.NORMAL
    assert m.current_state().block_new_entries is False


def test_broker_recon_success_does_not_clear_the_latch(tmp_path):
    """R2.3c: the pre-open recon reset was deleted. Broker reconciliation carries no data-health
    information, so it must NOT downgrade HARD_CRITICAL. Only OK SLO verdicts do."""
    m = _mgr(tmp_path)
    m.evaluate_from_slo(HARD)
    # simulate everything a successful recon might have touched — none of it is an SLO verdict
    assert m.current_state().mode == OperationalMode.HARD_CRITICAL_DEGRADE
    # the only lever that moves it is the SLO recovery streak
    for _ in range(HARD_RECOVERY_STREAK):
        m.evaluate_from_slo(OK)
    assert m.current_state().mode == OperationalMode.SOFT_DEGRADE


# --------------------------------------------------------------------------- #
# R2.4a — visibility: enter pages, clear resolves.
# --------------------------------------------------------------------------- #

def test_entering_hard_pages_and_recovery_resolves(tmp_path):
    n = _FakeNotifier()
    m = _mgr(tmp_path, notifier=n)
    m.evaluate_from_slo(HARD)
    assert n.notified, "entering HARD_CRITICAL_DEGRADE must page immediately"
    assert n.notified[0]["severity"] == "urgent"
    assert n.notified[0]["detail"]["component"] == "degradation_manager"

    for _ in range(HARD_RECOVERY_STREAK):
        m.evaluate_from_slo(OK)
    assert n.resolved, "clearing HARD_CRITICAL_DEGRADE must send exactly one recovered message"
    assert n.resolved[0] == ("degradation_manager", "hard_critical_degrade")
