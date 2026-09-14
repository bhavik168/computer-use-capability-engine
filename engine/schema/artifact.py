"""The capability artifact — the typed, versioned contract of this system.

A successful discovery run is compiled into one of these (see the Recorder), and the
Replay Engine executes it later with no LLM anywhere in the decision loop. Because both
sides depend on this shape, it is defined once here and validated on every read and write.

Enum-like fields use ``Literal`` deliberately: an invalid action or outcome type must fail
validation rather than travel silently into the replay executor.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

Action = Literal["navigate", "click", "type", "select", "extract"]
RiskClass = Literal["safe", "risky"]
ArtifactStatus = Literal["draft", "approved", "deprecated"]
OutcomeType = Literal["business_outcome", "recoverable", "hard_failure"]
LocatorStrategy = Literal["role+name", "css", "text"]
ValueType = Literal["string", "number", "currency", "boolean"]


class StrictModel(BaseModel):
    """Unknown keys are a mistake, not something to shrug at and drop."""

    model_config = ConfigDict(extra="forbid")


class LocatorRule(StrictModel):
    """One way of finding an element on screen.

    ``role+name`` values are written ``"<role>:<accessible name>"`` — e.g.
    ``"textbox:Customer ID"`` — which is the primary strategy everywhere it is usable,
    because roles and accessible names survive the layout churn that CSS selectors do not.
    """

    strategy: LocatorStrategy
    value: str


class Locator(StrictModel):
    primary: LocatorRule
    fallbacks: list[LocatorRule] = Field(default_factory=list)


class UrlTarget(StrictModel):
    kind: Literal["url"]
    value: str


class ElementTarget(StrictModel):
    """An element addressed by locator, optionally also by Knowledge Base element id.

    ``kb_element_id`` may be null: the Knowledge Base is layered on in a later phase, and
    nothing resolves these ids yet. When it is populated, a single drift fix in the KB
    propagates to every artifact referencing that element.
    """

    kb_element_id: str | None = None
    locator: Locator


class Step(StrictModel):
    step_id: str
    action: Action
    target: UrlTarget | ElementTarget
    # Exactly one source of the value for `type`/`select`: a run parameter or a literal.
    value_from_param: str | None = None
    value: str | None = None
    risk_class: RiskClass = "safe"
    description: str | None = None

    @model_validator(mode="after")
    def _check_target_and_value(self) -> "Step":
        if self.action == "navigate":
            if not isinstance(self.target, UrlTarget):
                raise ValueError("navigate steps need a target of kind 'url'")
        elif not isinstance(self.target, ElementTarget):
            raise ValueError(f"{self.action} steps need an element target with a locator")

        if self.value_from_param and self.value is not None:
            raise ValueError(
                "a step takes its value from either value_from_param or value, not both"
            )
        if self.action in ("type", "select") and not (self.value_from_param or self.value):
            raise ValueError(f"{self.action} steps need value_from_param or value")
        return self


class Checkpoint(StrictModel):
    """The condition that means the capability actually achieved its goal.

    Checked once, after every step has executed. A run whose steps all completed but whose
    checkpoint does not hold is a hard failure — the artifact did something other than what
    it was recorded to do.
    """

    type: Literal["element_present_with_pattern", "text_contains", "url_contains"]
    target: ElementTarget | None = None
    pattern: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "Checkpoint":
        if self.type == "element_present_with_pattern":
            if self.target is None or self.pattern is None:
                raise ValueError(
                    "element_present_with_pattern checkpoints need a target and a pattern"
                )
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"checkpoint pattern is not a valid regex: {exc}") from exc
        elif self.text is None:
            raise ValueError(f"{self.type} checkpoints need a 'text' value")
        return self


class Output(StrictModel):
    name: str
    type: ValueType
    source_step: str


class Detector(StrictModel):
    """How a known outcome is recognised on screen.

    Two kinds only, deliberately: a case-insensitive substring of the page's visible text,
    and a substring of the current URL. That covers every outcome the target app produces,
    and anything richer would be schema surface built ahead of a need.
    """

    text_contains: str | None = None
    url_contains: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "Detector":
        if not self.text_contains and not self.url_contains:
            raise ValueError("a detector needs text_contains or url_contains")
        return self


class KnownOutcome(StrictModel):
    code: str
    type: OutcomeType
    detector: Detector
    message: str | None = None
    recovery: str | None = None


class InputParam(StrictModel):
    name: str
    type: ValueType
    required: bool = True
    description: str | None = None
    example: str | None = None
    # A secret is supplied per invocation and never stored: the artifact records only that
    # a step reads this parameter, never the value, and every log line and report redacts
    # it. This is why authentication is an ordinary capability here rather than a special
    # case — a recorded login holds no credential.
    secret: bool = False

    @model_validator(mode="after")
    def _no_secret_examples(self) -> "InputParam":
        if self.secret and self.example is not None:
            raise ValueError(f"parameter {self.name!r} is secret; it cannot carry an example")
        return self


class TargetApp(StrictModel):
    app_id: str
    surface_type: Literal["web", "desktop"]


class Provenance(StrictModel):
    created_from_run: str
    created_at: str
    last_validated_at: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class Review(StrictModel):
    """The optimization review is human-triggered and offline; see ARCHITECTURE.md §5.1."""

    status: Literal["not_reviewed", "reviewed"] = "not_reviewed"
    optimization_notes: str | None = None
    reviewed_at: str | None = None


class Artifact(StrictModel):
    capability_id: str
    version: str
    status: ArtifactStatus = "draft"
    description: str
    target_app: TargetApp
    # Capabilities that must succeed first — an authenticated session, a selected branch,
    # whatever this application happens to require. Expressed as capability ids rather than
    # as engine behaviour, so "log in first" is a fact about one application that was
    # discovered and recorded, not an assumption the engine carries everywhere.
    requires: list[str] = Field(default_factory=list)
    # Derived, never authored: overwritten on validation with the max risk across steps.
    risk_class: RiskClass = "safe"
    provenance: Provenance
    input_params: list[InputParam] = Field(default_factory=list)
    steps: list[Step]
    checkpoint: Checkpoint
    outputs: list[Output] = Field(default_factory=list)
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    review: Review = Field(default_factory=Review)

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v

    @model_validator(mode="after")
    def _derive_and_check(self) -> "Artifact":
        if not self.steps:
            raise ValueError("an artifact needs at least one step")

        step_ids = [s.step_id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step_ids must be unique within an artifact")

        param_names = {p.name for p in self.input_params}
        for step in self.steps:
            if step.value_from_param and step.value_from_param not in param_names:
                raise ValueError(
                    f"step {step.step_id} reads undeclared parameter "
                    f"{step.value_from_param!r}"
                )
        for output in self.outputs:
            if output.source_step not in step_ids:
                raise ValueError(
                    f"output {output.name!r} names unknown source_step "
                    f"{output.source_step!r}"
                )

        # The artifact's risk is the risk of its riskiest step. Computed here rather than
        # trusted from the file so an artifact can never under-declare what it does.
        object.__setattr__(
            self,
            "risk_class",
            "risky" if any(s.risk_class == "risky" for s in self.steps) else "safe",
        )
        return self
