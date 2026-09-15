"""The raw record of one discovery run.

A trace is what the Discovery Engine produces; an Artifact is what the Recorder compiles
from it. Keeping them separate matters: the trace holds everything that happened, including
the LLM's rationale and any backtracking, while the artifact holds only the path worth
replaying. The optimization review described in ARCHITECTURE.md §5.1 reads the trace
precisely because it still contains the detours the artifact drops.
"""

from __future__ import annotations

import re

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TraceStatus = Literal[
    "completed",
    "stopped_step_budget",
    "stopped_timeout",
    "stopped_cycle",
    "stopped_failed_step",
    "paused_for_escalation",
    "blocked_by_policy",
]


# Verbs that make a returned value part of the goal rather than a side effect. Deliberately
# about the *goal sentence*, not the application: "read the balance" and "get the account
# types" name a result the caller expects back, whoever wrote them and whatever the app is.
# Lives here because the discovery loop enforces it and the Recorder audits it, and they
# must agree — two copies of this regex would drift into a loop that refuses to finish a run
# the Recorder would have accepted.
_VALUE_VERBS = re.compile(
    r"\b(read|extract|get|fetch|retrieve|return|report|confirm|check|look\s+up|find\s+out)\b",
    re.IGNORECASE,
)


def goal_expects_value(goal: str | None) -> bool:
    """Whether the goal sentence names a value the caller expects handed back."""
    return bool(goal and _VALUE_VERBS.search(goal))


class TraceStep(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    step_id: str
    action: str
    rationale: str | None = None
    # What the element looked like at the moment it was acted on. This is the raw material
    # the Recorder turns into a persistent `role+name` locator.
    element_role: str | None = None
    element_name: str | None = None
    element_id: str | None = None
    # The persistent locator built at act time, while the element was still on screen.
    # This is what the Recorder freezes into the artifact.
    locator: dict | None = None
    url_value: str | None = None
    typed_value: str | None = None
    extracted_value: str | None = None
    # What the model called the value it read, used as the artifact's output field name.
    output_name: str | None = None
    risk_class: Literal["safe", "risky"] = "safe"
    url_before: str | None = None
    url_after: str | None = None
    verified: bool = True
    verification_note: str | None = None
    description: str = ""
    screenshot: bytes | None = Field(default=None, repr=False)
    screenshot_path: str | None = None

    def locator_value(self) -> str | None:
        if self.element_role and self.element_name:
            return f"{self.element_role}:{self.element_name}"
        return None


class DiscoveryTrace(BaseModel):
    run_id: str
    goal: str
    app_id: str
    target_url: str
    status: TraceStatus = "stopped_step_budget"
    stop_reason: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    llm_calls: int = 0
    started_at: str | None = None
    duration_seconds: float | None = None
    final_url: str | None = None
    goal_evidence: str | None = None  # what the model pointed at as proof the goal was met
    # How many times the loop sent the model back to extract a value it had only looked at.
    extract_refusals: int = 0

    def executed_steps(self) -> list[TraceStep]:
        return [step for step in self.steps if step.verified]
