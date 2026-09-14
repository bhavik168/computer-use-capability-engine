"""Loads `.env` into the process environment, once, before anything reads it.

Configuration is read from the environment rather than from a config file the engine parses
itself, so the same code runs unchanged whether values come from a developer's `.env`, an
exported shell variable, or a secrets manager injecting them into a container. The `.env`
file is a local convenience only, and is never the source of truth.

Nothing here ever *returns* a secret — callers read `os.environ` themselves at the point of
use, so a credential is never held on an object that might end up in a log line or a
traceback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env(path: Path | str = ENV_PATH, override: bool = False) -> bool:
    """Populate `os.environ` from a `.env` file. Returns whether one was found.

    An already-exported variable wins by default: a value the operator set deliberately in
    their shell should not be silently replaced by a stale file on disk.
    """
    path = Path(path)
    if not path.exists():
        return False

    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or (not value and key in os.environ):
            continue
        if override or key not in os.environ:
            os.environ[key] = value

    log.debug("loaded environment from %s", path)
    return True
