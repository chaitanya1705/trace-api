"""Dev launcher: python -m app.serve [--port 8000] [--reload-less]

On Windows uvicorn builds its own ProactorEventLoop, which psycopg's async mode cannot use. This runs the server on a
SelectorEventLoop instead. On Linux (prod) just run: uvicorn app.main:app_factory --factory --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    server = uvicorn.Server(uvicorn.Config("app.main:app_factory", factory=True, host=a.host, port=a.port, loop="none"))
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
