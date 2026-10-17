"""Container entry: supervises the API and the worker as separate processes in one container (architecture A1/A7).

    python -m app.run_all

Each child is restarted with a short backoff if it dies; SIGTERM/SIGINT stop both cleanly. The API listens on $PORT (default 8000).
Splitting the worker into its own deployment later needs no code change: run `python -m app.worker_main` there instead.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

BACKOFF_SECONDS = 3.0


def commands(port: int) -> dict[str, list[str]]:
    py = sys.executable
    api = [py, "-m", "app.serve", "--host", "0.0.0.0", "--port", str(port)] if sys.platform == "win32" else [
        py, "-m", "uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", str(port), "--proxy-headers", "--forwarded-allow-ips", "*"]
    return {"api": api, "worker": [py, "-m", "app.worker_main"]}


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    procs: dict[str, subprocess.Popen] = {}
    stopping = False

    def stop(*_a) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    cmds = commands(port)
    last_start: dict[str, float] = {}
    while not stopping:
        for name, cmd in cmds.items():
            p = procs.get(name)
            if p is None or p.poll() is not None:
                if p is not None:
                    print(f"[supervisor] {name} exited with {p.returncode}; restarting", flush=True)
                    time.sleep(max(0.0, BACKOFF_SECONDS - (time.time() - last_start.get(name, 0))))
                procs[name] = subprocess.Popen(cmd)
                last_start[name] = time.time()
        time.sleep(1.0)
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 20
    for p in procs.values():
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
