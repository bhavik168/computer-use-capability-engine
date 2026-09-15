"""Compiles a successful discovery trace into a reusable capability.

This is the moment a one-off, model-driven run becomes something a production agent can
invoke for a page load's worth of cost. It is intentionally thin — the hard work happened
during discovery; this reshapes and persists it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from engine.discovery.trace import DiscoveryTrace, TraceStep, goal_expects_value
from engine.reporting.redaction import stable_fragment
from engine.schema.artifact import (
    Artifact,
    Checkpoint,
    ElementTarget,
    InputParam,
    KnownOutcome,
    Locator,
    Output,
    Provenance,
    Review,
    Step,
    TargetApp,
    UrlTarget,
)

log = logging.getLogger(__name__)

class Recorder:
    def __init__(self, knowledge_base=None) -> None:
        # The recorder knows nothing about any application, and nothing is declared to it.
        # A discovery run only walks the success path — by definition it stops when the goal
        # is met — so it cannot observe an application's failure modes. Those are *learned*:
        # a replay meets something it was not recorded to handle, a human names it, and the
        # Knowledge Base remembers it for that application. A capability recorded on a
        # cold KB therefore starts with no known outcomes and treats the first "not found"
        # it ever meets as a hard failure. That is correct, not a gap — the system has
        # genuinely never seen one — and it is how the KB fills up, one outcome at a time.
        self.knowledge_base = knowledge_base

    def compile(
        self,
        trace: DiscoveryTrace,
        capability_id: str,
        description: str,
        param_overrides: dict[str, str] | None = None,
        secret_params: set[str] | None = None,
        requires: list[str] | None = None,
        version: str = "1.0.0",
        param_values: dict[str, str] | None = None,
    ) -> Artifact:
        if trace.status != "completed":
            raise ValueError(
                f"refusing to compile trace {trace.run_id}: status is {trace.status!r}, "
                "not 'completed'"
            )

        # The run's opening navigation happens before the loop and so is not a trace step,
        # but a replay has to start somewhere — without this the artifact opens on a blank
        # page and fails on its first locator. Recorded as a relative path so the same
        # capability replays against any deployment of the application.
        steps: list[Step] = [
            Step(
                step_id="s0",
                action="navigate",
                target=UrlTarget(kind="url", value=_relative(trace.target_url)),
                description=f"Open the capability's entry point, {_relative(trace.target_url)}",
            )
        ]
        # The values the caller supplied for this discovery run, longest first so that a
        # value which contains another (member_id "10002" contains the deposit "100") is
        # matched before the shorter one and never half-templated. Used to lift a hard-coded
        # value back out of a locator whose accessible name embeds it — the result link's
        # name *is* "10007", the detail heading *is* "…Frank Osei (10007)" — so the compiled
        # capability targets `link:{{member_id}}`, not one specific member.
        self._templatable = sorted(
            (
                (str(value), name)
                for name, value in (param_values or {}).items()
                if value and len(str(value)) >= 3
            ),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        params: dict[str, InputParam] = {}
        outputs: list[Output] = []
        last_extract: TraceStep | None = None

        for trace_step in trace.steps:
            if not trace_step.verified:
                continue  # a step that changed nothing is not part of the path

            if trace_step.action == "navigate":
                steps.append(
                    Step(
                        step_id=trace_step.step_id,
                        action="navigate",
                        target=UrlTarget(kind="url", value=_relative(trace_step.url_value)),
                        description=trace_step.description,
                    )
                )
                continue

            locator = trace_step.locator or self._fallback_locator(trace_step)
            if locator is None:
                log.warning("skipping step %s: no usable locator", trace_step.step_id)
                continue

            target = ElementTarget(
                # Backfilled by the Knowledge Base once it owns the element registry.
                kb_element_id=None,
                locator=Locator.model_validate(self._templatize_locator(locator)),
            )

            if trace_step.action == "type":
                param_name = self._param_name(trace_step, param_overrides)
                is_secret = param_name in (secret_params or set())
                params.setdefault(
                    param_name,
                    InputParam(
                        name=param_name,
                        type="string",
                        required=True,
                        description=f"Value for the {trace_step.element_name!r} field",
                        # A secret carries no example, by schema rule. For anything else the
                        # discovery value is a genuinely useful hint to a calling agent.
                        example=None if is_secret else self._example(trace_step),
                        secret=is_secret,
                    ),
                )
                steps.append(
                    Step(
                        step_id=trace_step.step_id,
                        action="type",
                        target=target,
                        value_from_param=param_name,
                        risk_class=trace_step.risk_class,
                        description=trace_step.description,
                    )
                )
                continue

            steps.append(
                Step(
                    step_id=trace_step.step_id,
                    action=trace_step.action,
                    target=target,
                    risk_class=trace_step.risk_class,
                    description=trace_step.description,
                )
            )
            if trace_step.action == "extract":
                last_extract = trace_step

        if last_extract is not None:
            outputs.append(
                Output(
                    name=self._output_name(trace, last_extract),
                    type="currency" if _looks_like_money(last_extract.extracted_value) else "string",
                    source_step=last_extract.step_id,
                )
            )

        # A goal phrased as "read the balance" that recorded no extract has produced a
        # capability returning nothing — the model satisfied itself by *seeing* the value
        # and quoting it as evidence rather than recording it. The artifact is still
        # compiled, because the navigation it learned is genuinely useful, but it must not
        # pass as a finished capability: the checkpoint in that case degrades to matching
        # this run's literal answer, which is not an assertion about any future run.
        review = Review()
        confidence = self._confidence(trace)
        if not outputs and goal_expects_value(trace.goal):
            log.warning(
                "%s: goal asks for a value but no extract was recorded — the capability "
                "returns no outputs and its checkpoint falls back to literal page text",
                capability_id,
            )
            review = Review(
                optimization_notes=(
                    "Goal names a value to return, but discovery recorded no extract step, "
                    "so outputs is empty and the checkpoint matches this run's literal "
                    "text. Re-record, or add the output and checkpoint by hand, before "
                    "trusting this capability to return data."
                )
            )
            confidence = round(max(confidence - 0.3, 0.1), 2)

        artifact = Artifact(
            capability_id=capability_id,
            version=version,
            status="draft",
            description=description,
            target_app=TargetApp(app_id=trace.app_id, surface_type="web"),
            requires=list(requires or []),
            provenance=Provenance(
                created_from_run=trace.run_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                last_validated_at=None,
                confidence=confidence,
            ),
            input_params=list(params.values()),
            steps=steps,
            checkpoint=self._checkpoint(trace, last_extract),
            outputs=outputs,
            known_outcomes=[
                KnownOutcome.model_validate(entry)
                for entry in (
                    self.knowledge_base.outcomes(trace.app_id) if self.knowledge_base else []
                )
            ],
            review=review,
        )
        return artifact

    # ------------------------------------------------------------------ pieces

    def _checkpoint(self, trace: DiscoveryTrace, last_extract: TraceStep | None) -> Checkpoint:
        """The goal condition, taken from whatever ended the discovery loop.

        When the run finished by reading a value, the checkpoint is that the same element
        still holds a value of that shape — which is a real assertion about a future run,
        not a recording of this run's answer. Otherwise it falls back to the evidence text
        the model quoted when it declared the goal met.
        """
        if last_extract is not None and last_extract.locator:
            return Checkpoint(
                type="element_present_with_pattern",
                target=ElementTarget(
                    kb_element_id=None,
                    locator=Locator.model_validate(
                        self._templatize_locator(last_extract.locator)
                    ),
                ),
                pattern=_pattern_for(last_extract.extracted_value),
            )
        # Otherwise the model's own proof text becomes the assertion — but only the part of
        # it that will still be true next time. Quoted raw, "Sub-account successfully
        # created. Sub-Account ID SUB-10002-03" pins the checkpoint to one run's generated
        # id and to one caller's parameters, so the capability fails its own next replay.
        # Stripping the caller's values and then generalising record data leaves the part
        # that is actually the success condition: the application's own confirmation wording.
        evidence = self._strip_param_values((trace.goal_evidence or "").strip(), as_template=False)
        fragment = stable_fragment(evidence) if evidence else None
        if fragment:
            return Checkpoint(type="text_contains", text=fragment[:80])
        return Checkpoint(type="url_contains", text=_relative(trace.final_url))

    PLACEHOLDER = re.compile(r"^\{\{(\w+)\}\}$")

    def _param_name(self, step: TraceStep, overrides: dict[str, str] | None) -> str:
        """Name the parameter a typed value came from.

        When the caller supplied the value, the trace holds the placeholder the model typed
        (`{{member_id}}`) rather than the value itself, so the mapping is exact and needs no
        guessing. Only a value the model composed for itself falls back to a heuristic —
        naming the parameter after the field's accessible name — and even then it becomes a
        parameter rather than a literal, because a typed value in a business application is
        the caller's data far more often than it is a fixed UI constant, and a wrong call
        here costs one edit rather than a silently hard-coded artifact.
        """
        if overrides and step.step_id in overrides:
            return overrides[step.step_id]
        typed = (step.typed_value or "").strip()
        match = self.PLACEHOLDER.match(typed)
        if match:
            return match.group(1)
        # The model is asked to type `{{partial_ssn}}`, but when the goal sentence quotes the
        # value ("search by partial SSN 6789") it will often type the literal instead. The
        # value is still the caller's, so recognising it recovers the caller's name for it —
        # without this the parameter is named after the input field it happened to land in
        # (`search_by_member_id_name_or_partial_ssn`), which is the screen's vocabulary
        # rather than the capability's contract.
        for value, name in self._templatable:
            if typed == value:
                return name
        return _slug(step.element_name or step.step_id)

    @staticmethod
    def _example(step: TraceStep) -> str | None:
        value = (step.typed_value or "").strip()
        return None if Recorder.PLACEHOLDER.match(value) else (value or None)

    def _output_name(self, trace: DiscoveryTrace, step: TraceStep) -> str:
        # The model names the value as it reads it, which is the only point in the system
        # where the *meaning* of a table cell is known — the cell's accessible name is the
        # balance itself, and the column header it sits under is not part of the element.
        # A caller reading back `savings_balance` has a contract; one reading back `result`
        # has a string. The name is still sanitised: it is model-supplied text heading for
        # a schema field, so it is slugged and rejected if it turns out to be the value.
        if step.output_name:
            named = _slug(self._strip_param_values(step.output_name, as_template=False))
            if named and not named[0].isdigit() and not _looks_like_money(step.output_name):
                return named
        # Drop any caller-supplied value out of the name too, so a member-scoped extract is
        # not immortalised as `member_detail_frank_osei_10007`.
        raw = self._strip_param_values(step.element_name or "", as_template=False)
        name = _slug(raw)
        if not name or name == "value" or name[0].isdigit() or _looks_like_money(step.element_name):
            return "result"
        return name

    def _fallback_locator(self, step: TraceStep) -> dict | None:
        value = step.locator_value()
        if not value:
            return None
        return {"primary": {"strategy": "role+name", "value": value}, "fallbacks": []}

    def _templatize_locator(self, locator: dict) -> dict:
        """Rewrite a supplied parameter value embedded in a locator into `{{param}}`.

        A `role+name` locator carries the element's accessible name, and for a search result
        or a record heading that name *is* the identifier the caller passed in — `link:10007`,
        `heading:Member Detail — Frank Osei (10007)`. Frozen literally, the capability only
        ever resolves for that one member and silently rides positional fallbacks for any
        other. Substituting the value back out (`link:{{member_id}}`) makes the locator mean
        what discovery actually did: act on *the member the caller named*. Replay renders the
        placeholder from its params before resolving. A no-op when no value is embedded, so
        stable locators like `textbox:Username` are untouched.
        """
        if not self._templatable:
            return locator

        def render(rule: dict) -> dict:
            return {**rule, "value": self._strip_param_values(rule["value"], as_template=True)}

        return {
            "primary": render(locator["primary"]),
            "fallbacks": [render(rule) for rule in locator.get("fallbacks", [])],
        }

    def _strip_param_values(self, text: str, *, as_template: bool) -> str:
        """Replace each embedded parameter value with its `{{name}}` template (or drop it).

        Longest value first (see `self._templatable`) so an overlapping shorter value cannot
        corrupt a substitution already made.
        """
        for value, name in self._templatable:
            if value in text:
                text = text.replace(value, f"{{{{{name}}}}}" if as_template else "")
        return text

    @staticmethod
    def _confidence(trace: DiscoveryTrace) -> float:
        steps = [step for step in trace.steps if step.verified]
        if not steps:
            return 0.0
        confidence = 1.0
        if any(not step.verified for step in trace.steps):
            confidence -= 0.2
        if trace.llm_calls > len(steps) * 1.5:
            confidence -= 0.2  # noisy grounding: many calls per executed step
        return round(max(confidence, 0.1), 2)


# ---------------------------------------------------------------------- helpers


def _relative(url: str | None) -> str:
    if not url:
        return "/"
    parsed = urlparse(url)
    return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "value"


def _looks_like_money(value: str | None) -> bool:
    return bool(value and re.match(r"^[$€£]?[\d,]+\.\d{2}$", value.strip()))


def _pattern_for(value: str | None) -> str:
    if _looks_like_money(value):
        return r"^[$€£]?[0-9,]+\.[0-9]{2}$"
    return r"\S"
