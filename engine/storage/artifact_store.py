"""File-backed Artifact Store.

One JSON file per capability under `data/artifacts/`. A database would buy nothing here:
writes happen once per successful discovery run, reads happen once per replay, and a plain
file is reviewable in a pull request, which is exactly what a capability library wants.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from engine.schema.artifact import Artifact

log = logging.getLogger(__name__)

DEFAULT_ROOT = Path("data/artifacts")


class ArtifactStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, capability_id: str) -> Path:
        return self.root / f"{capability_id}.json"

    def save(self, artifact: Artifact) -> Path:
        """Write an artifact, never overwriting a stored version with an older one.

        Policy: on a collision where the incoming version is not strictly newer, the patch
        version is **auto-bumped** past what is stored rather than raising. Recording is
        the tail end of a successful, possibly slow LLM run; making the caller re-run it to
        satisfy a version rule would throw away real work for a bookkeeping reason. The
        bump is logged, and the previous version is preserved alongside.
        """
        # Validate before writing: invalid JSON must never reach the store.
        artifact = Artifact.model_validate(artifact.model_dump())

        path = self.path_for(artifact.capability_id)
        if path.exists():
            existing = Artifact.model_validate(json.loads(path.read_text()))
            if _version_tuple(artifact.version) <= _version_tuple(existing.version):
                bumped = _bump_patch(existing.version)
                log.warning(
                    "artifact %s v%s would not supersede stored v%s; bumping to v%s",
                    artifact.capability_id,
                    artifact.version,
                    existing.version,
                    bumped,
                )
                artifact = artifact.model_copy(update={"version": bumped})
            archive = self.root / "versions" / f"{existing.capability_id}-{existing.version}.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(json.dumps(existing.model_dump(), indent=2))

        path.write_text(json.dumps(artifact.model_dump(), indent=2) + "\n")
        log.info("wrote artifact %s v%s to %s", artifact.capability_id, artifact.version, path)
        return path

    def load(self, capability_id: str) -> Artifact:
        path = self.path_for(capability_id)
        if not path.exists():
            raise FileNotFoundError(
                f"no artifact {capability_id!r} in {self.root}; run discovery for it first"
            )
        return Artifact.model_validate(json.loads(path.read_text()))

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def get_latest(self, capability_id: str) -> Artifact | None:
        """The stored artifact for a capability, or None if there isn't one yet.

        `load` raising is right for a caller that named a capability and meant it. The
        intent planner is asking a different question — "is this already recorded, or
        does it need discovering?" — where absence is an expected answer rather than an
        error, so it gets an expression instead of a try/except at every call site.
        """
        try:
            return self.load(capability_id)
        except FileNotFoundError:
            return None

    def list_capabilities(self) -> list[dict]:
        """The catalog the intent parser matches a plain-language goal against.

        Deliberately thin: id, description and parameter names are what decide whether
        a goal is already covered. Handing the model whole artifacts would put recorded
        locators and step sequences in front of it for a question that does not turn on
        them, and would grow the prompt with every capability ever recorded.
        """
        catalog = []
        for capability_id in self.list():
            artifact = self.load(capability_id)
            catalog.append(
                {
                    "capability_id": artifact.capability_id,
                    "description": artifact.description,
                    "input_params": [p.name for p in artifact.input_params],
                    "requires": list(artifact.requires),
                }
            )
        return catalog


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _bump_patch(version: str) -> str:
    major, minor, patch = _version_tuple(version)
    return f"{major}.{minor}.{patch + 1}"
