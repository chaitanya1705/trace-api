"""Write the OpenAPI document that trace-web generates its TypeScript types from (A5).

    python -m app.openapi_export            # rewrite docs/openapi.json
    python -m app.openapi_export --check    # CI drift check: non-zero exit if the committed file is stale
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import Settings
from app.main import create_app

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def render() -> str:
    app = create_app(Settings(trace_env="test"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            raise SystemExit("docs/openapi.json is out of date; run: python -m app.openapi_export")
        print("openapi.json is current")
    else:
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT}")
