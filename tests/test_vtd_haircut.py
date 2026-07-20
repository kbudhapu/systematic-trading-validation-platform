"""VTD Task 2 -- Harvey-Liu haircut calibration. Pins the worked example
(SR 0.75 annual, T=240 monthly, N=200 -> haircut ~0.32 / ~58%) and the
nonlinearity of the haircut in the raw Sharpe, and checks the ledger wiring."""
from __future__ import annotations

import numpy as np

from src.research.vtd.haircut import (
    pvalue_to_sr, report_haircut, report_haircut_for_leg, sr_to_pvalue,
)
from src.research.psd.trial_ledger import TrialLedgerEntry, append_trial_sync


def test_worked_example_bonferroni_band() -> None:
    """Calibration (e): the doctrine's worked example lands at haircut SR
    ~0.32-0.33 (~57-60% haircut) under Bonferroni."""
    rep = report_haircut(0.75, T=240, N=200, periods_per_year=12)
    hc = rep["haircut_sr"]["bonferroni"]
    pct = rep["haircut_pct"]["bonferroni"]
    print(f"\nworked example: p_single={rep['p_single']:.2e} "
          f"Bonferroni haircut SR={hc:.4f} ({pct * 100:.1f}% haircut)")
    assert 0.30 <= hc <= 0.34, f"haircut SR out of band: {hc:.4f}"
    assert 0.55 <= pct <= 0.61, f"haircut pct out of band: {pct:.3f}"


def test_reports_all_three_methods_and_N() -> None:
    rep = report_haircut(0.75, T=240, N=200, periods_per_year=12)
    assert set(rep["haircut_sr"]) == {"bonferroni", "holm", "bhy"}
    assert set(rep["adjusted_p"]) == {"bonferroni", "holm", "bhy"}
    assert rep["N"] == 200
    # BHY (arbitrary-dependence, c(N) constant) is at least as severe as Bonferroni
    assert rep["haircut_sr"]["bhy"] <= rep["haircut_sr"]["bonferroni"] + 1e-9
    # every haircut Sharpe is below the raw Sharpe
    for m in ("bonferroni", "holm", "bhy"):
        assert rep["haircut_sr"][m] <= 0.75


def test_haircut_nonlinear_in_sharpe() -> None:
    """Calibration (f): a marginal edge (SR 0.4) takes a LARGER % haircut than a
    strong edge (SR 1.5) at the same N -- the haircut is nonlinear."""
    weak = report_haircut(0.4, T=240, N=200, periods_per_year=12)["haircut_pct"]["bonferroni"]
    strong = report_haircut(1.5, T=240, N=200, periods_per_year=12)["haircut_pct"]["bonferroni"]
    print(f"\nhaircut%: SR0.4={weak * 100:.1f}%  SR1.5={strong * 100:.1f}%")
    assert weak > strong, "marginal edge must take a larger % haircut than a strong one"


def test_bigger_N_bigger_haircut() -> None:
    """More trials -> larger multiplicity penalty -> smaller haircut Sharpe."""
    small = report_haircut(0.75, T=240, N=10, periods_per_year=12)["haircut_sr"]["bonferroni"]
    big = report_haircut(0.75, T=240, N=1000, periods_per_year=12)["haircut_sr"]["bonferroni"]
    assert big < small, f"N=1000 should haircut more than N=10: {big:.4f} !< {small:.4f}"


def test_sr_pvalue_roundtrip() -> None:
    """sr_to_pvalue and pvalue_to_sr are inverses for a positive Sharpe."""
    for sr_period in (0.05, 0.1, 0.2, 0.3):
        p = sr_to_pvalue(sr_period, T=240)
        back = pvalue_to_sr(p, T=240)
        assert abs(back - sr_period) < 1e-6, f"roundtrip failed at {sr_period}: {back}"


def test_zero_edge_gives_near_total_haircut() -> None:
    rep = report_haircut(0.02, T=240, N=500, periods_per_year=12)
    assert rep["haircut_pct"]["bonferroni"] > 0.8


def test_report_haircut_for_leg_pulls_N_from_ledger(tmp_path) -> None:
    """report_haircut_for_leg ties the multiplicity N to the trial ledger's
    cumulative trial count for the leg."""
    db = tmp_path / "ledger.db"
    # 100 grid points x 2 timeframes = 200 trials, matching the worked example
    append_trial_sync(
        TrialLedgerEntry("qqq", "q1", grid_points_evaluated=100, timeframes_evaluated=2), db)
    rep = report_haircut_for_leg("qqq", sr_annual=0.75, T=240, db_path=db, periods_per_year=12)
    assert rep["N"] == 200 and rep["leg_id"] == "qqq"
    assert 0.30 <= rep["haircut_sr"]["bonferroni"] <= 0.34


def test_empty_ledger_defaults_N_to_one(tmp_path) -> None:
    """No recorded trials -> N floored at 1 (no multiplicity penalty), so the
    haircut Sharpe equals the raw Sharpe within rounding."""
    db = tmp_path / "ledger.db"
    rep = report_haircut_for_leg("never_traded", sr_annual=0.75, T=240, db_path=db, periods_per_year=12)
    assert rep["N"] == 1
    assert np.isclose(rep["haircut_sr"]["bonferroni"], 0.75, atol=1e-6)
