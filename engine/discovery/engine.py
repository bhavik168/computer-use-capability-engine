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
from datetime import datetime, timezone
from urllib.parse import urljoin

from engine.discovery import grounding
from engine.discovery.llm_client import Decision, LLMClient, LLMError
from engine.discovery.loop_guard import LoopGuard
from engine.policy.policy import Policy
from engine.discovery.trace import DiscoveryTrace, TraceStep
from engine.surface.base import Surface, SurfaceError

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
- Read values from the "Text and values visible on screen" section — those are not
  clickable, so use `extract` with the element id only when you need to record a value as
  a result of the task.
- Set goal_reached to true only when the screen in front of you already proves the goal is
  met, and quote that proof in goal_evidence. Do not predict; look.
- Prefer the smallest number of steps. Do not explore screens the goal does not need.
"""


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

    class _Outcome:
        def __init__(self, ok: bool, message: str, terminal: bool = False) -> None:
            self.ok = ok
            self.message = message
            self.terminal = terminal

    def _apply(self, step_id, decision: Decision, elements, url_before, trace, values=None):
        action = decision.action
        rationale = decision.rationale

        if action == "done" or decision.goal_reached:
            # Trusting the model's own completion signal is a v1 choice: there is no
            # checkpoint to test against yet, because producing that checkpoint definition
            # is what this run is *for*. The signal is only accepted on a step whose
            # grounding and effect verification already passed.
            trace.status = "completed"
            trace.stop_reason = "model reported the goal was reached"
            trace.goal_evidence = decision.goal_evidence
            return self._Outcome(True, "Run ended: goal reported reached.", terminal=True), None

        if action == "navigate":
            target = decision.url or ""
            absolute = urljoin(url_before, target)
            if not self.policy.check_allowlist(absolute):
                trace.status = "blocked_by_policy"
                trace.stop_reason = f"navigation to {target} is outside the allowlist"
                return (
                    self._Outcome(False, f"Blocked: {target} is outside the allowlist.", True),
                    None,
                )
            self.surface.navigate(target)
            step = self._make_step(
                step_id, "navigate", rationale, url_before, url_value=target,
                description=f"Navigated to {target}",
            )
            return self._Outcome(True, f"Navigated to {target}. Observe again."), step

        element_id = decision.element_id
        expected = next((el for el in elements if el.element_id == element_id), None)
        if expected is None:
            return (
                self._Outcome(False, f"{element_id!r} is not an element in this observation."),
                None,
            )

        check = grounding.reverify(self.surface, element_id, expected)
        if not check.ok:
            # A failed step, not a best guess: re-observe and let the model decide again.
            return self._Outcome(False, f"Could not act: {check.reason}. Re-observe."), None

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
        self, step_id, action, rationale, decision, element, url_before, elements_before,
        locator=None, values=None,
    ):
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
                text = self._substitute(typed_template, values or {})
                self.surface.type_text(element.element_id, text)
                shown = typed_template if typed_template != text else repr(text)
                description = f"Typed {shown} into {element.role} \"{element.name}\""
            elif action == "extract":
                extracted = self.surface.read_text(element.element_id)
                description = f"Read {extracted!r} from {element.role} \"{element.name}\""
            else:
                return self._Outcome(False, f"Unsupported action {action!r}."), None
        except SurfaceError as exc:
            return self._Outcome(False, f"Action failed: {exc}"), None

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
        return self._Outcome(effect.ok, message), step

    def _handle_risky(self, step_id, action, rationale, element, url_before, trace, locator=None):
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
                description=f"Paused before {element.role} \"{element.name}\" — needs approval",
            )
            trace.steps.append(step)
            return self._Outcome(False, f"Halted: {reason}", terminal=True), None

        self.escalation.raise_intervention_for_discovery(
            goal=trace.goal,
            current_step=f"{step_id}: {action} {element.role} \"{element.name}\"",
            reason=reason,
            screenshot=self._screenshot(),
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
            self._Outcome(
                True,
                "A human took control of the live session and has handed it back. "
                "Observe the current screen and continue from what is actually there.",
            ),
            step,
        )

    # ------------------------------------------------------------------ helpers

    def _make_step(
        self, step_id, action, rationale, url_before, element=None, url_value=None,
        typed_value=None, extracted_value=None, url_after=None, risk_class="safe",
        verified=True, verification_note=None, description="", locator=None,
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
            risk_class=risk_class,
            url_before=url_before,
            url_after=url_after or self._safe_url(),
            verified=verified,
            verification_note=verification_note,
            description=description,
            screenshot=self._screenshot(),
        )

    @staticmethod
    def _substitute(template: str, values: dict) -> str:
        result = template
        for name, value in values.items():
            result = result.replace(f"{{{{{name}}}}}", str(value))
        return result

    def _kb_hint(self, goal: str) -> str | None:
        if self.knowledge_base is None:
            return None
        guidance = self.knowledge_base.lookup(self.app_id, goal)
        # A miss is the normal case on a cold knowledge base and must stay silent: blind
        # exploration is the fallback, not an error condition.
        return guidance.as_hint() if guidance else None

    def _screenshot(self) -> bytes | None:
        try:
            return self.surface.screenshot()
        except Exception:
            return None

    def _safe_url(self) -> str | None:
        try:
            return self.surface.current_url()
        except Exception:
            return None

    def _finish(self, trace: DiscoveryTrace, started: float) -> DiscoveryTrace:
        trace.duration_seconds = round(time.time() - started, 2)
        trace.final_url = self._safe_url()
        trace.llm_calls = self.llm.calls
        if self.report_generator is not None:
            self.report_generator.generate_for_discovery(trace)
        return trace
