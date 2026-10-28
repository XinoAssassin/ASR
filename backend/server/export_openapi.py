"""Write the OpenAPI document to ``backend/openapi.json`` (input of ``pnpm gen:api``).

Usage: ``uv run python -m server.export_openapi [output-path]``. Output is stable: sorted keys,
two-space indent, trailing newline, independent of ``.env`` files.
"""

import json
import sys
from pathlib import Path

from server.app import create_app
from server.config import BACKEND_DIR, load_env

DEFAULT_OUTPUT = BACKEND_DIR / "openapi.json"


def render_openapi() -> str:
    app = create_app(load_env(env_files=()))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    out = Path(args[0]) if args else DEFAULT_OUTPUT
    out.write_text(render_openapi(), encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
