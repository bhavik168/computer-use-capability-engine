"""What a replay run reports back to its caller.

The three-way split between a *business outcome* (the application said no, correctly and
predictably), a *recoverable* condition (the run can be retried after some recovery), and a
*hard failure* (something happened the artifact was never recorded to handle) is the whole
point of this model. Collapsing them into a boolean would throw away exactly the
information a calling agent needs to decide what to do next.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["success", "business_outcome", "recoverable", "hard_failure"]


class StepLog(BaseModel):
    """One executed step, in enough detail to reconstruct what happened without the run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    step_id: str
    action: str
    description: str
    locator_strategy: str | None = None  # "primary", "fallback[n]", or None for navigate
    url: str | None = None
    verified: bool = True
    rationale: str | None = None  # discovery runs only; replay has no LLM to quote
    error: str | None = None
    screenshot: bytes | None = Field(default=None, repr=False)
    screenshot_path: str | None = None


class RunResult(BaseModel):
    status: RunStatus
    outcome_code: str | None = None
    message: str | None = None
    outputs: dict = Field(default_factory=dict)
    steps_executed: list[StepLog] = Field(default_factory=list)
    failed_at_step: str | None = None
    run_id: str | None = None
    capability_id: str | None = None
    started_at: str | None = None
    duration_seconds: float | None = None

    def summary(self) -> str:
        if self.status == "success":
            return f"success · outputs={self.outputs}"
        return f"{self.status} · {self.outcome_code or ''} · {self.message or ''}".strip(" ·")
