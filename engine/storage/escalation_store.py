"""File-backed store for intervention requests, under `data/escalations/`."""

from __future__ import annotations

import json
from pathlib import Path

from engine.escalation.models import InterventionRequest

DEFAULT_ROOT = Path("data/escalations")


class EscalationStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, request: InterventionRequest) -> Path:
        path = self.root / f"{request.request_id}.json"
        path.write_text(json.dumps(request.model_dump(mode="json"), indent=2) + "\n")
        return path

    def load(self, request_id: str) -> InterventionRequest:
        path = self.root / f"{request_id}.json"
        return InterventionRequest.model_validate(json.loads(path.read_text()))

    def list_pending(self) -> list[InterventionRequest]:
        requests = [
            InterventionRequest.model_validate(json.loads(path.read_text()))
            for path in sorted(self.root.glob("*.json"))
        ]
        return [request for request in requests if request.status == "pending"]
