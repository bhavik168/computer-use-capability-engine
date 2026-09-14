"""The application's cumulative UI map, built as a side effect of real discovery runs.

This is an efficiency layer, never a correctness one. Discovery worked before it existed
and must keep working when it returns nothing: a `lookup()` miss is the ordinary cold-start
case and falls through silently to blind exploration rather than warning about itself.

Crucially, a KB hint only changes what gets *proposed*. Every KB-guided action is grounded
against the live observation and verified afterwards exactly like a blind one — the map can
be stale, and treating it as authority would reintroduce the failure it is meant to reduce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from engine.knowledge_base.store import (
    AppModel,
    ElementRef,
    KnowledgeBaseStore,
    LearnedOutcome,
    Screen,
    normalize_url,
)
from engine.reporting.redaction import redact_text
from engine.surface.elements import VALUE_LIKE_RE, ObservedElement

log = logging.getLogger(__name__)

# Function words only. Nothing here is a domain noun: "member" would be "patient",
# "policy", "claim" or "ticket" in the next application, and a stopword list that quietly
# discarded the most meaningful word in the goal would be app knowledge smuggled in as
# tuning.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with",
    "from", "into", "their", "its", "this", "that", "these", "those", "is", "are", "be",
    "any", "all", "then", "than",
}


@dataclass
class ScreenGuidance:
    screen_id: str
    element: ElementRef

    def as_hint(self) -> str:
        return (
            f"a previous run found {self.element.role} \"{self.element.purpose}\" on "
            f"{self.screen_id}. Verify it yourself before relying on it."
        )


class KnowledgeBaseService:
    def __init__(self, store: KnowledgeBaseStore | None = None) -> None:
        self.store = store or KnowledgeBaseStore()
        self._cache: dict[str, AppModel] = {}

    # ------------------------------------------------------------------ writes

    def record_observation(
        self, app_id: str, url: str, elements: list[ObservedElement]
    ) -> None:
        """Upsert the structure of one screen.

        **Element values are never written here** — only role, purpose and locator.

        Two filters, because one is not enough. An earlier version screened names against
        `VALUE_LIKE_RE` alone; that regex is anchored, so it caught a bare "4,060.55" and
        sailed past "Member Record — Alice Johnson (10001)", which then landed in the store.
        A KB whose justification is that it holds structure only cannot afford that.

        1. **Interactive elements only.** Links, buttons and fields are what a navigation
           map is made of. Read-only cells and headings are where record content lives and
           are of no use here, so the entire class is excluded rather than screened.
        2. **`redact_text` over what survives.** A control's own label can still carry data
           (a link named after the record it opens), so digit runs and addresses are masked
           before anything is written.

        Known limit, stated rather than papered over: a personal name inside a control's
        label — a link labelled "Alice Johnson" — is not something a regex can recognise,
        and would still be written. Closing that needs a named-entity pass or an
        allowlist of label shapes per app; neither is built (brief 3.4, ARCHITECTURE.md §6).
        """
        model = self._model(app_id)
        screen_id = normalize_url(url)
        screen = model.screen(screen_id)
        if screen is None:
            screen = Screen(screen_id=screen_id)
            model.screens.append(screen)

        now = datetime.now(timezone.utc).isoformat()
        for element in elements:
            if not element.interactive or not element.name:
                continue
            purpose = redact_text(element.name.strip())
            if not purpose or VALUE_LIKE_RE.match(purpose):
                continue
            ref_id = f"{app_id}{screen_id}#{element.role}:{purpose}"
            existing = next(
                (e for e in screen.elements if e.element_ref_id == ref_id), None
            )
            if existing:
                existing.confidence = min(1.0, existing.confidence + 0.1)
                existing.last_validated_at = now
                existing.stale = False
            else:
                screen.elements.append(
                    ElementRef(
                        element_ref_id=ref_id,
                        role=element.role,
                        purpose=purpose,
                        locator={
                            "primary": {
                                "strategy": "role+name",
                                "value": f"{element.role}:{purpose}",
                            },
                            "fallbacks": [],
                        },
                        confidence=0.5,
                        last_validated_at=now,
                    )
                )
        self.store.save(app_id, model)

    def record_outcome(self, app_id: str, outcome: dict) -> LearnedOutcome:
        """Teach this application a new exceptional state.

        Written once per application rather than once per capability, because "the session
        expired" is a fact about the application, not about the capability that happened to
        be running when it was first seen.
        """
        model = self._model(app_id)
        entry = LearnedOutcome.model_validate(
            {**outcome, "learned_at": datetime.now(timezone.utc).isoformat()}
        )
        model.outcomes = [o for o in model.outcomes if o.code != entry.code] + [entry]
        self.store.save(app_id, model)
        log.info("learned outcome %r for %s", entry.code, app_id)
        return entry

    def outcomes(self, app_id: str) -> list[dict]:
        """Everything this application has been taught, for the Recorder to stamp on."""
        return [
            o.model_dump(exclude={"learned_at", "learned_from"})
            for o in self._model(app_id).outcomes
        ]

    def mark_stale(self, app_id: str, element_ref_id: str) -> None:
        """Flag an element whose locator failed to resolve during a run.

        One fix here propagates to every artifact that references the element, which is the
        reason locators live in the KB rather than being copied into each artifact.
        """
        model = self._model(app_id)
        for screen in model.screens:
            for element in screen.elements:
                if element.element_ref_id == element_ref_id:
                    element.stale = True
                    element.confidence = max(0.0, element.confidence - 0.3)
                    log.warning("KB element marked stale: %s", element_ref_id)
                    self.store.save(app_id, model)
                    return
        log.debug("mark_stale: %s is not in the KB for %s", element_ref_id, app_id)

    # ------------------------------------------------------------------ reads

    def lookup(self, app_id: str, intent: str) -> ScreenGuidance | None:
        """Substring keyword match over element purposes — deliberately unsophisticated.

        Embeddings would be a better matcher and a worse demonstration: the point is that
        the loop consults a shared map and degrades silently when it misses, which a
        case-insensitive keyword match shows just as well at none of the cost.
        """
        model = self._model(app_id)
        terms = [
            term
            for term in "".join(c.lower() if c.isalnum() else " " for c in intent).split()
            if term not in STOPWORDS and len(term) > 2
        ]
        if not terms:
            return None

        best: tuple[int, Screen, ElementRef] | None = None
        for screen in model.screens:
            for element in screen.elements:
                if element.stale:
                    continue
                purpose = element.purpose.lower()
                score = sum(1 for term in terms if term in purpose)
                if score and (best is None or score > best[0]):
                    best = (score, screen, element)
        if best is None:
            return None
        _, screen, element = best
        return ScreenGuidance(screen_id=screen.screen_id, element=element)

    def _model(self, app_id: str) -> AppModel:
        if app_id not in self._cache:
            self._cache[app_id] = self.store.load(app_id)
        return self._cache[app_id]
