"""E7/R7 (F13): splitting the main.py command bodies into src/cli/commands.py must
not change the CLI surface. This drives the real entrypoint and asserts every
subcommand choice and every option flag still appears in `--help`, so a future
refactor that drops or renames part of the surface is caught.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMANDS = [
    "backtest", "sweep", "run", "bod-email", "eod-email", "maintenance",
    "maintenance-once", "bootstrap", "governance-snapshot", "governance-kill",
    "governance-release", "governance-post-mortem", "operator-report",
]
OPTIONS = [
    "--job-id", "--force-clean-sweep", "--no-auto-seed", "--kill-level",
    "--scope-key", "--operator", "--rationale", "--event-id", "--output",
]


@pytest.fixture(scope="module")
def help_text() -> str:
    env = {**os.environ, "COLUMNS": "80"}
    proc = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_all_subcommand_choices_present(help_text: str) -> None:
    for command in COMMANDS:
        assert command in help_text, f"missing subcommand: {command}"


def test_all_option_flags_present(help_text: str) -> None:
    for option in OPTIONS:
        assert option in help_text, f"missing option: {option}"


def test_description_and_usage_banner(help_text: str) -> None:
    assert "Trading Bot V1" in help_text
    assert help_text.lstrip().startswith("usage: main.py")


def test_commands_importable_from_cli_module() -> None:
    """The handlers actually live in the extracted module now."""
    from src.cli import commands
    for name in ("cmd_backtest", "cmd_run", "cmd_maintenance_once",
                 "cmd_governance_kill", "cmd_bootstrap", "_install_uvloop"):
        assert hasattr(commands, name), name
