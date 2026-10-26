"""Bug-3 follow-up: the paper-soak runner must shut the bot down GRACEFULLY under SIGTERM
so commit-2's WS close logs actually reach the .bot.log / journald.

Root cause fixed here: the runner is the log-forwarding PARENT (child stdout -> PIPE ->
_drain_bot_output -> .bot.log + the runner's own stdout). It had NO SIGTERM handler, so
`systemctl stop` killed it (and the drain) by default disposition, dropping every line the
child emitted during its graceful shutdown -- which is why commit-2 looked dead in prod even
though main.py's handler fires (SigCgt confirmed it installed).

Two properties are tested:
  1. (cross-platform) the drain forwards the child's output to EOF even after ``_stop`` is set
     -- the early ``break`` was the compounding half of the bug.
  2. (POSIX) a REAL SIGTERM to a process that wired the runner exactly as ``main()`` does drives
     the child's graceful close and CAPTURES its shutdown marker in the .bot.log -- the real
     signal path the passing unit tests never exercised.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.paper_soak_runner import PaperSoakRunner

_REPO = Path(__file__).resolve().parents[1]


class _FakeProc:
    """Stands in for the bot subprocess: stdout yields lines then EOFs (StopIteration)."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = iter(lines)


def _make_runner(tmp_path: Path) -> PaperSoakRunner:
    return PaperSoakRunner(
        duration_seconds=1.0,
        poll_seconds=5.0,
        environment="paper",
        bot_command=[sys.executable, "-c", "pass"],
        log_path=tmp_path / "soak.jsonl",
        restart_on_exit=False,
        bot_log_path=tmp_path / "soak.bot.log",
    )


def test_drain_forwards_child_output_until_eof_even_after_stop(tmp_path: Path) -> None:
    """The drain must NOT stop forwarding when _stop is set: the child's graceful-shutdown
    lines arrive AFTER _stop and this thread is their only forwarder. (Regression for the
    early `if self._stop.is_set(): break`.)"""
    runner = _make_runner(tmp_path)
    lines = [
        "cycle_lightweight_pass\n",
        "shutdown_signal_received signal=SIGTERM\n",          # emitted AFTER _stop is set
        "market_data_stream_clock_stopped\n",                  # the commit-2 marker
    ]
    runner._bot_proc = _FakeProc(lines)  # type: ignore[assignment]
    runner._stop.set()                   # shutdown already requested when the lines arrive

    runner._drain_bot_output()

    captured = (tmp_path / "soak.bot.log").read_text(encoding="utf-8")
    assert "shutdown_signal_received" in captured        # NOT cut off by the early break
    assert "market_data_stream_clock_stopped" in captured
    assert "cycle_lightweight_pass" in captured


@pytest.mark.skipif(os.name != "posix", reason="real SIGTERM graceful-shutdown semantics are POSIX-only")
def test_sigterm_triggers_graceful_child_shutdown_and_captures_its_logs(tmp_path: Path) -> None:
    """REAL signal path: a process wiring the runner as main() does (signal.signal(SIGTERM)
    -> request_stop) must, on an actual SIGTERM, reap the child gracefully AND forward the
    child's shutdown marker to the .bot.log. The existing unit tests called shutdown() directly
    and passed while production stayed silent; this exercises the signal delivery itself."""
    stub = tmp_path / "stub_bot.py"
    stub.write_text(
        "import signal, sys, time\n"
        "def _graceful(signum, frame):\n"
        "    print('STUB_CHILD_GRACEFUL_CLOSE', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _graceful)\n"
        "print('STUB_CHILD_STARTED', flush=True)\n"
        "for _ in range(1200):\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    bot_log = tmp_path / "soak.bot.log"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import os, signal, sys\n"
        f"os.environ.setdefault('MBAPPE_DATA_DIR', {str(tmp_path)!r})\n"
        f"sys.path.insert(0, {str(_REPO)!r})\n"
        "from pathlib import Path\n"
        "from scripts.paper_soak_runner import PaperSoakRunner\n"
        "runner = PaperSoakRunner(duration_seconds=120, poll_seconds=5, environment='paper',\n"
        f"    bot_command=[sys.executable, '-u', {str(stub)!r}], log_path=Path({str(tmp_path / 'soak.jsonl')!r}),\n"
        f"    restart_on_exit=False, bot_log_path=Path({str(bot_log)!r}))\n"
        "signal.signal(signal.SIGTERM, lambda s, f: runner.request_stop(reason='SIGTERM'))\n"
        "runner._spawn_bot()\n"
        "while not runner._stop.wait(0.2):\n"
        "    pass\n"
        "runner._stop_bot()\n"
        "if runner._drain_thread is not None:\n"
        "    runner._drain_thread.join(timeout=3.0)\n"
        "print('DRIVER_DONE', flush=True)\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # wait for the stub child to start (its marker is forwarded to the .bot.log)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if bot_log.exists() and "STUB_CHILD_STARTED" in bot_log.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
        else:
            proc.kill()
            pytest.fail(f"stub child never started; driver output:\n{proc.communicate()[0]}")

        # the REAL SIGTERM (proc.terminate() == SIGTERM on POSIX — what systemctl stop sends)
        proc.terminate()
        out, _ = proc.communicate(timeout=30.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, f"driver did not exit cleanly on SIGTERM; output:\n{out}"
    captured = bot_log.read_text(encoding="utf-8")
    assert "STUB_CHILD_GRACEFUL_CLOSE" in captured, (
        "the child's graceful-shutdown marker was NOT forwarded to the .bot.log on SIGTERM — "
        f"the runner did not shut it down gracefully or the drain dropped it.\nbot.log:\n{captured}\n"
        f"driver output:\n{out}"
    )
