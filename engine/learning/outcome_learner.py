"""Turning an unexpected state into a known one.

This is the loop that makes the system get better at an application over time without
anyone describing that application to it in advance.

    replay meets something it was never recorded to handle
      → hard failure, with a clear account of what was expected and what was observed
      → escalation to a human, carrying generalised candidate detectors
      → the human names it ("that's permission_denied, it's a business outcome")
      → the Knowledge Base remembers it for this application
      → every capability recorded against that application afterwards inherits it,
        and existing ones can be taught it individually.

The first time an application refuses something, a hard failure is the *correct* answer:
the system has genuinely never seen that state and has no basis for calling it routine.
Pre-declaring the answer would make the first run look better and teach the system nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from urllib.parse import urlparse

from engine.knowledge_base.service import KnowledgeBaseService
from engine.reporting.redaction import (
    MAX_CANDIDATE,
    MIN_CANDIDATE,
    MIN_URL_CANDIDATE,
    stable_fragment,
)
from engine.schema.artifact import KnownOutcome
from engine.storage.artifact_store import ArtifactStore

log = logging.getLogger(__name__)

def candidate_detectors(elements, url: str) -> list[dict]:
    """Suggest detectors for a state the system has just failed on.

    Deliberately suggestions, not conclusions: the engine has no way to tell whether an
    unfamiliar banner means "no such record" (a business outcome the caller wants) or "the
    database is down" (a hard failure that must stay one). Naming it is the human's job; all
    this does is hand them the strings worth choosing between, already stripped of record
    data and already generalised.
    """
    seen: set[str] = set()
    suggestions: list[dict] = []
    for element in elements:
        name = (element.name or "").strip()
        if not (MIN_CANDIDATE <= len(name) <= MAX_CANDIDATE):
            continue
        fragment = stable_fragment(name)
        if not fragment or fragment in seen:
            continue
        seen.add(fragment)
        suggestions.append({"text_contains": fragment, "role": element.role})

    # Host and port are deployment detail, not signal — a path is what distinguishes a
    # state. Digit segments in it are record ids and are dropped with everything else.
    parsed = urlparse(url)
    path_fragment = stable_fragment(
        (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""),
        minimum=MIN_URL_CANDIDATE,
    )
    if path_fragment:
        suggestions.append({"url_contains": path_fragment})
    return suggestions


class OutcomeLearner:
    def __init__(
        self,
        store: ArtifactStore | None = None,
        knowledge_base: KnowledgeBaseService | None = None,
    ) -> None:
        self.store = store or ArtifactStore()
        self.knowledge_base = knowledge_base or KnowledgeBaseService()

    def learn(
        self,
        app_id: str,
        code: str,
        outcome_type: str,
        detector: dict,
        message: str | None = None,
        recovery: str | None = None,
        capability_id: str | None = None,
        learned_from: str | None = None,
    ) -> KnownOutcome:
        """Record a newly named outcome, app-wide and optionally on one capability now.

        It goes to the Knowledge Base first because it is a fact about the *application*:
        a session that expires mid-flow will expire for every capability, not only the one
        that happened to be running. Capabilities already recorded do not pick it up
        retroactively — an artifact is a frozen contract, and silently changing what a
        reviewed capability detects would undermine the point of freezing it — so the
        caller names one to patch, and it is patched with a version bump.
        """
        outcome = KnownOutcome.model_validate(
            {
                "code": code,
                "type": outcome_type,
                "detector": detector,
                "message": message,
                "recovery": recovery,
            }
        )
        self.knowledge_base.record_outcome(
            app_id,
            {**outcome.model_dump(), "learned_from": learned_from},
        )

        if capability_id:
            artifact = self.store.load(capability_id)
            remaining = [o for o in artifact.known_outcomes if o.code != outcome.code]
            major, minor, patch = (int(part) for part in artifact.version.split("."))
            updated = artifact.model_copy(
                update={
                    "known_outcomes": remaining + [outcome],
                    "version": f"{major}.{minor}.{patch + 1}",
                    "provenance": artifact.provenance.model_copy(
                        update={"last_validated_at": datetime.now(timezone.utc).isoformat()}
                    ),
                }
            )
            self.store.save(updated)
            log.info("taught %s the outcome %r", capability_id, code)
        return outcome
