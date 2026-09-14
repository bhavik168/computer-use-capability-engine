"""Pause the run, hand the live session to a human, resume.

The design point that makes this real rather than a mock: the browser runs non-headless, so
the window the agent is driving *is* the window the operator takes over. There is no second
session to synchronise, no state to transfer and no co-browsing infrastructure — control
simply changes hands in the same window, which is precisely why the brief's "mock the
operator UI, make the control-transfer model real" split lands on the side of real here.

What is minimal is the operator's *view*: a console summary and a screenshot path, rather
than a web console. What is not minimal is the mechanism — the run genuinely stops, the
human genuinely acts in the live session, and the run genuinely continues from whatever
they left behind.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from engine.escalation.models import InterventionRequest
from engine.reporting.redaction import redact_screenshot
from engine.storage.escalation_store import EscalationStore

log = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_DIR = Path("evidence/escalations")


class EscalationService:
    def __init__(
        self,
        store: EscalationStore | None = None,
        screenshot_dir: Path | str = DEFAULT_SCREENSHOT_DIR,
        interactive: bool = True,
        surface=None,
    ) -> None:
        self.store = store or EscalationStore()
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        # Non-interactive mode exists for automated demos and tests: the request is still
        # written and reported, it simply does not block on a terminal nobody is watching.
        self.interactive = interactive
        self.surface = surface

    # ------------------------------------------------------------------ raising

    def raise_intervention(self, req: InterventionRequest) -> InterventionRequest:
        self.store.save(req)
        self._print_context(req)

        if not self.interactive:
            log.warning("non-interactive mode: recording the intervention without blocking")
            return self.resolve(req.request_id, "auto-resolved (non-interactive run)")

        print("\n>>> Take over the browser window now. Press Enter here when you are done.")
        try:
            input()
        except EOFError:
            return self.resolve(req.request_id, "no operator attached (stdin closed)")
        notes = input("What did you do? (free text, Enter to skip): ").strip()
        return self.resolve(req.request_id, notes or "operator pressed Enter without notes")

    def raise_intervention_for_discovery(
        self, goal: str, current_step: str, reason: str, screenshot: bytes | None = None
    ) -> InterventionRequest:
        return self.raise_intervention(
            self._build("discovery_policy_gate", goal, current_step, reason, screenshot)
        )

    def raise_intervention_for_replay(
        self, capability_id: str, current_step: str, reason: str,
        screenshot: bytes | None = None, candidates: list[dict] | None = None,
    ) -> InterventionRequest:
        return self.raise_intervention(
            self._build(
                "replay_unrecoverable", capability_id, current_step, reason, screenshot,
                candidates,
            )
        )

    # ------------------------------------------------------------------ resolving

    def resolve(self, request_id: str, notes: str) -> InterventionRequest:
        """Record what the operator did.

        Free text, not inference: this system deliberately does not try to detect what a
        human clicked in the live window. Guessing at it would be unreliable and would read
        as evidence, which is worse than an honest note.
        """
        request = self.store.load(request_id)
        request.status = "resolved"
        request.resolution_notes = notes
        request.resolved_at = datetime.now(timezone.utc)
        self.store.save(request)
        log.info("intervention %s resolved: %s", request_id, notes)
        return request

    # ------------------------------------------------------------------ helpers

    def _build(self, source, subject, current_step, reason, screenshot,
               candidates=None) -> InterventionRequest:
        request_id = f"esc_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
        screenshot_path = None
        if screenshot:
            path = self.screenshot_dir / f"{request_id}.png"
            # Redaction sits ahead of every write to disk, without exception.
            path.write_bytes(redact_screenshot(screenshot))
            screenshot_path = str(path)
        url = None
        if self.surface is not None:
            try:
                url = self.surface.current_url()
            except Exception:
                url = None
        return InterventionRequest(
            request_id=request_id,
            created_at=datetime.now(timezone.utc),
            source=source,
            goal_or_capability=subject,
            current_step=current_step,
            reason=reason,
            screenshot_path=screenshot_path,
            url=url,
            candidate_detectors=candidates or [],
        )

    @staticmethod
    def _print_context(req: InterventionRequest) -> None:
        print("\n" + "=" * 72)
        print("  HUMAN INTERVENTION REQUIRED")
        print("=" * 72)
        print(f"  Request    : {req.request_id}")
        print(f"  Raised by  : {req.source}")
        print(f"  Goal/cap.  : {req.goal_or_capability}")
        print(f"  At step    : {req.current_step}")
        print(f"  Reason     : {req.reason}")
        if req.url:
            print(f"  Screen     : {req.url}")
        if req.screenshot_path:
            print(f"  Screenshot : {req.screenshot_path}")
        if req.candidate_detectors:
            print("-" * 72)
            print("  This state is not one this capability knows about. If it is a routine")
            print("  outcome rather than a fault, teach it with `learn-outcome` using one of:")
            for candidate in req.candidate_detectors:
                key = "text_contains" if "text_contains" in candidate else "url_contains"
                print(f"    --{key.replace('_', '-')} {candidate[key]!r}")
        print("-" * 72)
        print("  The live browser window is paused and under your control.")
        print("=" * 72)
