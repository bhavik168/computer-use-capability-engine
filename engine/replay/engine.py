"""Deterministic execution of a recorded capability.

There is no LLM anywhere in this file, and there must never be one: replay is the path a
production agent actually invokes, and its value is that it costs a page load rather than a
model call and behaves identically every time. Everything it needs was decided once, during
discovery, and frozen into the artifact.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone

from engine.replay.result import RunResult, StepLog
from engine.schema.artifact import Artifact, ElementTarget, Step, UrlTarget
from engine.surface.base import Surface

log = logging.getLogger(__name__)

NUMERIC_TYPES = ("number", "currency")


class ReplayEngine:
    def __init__(self, surface: Surface, escalation=None, knowledge_base=None,
                 report_generator=None, store=None) -> None:
        self.surface = surface
        # Needed only to resolve an artifact's `requires` chain — a capability that depends
        # on another (an authenticated session, most often) replays that one first.
        self.store = store
        # A replay that cannot proceed notifies a human through the same shared Escalation
        # Service the discovery policy gate uses — one mechanism, two callers.
        self.escalation = escalation
        self.knowledge_base = knowledge_base
        self.report_generator = report_generator

    # ------------------------------------------------------------------ entry point

    def run(self, artifact: Artifact, params: dict) -> RunResult:
        run_id = f"replay_{artifact.capability_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
        started = time.time()
        started_at = datetime.now(timezone.utc).isoformat()
        steps: list[StepLog] = []

        def finish(result: RunResult) -> RunResult:
            result.run_id = run_id
            result.capability_id = artifact.capability_id
            result.started_at = started_at
            result.duration_seconds = round(time.time() - started, 2)
            result.steps_executed = steps
            if result.status in ("hard_failure", "recoverable") and self.escalation is not None:
                # The structured result still goes back to the caller either way; the
                # escalation is so a human learns about it, not so replay can be rescued.
                self.escalation.raise_intervention_for_replay(
                    capability_id=artifact.capability_id,
                    current_step=result.failed_at_step or "(after final step)",
                    reason=f"{result.status}: {result.outcome_code} — {result.message}",
                    screenshot=self._safe_screenshot(),
                    # A hard failure means this capability met a state nobody has named yet.
                    # Hand the human the strings worth naming it with.
                    candidates=(
                        self._candidates() if result.status == "hard_failure" else []
                    ),
                )
            if self.report_generator is not None:
                self.report_generator.generate_for_replay(result, artifact)
            return result

        # Validated before prerequisites run, not after: a prerequisite is itself a live
        # replay that signs on and drives the real UI, so validating second means a bad
        # call still touches the application before being rejected. Validation is pure —
        # it only loads prerequisite artifacts to learn their parameter names — so it is
        # safe to do first, and a rejected call now leaves the surface untouched.
        param_error = self._validate_params(artifact, params)
        if param_error:
            return finish(
                RunResult(
                    status="hard_failure",
                    outcome_code="invalid_params",
                    message=param_error,
                )
            )

        prerequisite_error = self._run_prerequisites(artifact, params, steps)
        if prerequisite_error:
            return finish(
                RunResult(
                    status="hard_failure",
                    outcome_code="prerequisite_failed",
                    message=prerequisite_error,
                )
            )

        extracted: dict[str, str] = {}

        for step in artifact.steps:
            try:
                step_log, outcome = self._execute_step(artifact, step, params, extracted)
            except Exception as exc:  # surface/Playwright failure — never escapes run()
                log.exception("step %s raised", step.step_id)
                steps.append(
                    StepLog(
                        step_id=step.step_id,
                        action=step.action,
                        description=f"{self._describe(step, params, artifact=artifact)} — raised {type(exc).__name__}",
                        url=self._safe_url(),
                        verified=False,
                        error=str(exc),
                        screenshot=self._safe_screenshot(),
                    )
                )
                return finish(
                    RunResult(
                        status="hard_failure",
                        outcome_code="surface_error",
                        message=(
                            f"Step {step.step_id} ({step.action}) failed: {exc}. "
                            f"Expected to act on {self._locator_text(step)}."
                        ),
                        failed_at_step=step.step_id,
                    )
                )

            steps.append(step_log)

            if step_log.error:
                return finish(
                    RunResult(
                        status="hard_failure",
                        outcome_code="locator_unresolved",
                        message=step_log.error,
                        failed_at_step=step.step_id,
                    )
                )

            if outcome is not None:
                # A known outcome fired: stop immediately rather than driving the remaining
                # steps into a page they were never recorded against.
                status = "recoverable" if outcome.type == "recoverable" else "business_outcome"
                if outcome.type == "hard_failure":
                    status = "hard_failure"
                return finish(
                    RunResult(
                        status=status,
                        outcome_code=outcome.code,
                        message=outcome.message or f"Known outcome {outcome.code} detected",
                        failed_at_step=step.step_id if status == "hard_failure" else None,
                    )
                )

        return finish(self._evaluate_checkpoint(artifact, extracted))

    # ------------------------------------------------------------------ prerequisites

    def _run_prerequisites(self, artifact: Artifact, params: dict, steps: list) -> str | None:
        """Replay each capability this one declares it needs, in order.

        Parameters are passed through by name, so a capability requiring `operator_login`
        is handed whatever `username`/`password` the caller supplied without this engine
        having any idea what those mean. A prerequisite that does not succeed fails the run
        before any of its own steps execute, which is the difference between a clear
        "could not establish a session" and three confusing steps into a login page.
        """
        if not artifact.requires:
            return None
        if self.store is None:
            return (
                f"{artifact.capability_id} requires {artifact.requires} but no artifact "
                "store was provided to resolve them"
            )

        for capability_id in artifact.requires:
            try:
                prerequisite = self.store.load(capability_id)
            except FileNotFoundError:
                return (
                    f"{artifact.capability_id} requires capability {capability_id!r}, "
                    "which is not in the artifact store"
                )
            accepted = {p.name for p in prerequisite.input_params}
            result = self.run(prerequisite, {k: v for k, v in params.items() if k in accepted})
            steps.extend(result.steps_executed)
            if result.status != "success":
                return (
                    f"prerequisite {capability_id!r} did not succeed "
                    f"({result.status}: {result.outcome_code or result.message})"
                )
        return None

    # ------------------------------------------------------------------ params

    def _validate_params(self, artifact: Artifact, params: dict) -> str | None:
        declared = {p.name: p for p in artifact.input_params}
        for name, spec in declared.items():
            if spec.required and (name not in params or params[name] in (None, "")):
                return f"Missing required parameter {name!r} for {artifact.capability_id}"
            if name in params and spec.type in NUMERIC_TYPES:
                try:
                    float(str(params[name]).replace(",", ""))
                except ValueError:
                    return (
                        f"Parameter {name!r} must be a {spec.type}, got {params[name]!r}"
                    )
        # Parameters consumed by a prerequisite are legitimate here even though this
        # artifact does not declare them itself.
        unknown = set(params) - set(declared) - self._prerequisite_params(artifact)
        if unknown:
            return f"Unknown parameter(s) {sorted(unknown)} for {artifact.capability_id}"
        return None

    def _prerequisite_params(self, artifact: Artifact) -> set[str]:
        names: set[str] = set()
        for capability_id in artifact.requires:
            try:
                names |= {p.name for p in self.store.load(capability_id).input_params}
            except Exception:
                continue
        return names

    # ------------------------------------------------------------------ steps

    def _execute_step(
        self, artifact: Artifact, step: Step, params: dict, extracted: dict
    ) -> tuple[StepLog, object | None]:
        strategy = None

        if step.action == "navigate":
            assert isinstance(step.target, UrlTarget)
            self.surface.navigate(step.target.value)
        else:
            assert isinstance(step.target, ElementTarget)
            locator = step.target.locator.model_dump()
            element_id = self.surface.resolve_locator(locator)
            strategy = self.surface.last_resolution_strategy()

            if element_id is None:
                # A KB-sourced locator that no longer resolves is drift: flag it once here
                # and every artifact referencing that element benefits from the fix.
                if self.knowledge_base is not None and step.target.kb_element_id:
                    self.knowledge_base.mark_stale(
                        artifact.target_app.app_id, step.target.kb_element_id
                    )
                observed = ", ".join(
                    f"{el.role}:{el.name}" for el in self.surface.observe()[:15]
                )
                return (
                    StepLog(
                        step_id=step.step_id,
                        action=step.action,
                        description=f"{self._describe(step, params, artifact=artifact)} — target not found",
                        url=self._safe_url(),
                        verified=False,
                        error=(
                            f"Step {step.step_id}: expected {self._locator_text(step)} "
                            f"on {self.surface.current_url()}, but no element resolved. "
                            f"Observed instead: {observed}"
                        ),
                        screenshot=self._safe_screenshot(),
                    ),
                    None,
                )

            if step.action == "click":
                self.surface.click(element_id)
            elif step.action in ("type", "select"):
                self.surface.type_text(element_id, self._value_for(step, params))
            elif step.action == "extract":
                extracted[step.step_id] = self.surface.read_text(element_id)

        step_log = StepLog(
            step_id=step.step_id,
            action=step.action,
            description=self._describe(step, params, extracted, artifact=artifact),
            locator_strategy=strategy,
            url=self._safe_url(),
            verified=True,
            screenshot=self._safe_screenshot(),
        )
        return step_log, self._match_known_outcome(artifact)

    def _match_known_outcome(self, artifact: Artifact):
        if not artifact.known_outcomes:
            return None
        text = self.surface.visible_text().lower()
        url = self.surface.current_url().lower()
        for outcome in artifact.known_outcomes:
            detector = outcome.detector
            if detector.text_contains and detector.text_contains.lower() in text:
                return outcome
            if detector.url_contains and detector.url_contains.lower() in url:
                return outcome
        return None

    # ------------------------------------------------------------------ checkpoint

    def _evaluate_checkpoint(self, artifact: Artifact, extracted: dict) -> RunResult:
        checkpoint = artifact.checkpoint
        observed: str

        if checkpoint.type == "text_contains":
            observed = self.surface.visible_text()
            if checkpoint.text.lower() in observed.lower():
                return self._success(artifact, extracted)
            expectation = f"page text containing {checkpoint.text!r}"
        elif checkpoint.type == "url_contains":
            observed = self.surface.current_url()
            if checkpoint.text.lower() in observed.lower():
                return self._success(artifact, extracted)
            expectation = f"URL containing {checkpoint.text!r}"
        else:
            element_id = self.surface.resolve_locator(checkpoint.target.locator.model_dump())
            if element_id is None:
                observed = self.surface.current_url()
                expectation = (
                    f"element {checkpoint.target.locator.primary.value!r} "
                    f"matching /{checkpoint.pattern}/"
                )
            else:
                observed = self.surface.read_text(element_id).strip()
                if re.search(checkpoint.pattern, observed):
                    return self._success(artifact, extracted)
                expectation = (
                    f"element {checkpoint.target.locator.primary.value!r} "
                    f"matching /{checkpoint.pattern}/"
                )

        # Every step ran, no known outcome fired, and the goal condition still does not
        # hold: by definition the artifact met something it was not recorded to handle.
        last_step = artifact.steps[-1].step_id
        return RunResult(
            status="hard_failure",
            outcome_code="checkpoint_failed",
            message=(
                f"Checkpoint failed after step {last_step}. Expected {expectation}; "
                f"observed {self._truncate(observed)!r} at {self.surface.current_url()}."
            ),
            failed_at_step=last_step,
        )

    def _success(self, artifact: Artifact, extracted: dict) -> RunResult:
        outputs: dict[str, str] = {}
        for output in artifact.outputs:
            value = extracted.get(output.source_step)
            if value is None:
                return RunResult(
                    status="hard_failure",
                    outcome_code="output_missing",
                    message=(
                        f"Checkpoint passed but output {output.name!r} had no value from "
                        f"step {output.source_step}."
                    ),
                    failed_at_step=output.source_step,
                )
            outputs[output.name] = value.strip()
        return RunResult(status="success", outputs=outputs)

    # ------------------------------------------------------------------ helpers

    def _value_for(self, step: Step, params: dict) -> str:
        if step.value_from_param:
            return str(params[step.value_from_param])
        return str(step.value or "")

    @staticmethod
    def _display_value(artifact: Artifact, step: Step, value: str) -> str:
        """What may be written to a log, a report or a step description.

        A secret parameter's value is used to drive the page and is never rendered anywhere
        that persists. The artifact already holds only the parameter *name*, so this closes
        the remaining path by which a credential could reach disk.
        """
        if step.value_from_param and any(
            p.name == step.value_from_param and p.secret for p in artifact.input_params
        ):
            return "«redacted»"
        return repr(value)

    def _describe(self, step: Step, params: dict, extracted: dict | None = None,
                  artifact: Artifact | None = None) -> str:
        if step.description:
            base = step.description
        else:
            base = f"{step.action} {self._locator_text(step)}"
        if step.action == "navigate" and isinstance(step.target, UrlTarget):
            return f"Navigated to {step.target.value}"
        if step.action in ("type", "select"):
            value = self._value_for(step, params)
            shown = self._display_value(artifact, step, value) if artifact else repr(value)
            return f"{base} — entered {shown}"
        if step.action == "extract" and extracted is not None:
            return f"{base} — read {extracted.get(step.step_id, '')!r}"
        return base

    @staticmethod
    def _locator_text(step: Step) -> str:
        if isinstance(step.target, UrlTarget):
            return step.target.value
        primary = step.target.locator.primary
        return f"{primary.strategy}={primary.value!r}"

    @staticmethod
    def _truncate(text: str, limit: int = 160) -> str:
        flat = " ".join(text.split())
        return flat if len(flat) <= limit else flat[:limit] + "…"

    def _safe_url(self) -> str | None:
        try:
            return self.surface.current_url()
        except Exception:
            return None

    def _candidates(self) -> list[dict]:
        try:
            from engine.learning.outcome_learner import candidate_detectors

            return candidate_detectors(self.surface.observe(), self.surface.current_url())
        except Exception:
            return []

    def _safe_screenshot(self) -> bytes | None:
        try:
            return self.surface.screenshot()
        except Exception:
            return None
