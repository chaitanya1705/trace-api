"""The container supervisor restarts a crashed child and stops cleanly on a signal (run in a subprocess so signals are safe)."""
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import signal, sys, threading
    import app.run_all as r

    py = sys.executable
    r.commands = lambda port: {
        "api": [py, "-c", "import time; time.sleep(60)"],      # stays up
        "worker": [py, "-c", "import sys; sys.exit(3)"],       # crashes immediately
    }
    r.BACKOFF_SECONDS = 0.3
    threading.Timer(4.0, lambda: signal.raise_signal(signal.SIGINT)).start()
    raise SystemExit(r.main())
    """
)


def test_crashed_child_is_restarted_and_signal_stops_everything():
    p = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=40)
    assert p.returncode == 0
    restarts = [line for line in p.stdout.splitlines() if "worker exited with 3; restarting" in line]
    assert len(restarts) >= 3                                   # kept coming back, with backoff
    assert not any("api exited" in line for line in p.stdout.splitlines())  # the healthy child was left alone


def test_commands_use_the_platform_port_and_both_processes():
    from app.run_all import commands

    c = commands(9123)
    assert set(c) == {"api", "worker"} and "9123" in c["api"] and c["worker"][-1] == "app.worker_main"
