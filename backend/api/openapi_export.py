"""Write the API's OpenAPI schema, for generating the frontend's TypeScript types.

    python -m backend.api.openapi_export frontend/openapi.json
    cd frontend && npm run gen:api

CI regenerates both files and fails if they differ from what is committed, so
the UI's types can't drift from the API.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    # Importing the app reads settings and creates the data directory; keep
    # both away from the developer's real .env and data/.
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="trade-terminal-openapi-"))
    os.environ.setdefault("TRADE_TERMINAL_ENV_FILE", "")

    from backend.api.main import app

    schema = app.openapi()
    text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(argv) > 1:
        Path(argv[1]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
