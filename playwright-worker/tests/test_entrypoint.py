"""
The container start contract (CLAUDE.md → Playwright container start) — one entrypoint
for both WORKER_MODEs. Xvfb → wait for the socket → exec python as pid 1, so a worker
crash surfaces as CrashLoopBackOff instead of a healthy-looking container.
"""

from pathlib import Path

_ENTRYPOINT = Path(__file__).parent.parent / "entrypoint.sh"


def _commands() -> list[str]:
    lines = (line.strip() for line in _ENTRYPOINT.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_ends_by_exec_ing_the_worker_as_pid_1():
    assert _commands()[-1] == "exec python -m worker.main"


def test_never_wraps_the_worker_in_xvfb_run():
    assert not any("xvfb-run" in line for line in _commands())


def test_waits_for_the_x_socket_before_the_exec():
    commands = _commands()
    wait = next(i for i, line in enumerate(commands) if "/tmp/.X11-unix/X" in line)
    assert wait < len(commands) - 1
