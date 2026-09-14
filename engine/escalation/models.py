"""The record of one human intervention."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class InterventionRequest(BaseModel):
    request_id: str
    created_at: datetime
    # Two callers, one mechanism: the discovery policy gate when an action is irreversible,
    # and the replay engine when it meets something it was never recorded to handle.
    source: Literal["discovery_policy_gate", "replay_unrecoverable"]
    goal_or_capability: str
    current_step: str
    reason: str
    screenshot_path: str | None = None
    url: str | None = None
    # Generalised, record-free detector suggestions for the state that stopped the run.
    # What turns an escalation into something the system learns from rather than just a
    # notification: the human picks one and names it.
    candidate_detectors: list[dict] = Field(default_factory=list)
    status: Literal["pending", "resolved"] = "pending"
    resolution_notes: str | None = None
    resolved_at: datetime | None = None
    extra: dict = Field(default_factory=dict)
