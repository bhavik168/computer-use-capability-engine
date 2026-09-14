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


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _bump_patch(version: str) -> str:
    major, minor, patch = _version_tuple(version)
    return f"{major}.{minor}.{patch + 1}"
