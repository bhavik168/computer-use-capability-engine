"""The seam between the discovery loop and whichever model is driving it.

Two things live here, and the split matters. `Decision` is what the loop consumes — a
plain description of one proposed action, with no trace of any provider's wire format in
it. `LLMClient` is the contract a provider adapter implements, and it deliberately owns the
conversation history rather than handing it back to the caller, because message shape is the
single most provider-specific thing in an agent loop: Anthropic threads tool results by id
in a `messages` list, Gemini threads them by function name in `Content` parts with a `model`
role rather than `assistant`. A loop that assembled either shape itself would have to be
rewritten to change provider; this one does not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    """Anything that stopped the model from producing a usable decision."""


@dataclass
class Decision:
    """One grounded action the model proposed, in the loop's own vocabulary."""

    action: str
    rationale: str | None = None
    element_id: str | None = None
    text: str | None = None
    url: str | None = None
    goal_reached: bool = False
    goal_evidence: str | None = None
    raw: dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        """Dict-style access, so call sites read the same as the raw tool arguments did."""
        return getattr(self, key, default) if hasattr(self, key) else self.raw.get(key, default)


class LLMClient(ABC):
    """What the Discovery Engine needs from a model, and nothing more."""

    calls: int = 0

    @abstractmethod
    def start(self, system_prompt: str) -> None:
        """Begin a fresh conversation. Called once per discovery run."""

    @abstractmethod
    def decide(self, observation: str, tool_schema: dict) -> Decision:
        """Take one turn: send the observation, get back one proposed action.

        `tool_schema` is the grounded action tool for *this* turn — its `element_id` enum
        lists exactly the elements currently on screen. Every adapter must pass that schema
        through to the provider unmodified and must force the call, because the constraint
        is the anti-hallucination mechanism, not a hint.
        """

    @abstractmethod
    def record_result(self, message: str, ok: bool = True) -> None:
        """Tell the model what its last action actually did, so the next turn sees reality."""
