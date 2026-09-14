"""Step budget, wall-clock timeout, and cycle detection.

A fully grounded agent that never hallucinates can still fail to converge — it can bounce
between two screens forever, each action perfectly valid. That is a different failure mode
from hallucination and needs its own mechanism, which is what this is.

The cycle hash is computed over the *semantic* state (sorted role/name pairs plus the URL),
not over pixels: a screenshot hash would differ on a blinking cursor while the agent is
genuinely stuck, and would match across two different members' pages while it is genuinely
progressing.
"""

from __future__ import annotations

import hashlib
import time
from typing import Literal

from engine.surface.elements import ObservedElement

GuardVerdict = Literal["continue", "stop_step_budget", "stop_timeout", "stop_cycle"]


def state_hash(url: str, elements: list[ObservedElement]) -> str:
    signature = "|".join(
        sorted(f"{element.role}:{element.name}" for element in elements)
    )
    return hashlib.sha256(f"{url}||{signature}".encode()).hexdigest()[:16]


class LoopGuard:
    def __init__(
        self, max_steps: int = 25, timeout_seconds: int = 120, window: int = 10
    ) -> None:
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.window = window
        self.started = time.time()
        self._hashes: list[str] = []

    def record_state(
        self, url: str, elements: list[ObservedElement], mutating: bool = True
    ) -> None:
        """Note the state the next decision will be made from.

        `mutating` says whether the action that led here was *supposed* to change the
        screen. Reading a value is not: an `extract` leaves the page exactly as it was, so
        recording the resulting state would make the cycle detector conclude the agent is
        stuck at the precise moment it has finished gathering what the goal asked for. That
        killed every read-only capability one step before it could report success, so a
        non-mutating action contributes no hash — there is nothing meaningful to compare.
        """
        if not mutating:
            return
        self._hashes.append(state_hash(url, elements))
        self._hashes = self._hashes[-self.window :]

    def check(self, trace) -> GuardVerdict:
        if len(trace.steps) >= self.max_steps:
            return "stop_step_budget"
        if time.time() - self.started > self.timeout_seconds:
            return "stop_timeout"
        # The state we are about to decide from has already been seen in this window: the
        # last action changed nothing that matters, so deciding again will not either.
        if len(self._hashes) > 1 and self._hashes[-1] in self._hashes[:-1]:
            return "stop_cycle"
        return "continue"
