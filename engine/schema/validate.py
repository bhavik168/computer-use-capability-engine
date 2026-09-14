"""Validate an artifact JSON file against the schema.

    python -m engine.schema.validate <path-to-json>

Exits 0 and prints `OK: <capability_id> v<version>` when the file is a valid artifact;
exits 1 and names the failing field otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from engine.schema.artifact import Artifact


def validate_file(path: Path) -> int:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"FAIL: no such file: {path}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path} is not valid JSON: {exc}")
        return 1

    try:
        artifact = Artifact.model_validate(raw)
    except ValidationError as exc:
        print(f"FAIL: {path} is not a valid artifact")
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            print(f"  {location}: {error['msg']}")
        return 1

    print(f"OK: {artifact.capability_id} v{artifact.version} ({artifact.risk_class})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m engine.schema.validate <path-to-json>")
        return 1
    return validate_file(Path(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
