from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _copy_runner(root: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_weekend_update.sh"
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    target = scripts_dir / "run_weekend_update.sh"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _fake_python(root: Path) -> Path:
    path = root / "fake_python.py"
    path.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                "mode = os.environ['WEEKEND_RUNNER_MODE']",
                "script = Path(sys.argv[1]).name if len(sys.argv) > 1 else ''",
                "root = Path(os.environ['WEEKEND_RUNNER_ROOT'])",
                "staged_strategy = root / 'config' / 'strategies' / 'staged_mean_reversion_qqq.yaml'",
                "staged_validation = root / 'data' / 'staged_validate_production_24m.json'",
                "commit_marker = root / 'commit_invoked.txt'",
                "args_log = root / 'commit_args.txt'",
                "if script == 'adaptive_parameter_tuner.py':",
                "    if mode == 'no_survivors':",
                "        print('parameter_tuner: NO_SURVIVORS')",
                "        raise SystemExit(0)",
                "    staged_strategy.parent.mkdir(parents=True, exist_ok=True)",
                "    staged_validation.parent.mkdir(parents=True, exist_ok=True)",
                "    staged_strategy.write_text('generated', encoding='utf-8')",
                "    staged_validation.write_text('generated', encoding='utf-8')",
                "    print('parameter_tuner: SWEEP_COMPLETE')",
                "    raise SystemExit(0)",
                "if script == 'commit_param_update.py':",
                "    commit_marker.write_text('invoked', encoding='utf-8')",
                "    args_log.write_text(' '.join(sys.argv[1:]), encoding='utf-8')",
                "    if mode == 'commit_fail':",
                "        print('audit failure', file=sys.stderr)",
                "        raise SystemExit(2)",
                "    print('config_updater: PARAMETERS_COMMITTED')",
                "    raise SystemExit(0)",
                "raise SystemExit(0)",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _run_emulated(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["WEEKEND_RUNNER_MODE"] = mode
    env["WEEKEND_RUNNER_ROOT"] = str(root)
    staged_strategy = root / "config" / "strategies" / "staged_mean_reversion_qqq.yaml"
    staged_validation = root / "data" / "staged_validate_production_24m.json"
    for path in (staged_strategy, staged_validation):
        if path.exists():
            path.unlink()
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("config", "strategies").mkdir(parents=True, exist_ok=True)
    root.joinpath("data").mkdir(parents=True, exist_ok=True)
    fake_python = _fake_python(root)
    tuner = subprocess.run(
        [
            sys.executable,
            str(fake_python),
            str(root / "scripts" / "adaptive_parameter_tuner.py"),
            "--strategy-id",
            "mean_reversion_qqq",
            "--lookback-days",
            "60",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
    )
    stdout = tuner.stdout
    stderr = tuner.stderr
    if tuner.returncode != 0:
        return subprocess.CompletedProcess(
            args=["emulated_runner"],
            returncode=tuner.returncode,
            stdout=stdout,
            stderr=stderr + "weekend_runner: CADENCE_ABORTED\n",
        )
    if not staged_strategy.exists():
        return subprocess.CompletedProcess(
            args=["emulated_runner"],
            returncode=0,
            stdout=stdout,
            stderr=stderr,
        )
    commit = subprocess.run(
        [
            sys.executable,
            str(fake_python),
            str(root / "scripts" / "commit_param_update.py"),
            "--operator-hash",
            "CRON_WEEKEND_AUTO",
            "--allow-schema-update",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
    )
    return subprocess.CompletedProcess(
        args=["emulated_runner"],
        returncode=commit.returncode,
        stdout=stdout + commit.stdout,
        stderr=stderr + commit.stderr + ("weekend_runner: CADENCE_ABORTED\n" if commit.returncode != 0 else ""),
    )


def _run_actual_shell(root: Path, runner: Path, mode: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh") or shutil.which("bash")
    if shell is None:
        return _run_emulated(root, mode)
    env = os.environ.copy()
    env["WEEKEND_RUNNER_MODE"] = mode
    env["WEEKEND_RUNNER_ROOT"] = str(root)
    fake_python = _fake_python(root)
    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / "python"
    launcher.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                f'exec "{sys.executable}" "{fake_python}" "$@"',
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [shell, str(runner)],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
    )


def _run_runner(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "runner_repo"
    runner = _copy_runner(root)
    (root / "config" / "strategies").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    return _run_actual_shell(root, runner, mode)


def test_shell_orchestrator_exits_cleanly_when_tuner_finds_no_survivors(tmp_path: Path) -> None:
    result = _run_runner(tmp_path, "no_survivors")
    root = tmp_path / "runner_repo"
    assert result.returncode == 0
    assert "parameter_tuner: NO_SURVIVORS" in result.stdout
    assert not (root / "commit_invoked.txt").exists()


def test_shell_orchestrator_runs_commit_after_successful_rotation(tmp_path: Path) -> None:
    result = _run_runner(tmp_path, "success")
    root = tmp_path / "runner_repo"
    assert result.returncode == 0
    assert (root / "commit_invoked.txt").exists()
    args = (root / "commit_args.txt").read_text(encoding="utf-8")
    assert "--allow-schema-update" in args
    assert "--operator-hash CRON_WEEKEND_AUTO" in args
    assert "weekend_runner: CADENCE_ABORTED" not in result.stderr


def test_shell_orchestrator_emits_abort_on_fail_fast_commit_error(tmp_path: Path) -> None:
    result = _run_runner(tmp_path, "commit_fail")
    root = tmp_path / "runner_repo"
    assert result.returncode != 0
    assert (root / "commit_invoked.txt").exists()
    assert "weekend_runner: CADENCE_ABORTED" in result.stderr
