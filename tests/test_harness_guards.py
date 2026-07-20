"""Unit tests for the root-conftest interpreter guard's discrimination logic.

The guard itself (pytest_configure) can't be exercised from inside a run it would have aborted, so we
test the pure `_is_canonical_interpreter(prefix)` helper with synthetic prefixes, plus a positive
assertion that THIS run is canonical (the guard let it start).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_ROOT_CONFTEST = Path(__file__).resolve().parent.parent / "conftest.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("_root_conftest_under_test", _ROOT_CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guard_accepts_venv_rejects_conda_and_bare_name(tmp_path):
    g = _load_guard()
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = x\n")
    assert g._is_canonical_interpreter(venv) is True                  # a real .venv

    conda = tmp_path / "miniconda3"
    conda.mkdir()
    assert g._is_canonical_interpreter(conda) is False                # conda base -> rejected

    fake = tmp_path / "sub" / ".venv"
    fake.mkdir(parents=True)                                          # named .venv but no pyvenv.cfg
    assert g._is_canonical_interpreter(fake) is False


def test_this_run_is_under_the_canonical_venv():
    if os.environ.get("MBAPPE_ALLOW_ANY_INTERPRETER"):
        pytest.skip("interpreter guard bypassed via MBAPPE_ALLOW_ANY_INTERPRETER")
    g = _load_guard()
    assert g._is_canonical_interpreter() is True                      # the guard let this run start
