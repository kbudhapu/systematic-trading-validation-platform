"""E2/R1: import-direction contracts are CI-enforced, and a research runner
produces byte-identical output pre/post the import-hygiene refactor (golden file).
Behavior-preserving proof for the scripts extraction + bootstrap/backup moves."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import numpy as np

from scripts.chained_backtest import bootstrap_sharpe_ci

# Golden output recorded at the E2 commit (math unchanged by the refactor).
_GOLDEN = {
    "sharpe_point": 1.03641795, "sharpe_ci_lower": -0.89226144,
    "sharpe_ci_upper": 2.96237172, "lag1_autocorr": -0.00568165,
}


def test_research_runner_output_is_byte_identical_golden() -> None:
    """A tiny deterministic research computation (via the extracted/silent
    chained_backtest) matches the recorded golden -- the extraction changed no
    numbers."""
    rng = np.random.default_rng(20260706)
    returns = rng.normal(0.004, 0.02, 60).tolist()
    res = bootstrap_sharpe_ci(returns, n_resamples=500, seed=7)
    got = {
        "sharpe_point": round(res["sharpe"]["point"], 8),
        "sharpe_ci_lower": round(res["sharpe"]["ci_lower"], 8),
        "sharpe_ci_upper": round(res["sharpe"]["ci_upper"], 8),
        "lag1_autocorr": round(res["lag1_autocorr"], 8),
    }
    assert got == _GOLDEN, f"research output drifted from golden: {got}"


def test_chained_backtest_import_is_silent() -> None:
    """Importing chained_backtest must be side-effect-free (no [grid] banners) --
    spawn-pool workers and tests import it repeatedly."""
    out = subprocess.run(
        [sys.executable, "-c", "import scripts.chained_backtest"],
        capture_output=True, text=True, timeout=120)
    assert "[grid]" not in out.stdout, f"banner leaked on import: {out.stdout[:200]}"


def _resolve_guard_binary(name: str) -> str | None:
    """Locate an external guard binary WITHOUT depending on PATH.

    A bare PATH lookup made this guard fail spuriously whenever pytest was invoked as
    `.venv/Scripts/python.exe -m pytest` (the venv's Scripts dir is not on PATH in that mode),
    which is indistinguishable at a glance from the binary genuinely being uninstalled. Resolving
    next to sys.executable first makes the guard RUN whenever it CAN run, so a failure means the
    contracts are actually broken or the tool is actually missing — never an invocation artifact."""
    import shutil
    scripts_dir = pathlib.Path(sys.executable).parent
    for cand in (scripts_dir / f"{name}.exe", scripts_dir / name):
        if cand.exists():
            return str(cand)
    return shutil.which(name)


def test_import_direction_contracts_hold() -> None:
    """import-linter contracts (scripts<-src only; live!->research; strategies!->
    broker) are enforced, not convention.

    FAIL-NOT-SKIP (R1): if the `lint-imports` binary is absent this test FAILS loudly naming the
    package. A guard that cannot run must SAY SO at gate time. On 2026-07-19 import-linter was
    silently removed by an ad-hoc pip install, and this guard failed with an opaque
    FileNotFoundError -- the architectural contract was unenforced and the reason was unreadable."""
    binary = _resolve_guard_binary("lint-imports")
    assert binary is not None, (
        "GUARD CANNOT RUN: the `lint-imports` binary was not found next to sys.executable "
        f"({pathlib.Path(sys.executable).parent}) nor on PATH.\n"
        "It is provided by the `import-linter` package, which is pinned in requirements.lock.\n"
        "Restore with: .venv/Scripts/python -m pip install -r requirements.lock\n"
        "This is a HARD FAILURE by design: an architectural guard that cannot execute must never "
        "pass or skip silently -- the import-direction contracts would be unenforced.")
    result = subprocess.run([binary], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, f"import contracts broken:\n{result.stdout}\n{result.stderr}"
    assert "Contracts: 3 kept, 0 broken" in result.stdout
