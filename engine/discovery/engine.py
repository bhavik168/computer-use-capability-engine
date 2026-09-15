"""The observe → decide → act loop.

This is the only mode with a model in the decision path, and it runs once per capability:
the first time a goal is asked for. Everything it does is aimed at producing something the
Recorder can freeze into an artifact, after which the same goal is served by Replay with no
model call at all.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from engine.discovery import grounding
from engine.discovery.llm_client import Decision, LLMClient, LLMError
from engine.discovery.loop_guard import LoopGuard
from engine.policy.policy import Policy
from engine.schema import templating
from engine.discovery.trace import DiscoveryTrace, TraceStep, goal_expects_value
from engine.surface.base import Surface, SurfaceError
from engine.surface.elements import ObservedElement

log = logging.getLogger(__name__)

# Application-agnostic, and it stays that way. The engine is pointed at a URL and told a
# goal in plain language; everything else — that there is a login form, where search lives,
# what a restricted account looks like — it finds by looking. A prompt that described one
# application's screens would be one more thing to rewrite for the next one, and would make
# a "discovery" run partly a recital of what it had been told.
SYSTEM_PROMPT = """You operate a business application through its accessibility tree, the
way a staff member would through its screens. There is no API; the UI is the only way in.

Rules:
- Take exactly one action per turn, using the `act` tool. Always give your rationale.
- You may only act on element ids listed in the current observation. Ids are reassigned
  every turn; never reuse an id from a previous turn.
- If the application requires a session and you are not in one, establishing it is part of
  the goal like anything else: find the controls and use them. Never sign out.
- Entries under "Read-only text and values" carry element ids like any other. You cannot
  click or type into them, but you can `extract` one by its id.
- When the goal asks you to read, extract, get, confirm or return a value, you must
  `extract` that value by its element id before you finish. The extract is what the
  capability hands back to whoever calls it, so a run that ends without one has produced a
  capability that returns nothing. Quoting the value in goal_evidence is not a substitute.
- Set goal_reached to true only when the screen in front of you already proves the goal is
  met, and quote that proof in goal_evidence. Do not predict; look. If the goal named a
  value to return, extract it first and finish on the turn after.
- Prefer the smallest number of steps. Do not explore screens the goal does not need.
"""


@dataclass(frozen=True)
class StepOutcome:
    """What one attempted action did to the loop.

    `ok` is reported back to the model as the result of its tool call, so it is the model's
    only feedback signal; `terminal` ends the run. A nested class previously, which meant the
    three methods returning it could not name it in their annotations.
    """

    ok: bool
    message: str
    terminal: bool = False


class DiscoveryEngine:
    def __init__(
        self,
        surface: Surface,
        llm_client: LLMClient,
        policy: Policy,
        loop_guard: LoopGuard,
        app_id: str,
        knowledge_base=None,
        escalation=None,
        report_generator=None,
    ) -> None:
        self.surface = surface
        self.llm = llm_client
        self.policy = policy
        self.loop_guard = loop_guard
        self.app_id = app_id
        self.knowledge_base = knowledge_base
        self.escalation = escalation
        self.report_generator = report_generator

    # ------------------------------------------------------------------ the loop

    def run(self, goal: str, target_url: str,
            param_values: dict[str, str] | None = None) -> DiscoveryTrace:
        """Drive the loop until the goal is met or a stopping condition fires.

        `param_values` are values the caller supplied for this run (a member id, a
        credential). The model is told the *names* and types the placeholder `{{name}}`;
        this engine substitutes the real value at the keyboard. A password is therefore
        never sent to the model API, never appears in the trace, and never reaches the
        artifact — which also gives the Recorder an unambiguous signal about which typed
        values are parameters rather than guessing from the text.
        """
        run_id = f"discovery_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
        started = time.time()
        trace = DiscoveryTrace(
            run_id=run_id,
            goal=goal,
            app_id=self.app_id,
            target_url=target_url,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        if not self.policy.check_allowlist(target_url):
            trace.status = "blocked_by_policy"
            trace.stop_reason = f"target {target_url} is outside the allowlist"
            return self._finish(trace, started)

        values = param_values or {}
        self.surface.navigate(target_url)
        # The client owns the conversation: message shape is the most provider-specific
        # part of an agent loop, and assembling it here would tie this file to one vendor.
        self.llm.start(SYSTEM_PROMPT)
        step_number = 0
        # Whether the previous action was meant to change the screen; see LoopGuard.
        last_action_mutating = True

        while True:
            elements = self.surface.observe()
            url = self.surface.current_url()
            self.loop_guard.record_state(url, elements, mutating=last_action_mutating)
            if self.knowledge_base is not None:
                self.knowledge_base.record_observation(self.app_id, url, elements)

            verdict = self.loop_guard.check(trace)
            if verdict != "continue":
                trace.status = f"stopped_{verdict.removeprefix('stop_')}"  # type: ignore[assignment]
                trace.stop_reason = f"loop guard: {verdict}"
                log.warning("discovery halted by loop guard: %s", verdict)
                break

            observation = grounding.render_observation(
                elements, url, self.surface.visible_text(), available_params=sorted(values)
            )
            hint = self._kb_hint(goal)
            prompt = f"GOAL: {goal}\n\n{observation}" + (
                f"\n\nKnown from previous runs: {hint}" if hint else ""
            )

            try:
                decision = self.llm.decide(prompt, grounding.build_tool_schema(elements)[0])
            except LLMError as exc:
                trace.status = "stopped_failed_step"
                trace.stop_reason = str(exc)
                break

            trace.llm_calls = self.llm.calls
            step_number += 1
            step_id = f"s{step_number}"

            # `extract` reads a value and `type` fills a field: both change page content
            # (a read leaves it untouched; a typed value is not part of the role/name hash),
            # so the resulting screen is semantically identical to the one before it. Feeding
            # that to the cycle detector halts a multi-field form one keystroke in — the agent
            # entering the second field looks like it is stuck on the first. Neither
            # contributes a hash; runaway typing is still bounded by the step and time budgets.
            last_action_mutating = decision.action not in ("extract", "type")
            outcome, step = self._apply(step_id, decision, elements, url, trace, values)
            if step is not None:
                trace.steps.append(step)
            self.llm.record_result(outcome.message, ok=outcome.ok)

            if outcome.terminal:
                break

        return self._finish(trace, started)

    # ------------------------------------------------------------------ acting

    # Refuse a premature completion once, then let the run finish and let the Recorder
    # flag what it produced. A model that will not extract after being told twice is not
    # going to on the third ask, and a loop that cannot end is worse than a flagged artifact.
    MAX_EXTRACT_REFUSALS = 1

    def _needs_extract(self, trace) -> bool:
        if trace.extract_refusals >= self.MAX_EXTRACT_REFUSALS:
            return False
        if not goal_expects_value(trace.goal):
            return False
        return not any(s.action == "extract" and s.verified for s in trace.steps)

    def _apply(
        self,
        step_id: str,
        decision: Decision,
        elements: list[ObservedElement],
        url_before: str,
        trace: DiscoveryTrace,
        values: dict | None = None,
    ) -> tuple[StepOutcome, TraceStep | None]:
        action = decision.action
        rationale = decision.rationale

        if action == "done" or decision.goal_reached:
            # A goal phrased as "read the balance" is not finished while the value has only
            # been *seen*. The model reliably reaches the right screen and then declares
            # victory by quoting the value as evidence, which produces a capability that
            # returns nothing and a checkpoint pinned to this run's literal answer. Asking
            # for the extract in the prompt helps but does not hold, so the loop refuses the
            # completion once and says why. Refused at most once per run: if the model
            # insists, the run still completes and the Recorder flags the artifact, because
            # a capability that navigates correctly is worth more than a failed run.
            if self._needs_extract(trace):
                trace.extract_refusals += 1
                return (
                    StepOutcome(
                        False,
                        "Not done yet: the goal asks for a value to be returned, and you "
                        "have not extracted it. Find it under \"Read-only text and values\" "
                        "and call extract with its element id and an output_name, then "
                        "finish.",
                    ),
                    None,
                )
            trace.status = "completed"
            trace.stop_reason = "model reported the goal was reached"
            trace.goal_evidence = decision.goal_evidence
            return StepOutcome(True, "Run ended: goal reported reached.", terminal=True), None

        if action == "navigate":
            target = decision.url or ""
            absolute = urljoin(url_before, target)
            if not self.policy.check_allowlist(absolute):
                trace.status = "blocked_by_policy"
                trace.stop_reason = f"navigation to {target} is outside the allowlist"
                return (
                    StepOutcome(False, f"Blocked: {target} is outside the allowlist.", True),
                    None,
                )
            self.surface.navigate(target)
            step = self._make_step(
                step_id, "navigate", rationale, url_before, url_value=target,
                description=f"Navigated to {target}",
            )
            return StepOutcome(True, f"Navigated to {target}. Observe again."), step

        element_id = decision.element_id
        expected = next((el for el in elements if el.element_id == element_id), None)
        if expected is None:
            return (
                StepOutcome(False, f"{element_id!r} is not an element in this observation."),
                None,
            )

        check = grounding.reverify(self.surface, element_id, expected)
        if not check.ok:
            # A failed step, not a best guess: re-observe and let the model decide again.
            return StepOutcome(False, f"Could not act: {check.reason}. Re-observe."), None

        # Build the persistent locator now, while the element is definitely on screen — a
        # click may navigate away and leave nothing to describe afterwards.
        try:
            locator = self.surface.build_locator(element_id)
        except SurfaceError as exc:
            log.warning("could not build a locator for %s: %s", element_id, exc)
            locator = None

        risk = self.policy.classify(action, expected)
        if risk == "risky":
            return self._handle_risky(
                step_id, action, rationale, expected, url_before, trace, locator
            )

        return self._perform(
            step_id, action, rationale, decision, expected, url_before, elements, locator,
            values or {},
        )

    def _perform(
        self,
        step_id: str,
        action: str,
        rationale: str | None,
        decision: Decision,
        element: ObservedElement,
        url_before: str,
        elements_before: list[ObservedElement],
        locator: dict | None = None,
        values: dict | None = None,
    ) -> tuple[StepOutcome, TraceStep | None]:
        extracted = None
        typed_template = None
        try:
            if action == "click":
                self.surface.click(element.element_id)
                description = f"Clicked {element.role} \"{element.name}\""
            elif action == "type":
                typed_template = decision.text or ""
                # Substitution happens here, at the keyboard, and nowhere earlier: the
                # template is what the model chose and what the trace keeps, the real value
                # only ever exists in this call.
                text = templating.render(typed_template, values or {})
                self.surface.type_text(element.element_id, text)
                shown = typed_template if typed_template != text else repr(text)
                description = f"Typed {shown} into {element.role} \"{element.name}\""
            elif action == "extract":
                extracted = self.surface.read_text(element.element_id)
                description = f"Read {extracted!r} from {element.role} \"{element.name}\""
            else:
                return StepOutcome(False, f"Unsupported action {action!r}."), None
        except SurfaceError as exc:
            return StepOutcome(False, f"Action failed: {exc}"), None

        elements_after = self.surface.observe()
        url_after = self.surface.current_url()
        if action == "extract":
            effect = grounding.GroundingResult(True)  # reading changes nothing by design
        else:
            effect = grounding.verify_effect(url_before, elements_before, url_after, elements_after)

        step = self._make_step(
            step_id,
            action,
            rationale,
            url_before,
            element=element,
            typed_value=typed_template,
            extracted_value=extracted,
            output_name=decision.output_name if action == "extract" else None,
            url_after=url_after,
            locator=locator,
            verified=effect.ok,
            verification_note=effect.reason,
            description=description,
        )
        message = description + (
            f" Now on {url_after}."
            if effect.ok
            else f" WARNING: {effect.reason}. Treat this step as not having worked."
        )
        return StepOutcome(effect.ok, message), step

    def _handle_risky(
        self,
        step_id: str,
        action: str,
        rationale: str | None,
        element: ObservedElement,
        url_before: str,
        trace: DiscoveryTrace,
        locator: dict | None = None,
    ) -> tuple[StepOutcome, TraceStep | None]:
        reason = f"Action classified risky: {element.name}"
        if self.escalation is None:
            # No escalation service attached (--no-escalation, or an unattended sweep). The
            # action is still never executed: the run halts here with a distinct status
            # rather than silently performing it or silently skipping it.
            log.warning("HALTING: %s would require human approval", reason)
            trace.status = "paused_for_escalation"
            trace.stop_reason = reason
            step = self._make_step(
                step_id, action, rationale, url_before, element=element, locator=locator,
                risk_class="risky", verified=False,
                verification_note="halted for human approval; not executed",
                description=f"Paused before {element.role} \"{element.name}\" (needs approval)",
            )
            trace.steps.append(step)
            return StepOutcome(False, f"Halted: {reason}", terminal=True), None

        self.escalation.raise_intervention_for_discovery(
            goal=trace.goal,
            current_step=f"{step_id}: {action} {element.role} \"{element.name}\"",
            reason=reason,
            screenshot=self.surface.safe_screenshot(),
        )
        # The human may have performed the action, done something different, or simply
        # approved us to carry on. Re-observe and decide from whatever is now true rather
        # than replaying the action that triggered the pause.
        step = self._make_step(
            step_id, action, rationale, url_before, element=element, locator=locator,
            risk_class="risky",
            url_after=self.surface.current_url(),
            description=f"Escalated before {element.role} \"{element.name}\"; human took control",
        )
        return (
            StepOutcome(
                True,
                "A human took control of the live session and has handed it back. "
                "Observe the current screen and continue from what is actually there.",
            ),
            step,
        )

    # ------------------------------------------------------------------ helpers

    def _make_step(
        self,
        step_id: str,
        action: str,
        rationale: str | None,
        url_before: str,
        element: ObservedElement | None = None,
        url_value: str | None = None,
        typed_value: str | None = None,
        extracted_value: str | None = None,
        output_name: str | None = None,
        url_after: str | None = None,
        risk_class: str = "safe",
        verified: bool = True,
        verification_note: str | None = None,
        description: str = "",
        locator: dict | None = None,
    ) -> TraceStep:
        return TraceStep(
            step_id=step_id,
            action=action,
            rationale=rationale,
            element_role=element.role if element else None,
            element_name=element.name if element else None,
            element_id=element.element_id if element else None,
            locator=locator,
            url_value=url_value,
            typed_value=typed_value,
            extracted_value=extracted_value,
            output_name=output_name,
            risk_class=risk_class,
            url_before=url_before,
            url_after=url_after or self.surface.safe_url(),
            verified=verified,
            verification_note=verification_note,
            description=description,
            screenshot=self.surface.safe_screenshot(),
        )

    def _kb_hint(self, goal: str) -> str | None:
        if self.knowledge_base is None:
            return None
        guidance = self.knowledge_base.lookup(self.app_id, goal)
        # A miss is the normal case on a cold knowledge base and must stay silent: blind
        # exploration is the fallback, not an error condition.
        return guidance.as_hint() if guidance else None

    def _finish(self, trace: DiscoveryTrace, started: float) -> DiscoveryTrace:
        trace.duration_seconds = round(time.time() - started, 2)
        trace.final_url = self.surface.safe_url()
        trace.llm_calls = self.llm.calls
        if self.report_generator is not None:
            self.report_generator.generate_for_discovery(trace)
        return trace
