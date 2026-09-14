"""File-backed KB Store — one JSON file per application.

The KB is keyed by *application template*, not by tenant. Two credit unions running the
same vendor console share one map, which is what makes a capability recorded at one
institution usable at another without re-recording it; per-element overrides are the
intended escape hatch where a tenant's install genuinely differs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_ROOT = Path("data/kb")


class ElementRef(BaseModel):
    element_ref_id: str
    role: str
    purpose: str  # the element's accessible name — a label, never a value
    locator: dict
    confidence: float = 0.5
    last_validated_at: str | None = None
    stale: bool = False


class NavEdge(BaseModel):
    from_screen: str
    to_screen: str
    via: str  # accessible name of the control that makes the transition


class Screen(BaseModel):
    # The normalized path, e.g. "/members/{id}?tab=account". There is deliberately no
    # "example_url" alongside it: a concrete URL is a record identifier (/members/10001)
    # and a host, and the KB has no use for either — the normalized form is what makes one
    # screen recognisable across records, tenants and deployments.
    screen_id: str
    elements: list[ElementRef] = Field(default_factory=list)


class LearnedOutcome(BaseModel):
    """An exceptional state this application turned out to have.

    Learned, never declared. The engine cannot know in advance that an application says
    "no member found" — a discovery run only ever walks the success path, so by definition
    it never sees one. These accumulate when a replay meets something it was not recorded
    to handle, a human names it, and the engine writes it down. The next capability recorded
    against the same application inherits them.
    """

    code: str
    type: str
    detector: dict
    message: str | None = None
    recovery: str | None = None
    learned_at: str | None = None
    learned_from: str | None = None  # the run that first hit it


class AppModel(BaseModel):
    app_id: str
    screens: list[Screen] = Field(default_factory=list)
    nav_edges: list[NavEdge] = Field(default_factory=list)
    outcomes: list[LearnedOutcome] = Field(default_factory=list)

    def screen(self, screen_id: str) -> Screen | None:
        return next((s for s in self.screens if s.screen_id == screen_id), None)


def normalize_url(url: str) -> str:
    """Collapse record identifiers so every member's page is recognised as one screen."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path = re.sub(r"/\d+", "/{id}", parsed.path or "/")
    return path + (f"?{parsed.query}" if parsed.query else "")


class KnowledgeBaseStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, app_id: str) -> Path:
        return self.root / f"{app_id}.json"

    def load(self, app_id: str) -> AppModel:
        path = self.path_for(app_id)
        if not path.exists():
            return AppModel(app_id=app_id)
        return AppModel.model_validate(json.loads(path.read_text()))

    def save(self, app_id: str, model: AppModel) -> Path:
        path = self.path_for(app_id)
        path.write_text(json.dumps(model.model_dump(), indent=2) + "\n")
        return path
