"""Constrains what the model can propose, and verifies that what it proposed happened.

Three separate things are going on here, and it is worth being precise about which failure
each one addresses:

1. **Schema constraint.** The `element_id` parameter's allowed values are rebuilt every turn
   as an enum of exactly the ids in the current observation. The model is therefore
   *schema-incapable* of naming an element that is not on screen. This is stronger than
   asking it not to hallucinate in the prompt, because it does not depend on the model
   complying with anything.
2. **Re-verification before acting.** State can shift between the observation that built the
   schema and the moment the action runs. The chosen id is re-resolved against a fresh
   observation; a mismatch is a failed step, not a best-guess execution.
3. **Effect verification after acting.** A model narrating "I clicked Search" proves
   nothing. If the action produced no observable change in URL or in the set of elements on
   screen, the step is marked unverified rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.surface.base import Surface
from engine.surface.elements import ObservedElement

ACT_TOOL_NAME = "act"


def build_tool_schema(elements: list[ObservedElement]) -> list[dict]:
    """Build this turn's action tool, with element ids constrained to what exists now.

    The enum spans *every* observed element, not only the clickable ones. An earlier version
    restricted it to interactive elements, which quietly made `extract` impossible: a balance
    lives in a read-only table cell, so the one action the capability existed to perform
    could never name its target. Read-only ids are addressable for `extract` and useless for
    `click`/`type`, which the description says and the surface enforces anyway — a click on a
    static cell changes nothing observable and fails effect verification.
    """
    available = [element for element in elements if not element.disabled]
    element_ids = [element.element_id for element in available]
    readable_ids = [e.element_id for e in available if not e.interactive]

    element_property: dict = {
        "type": "string",
        "description": (
            "Which on-screen element to act on. Required for click/type/extract. "
            + (
                f"Read-only ids ({', '.join(readable_ids)}) hold text or values: use them "
                "with extract only, never click or type."
                if readable_ids
                else ""
            )
        ).strip(),
    }
    if element_ids:
        # The anti-hallucination mechanism: an id outside this enum cannot be expressed.
        element_property["enum"] = element_ids

    return [
        {
            "name": ACT_TOOL_NAME,
            "description": (
                "Take exactly one action against the application, or declare the goal "
                "reached. Every call must include your rationale."
            ),
            "strict": False,
            "input_schema": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "Why this action moves toward the goal, in one sentence.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["click", "type", "navigate", "extract", "done"],
                        "description": (
                            "click/type act on element_id; navigate loads a relative URL; "
                            "extract reads an element's text as a result; done ends the run."
                        ),
                    },
                    "element_id": element_property,
                    "text": {
                        "type": "string",
                        "description": "Text to enter, for action=type.",
                    },
                    "url": {
                        "type": "string",
                        "description": "Relative URL to load, for action=navigate.",
                    },
                    "goal_reached": {
                        "type": "boolean",
                        "description": (
                            "True only when the goal is demonstrably satisfied by what is "
                            "on screen right now."
                        ),
                    },
                    "goal_evidence": {
                        "type": "string",
                        "description": (
                            "When goal_reached is true, the exact on-screen text or value "
                            "that proves it."
                        ),
                    },
                },
                "required": ["rationale", "action"],
                "additionalProperties": False,
            },
        }
    ]


def render_observation(
    elements: list[ObservedElement], url: str, visible_text: str,
    available_params: list[str] | None = None,
) -> str:
    lines = [f"Current URL: {url}", ""]
    if available_params:
        lines += [
            "Values the caller supplied for this run. Type them as the placeholder shown —"
            " never invent a value, and never ask for one:",
            *(f"  {name} → type it as {{{{{name}}}}}" for name in available_params),
            "",
        ]
    lines.append("Elements you can click or type into:")
    for element in elements:
        if element.interactive and not element.disabled:
            lines.append(f"  {element.element_id}: {element.role} \"{element.name}\"")
    readable = [element for element in elements if not element.interactive and element.name]
    if readable:
        lines.append("")
        lines.append(
            "Read-only text and values. These carry ids too, so you can `extract` one when "
            "the task needs its value recorded — but you cannot click or type into them:"
        )
        for element in readable:
            lines.append(f"  {element.element_id}: {element.role} \"{element.name}\"")
    lines.append("")
    lines.append("Raw page text:")
    lines.append(_truncate(visible_text, 1500))
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    flat = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[:limit] + "\n…(truncated)"


@dataclass
class GroundingResult:
    ok: bool
    element: ObservedElement | None = None
    reason: str | None = None


def reverify(surface: Surface, element_id: str, expected: ObservedElement) -> GroundingResult:
    """Re-resolve the chosen element against a fresh observation, immediately before acting."""
    for element in surface.observe():
        if element.element_id != element_id:
            continue
        if element.role != expected.role or element.name != expected.name:
            return GroundingResult(
                False,
                reason=(
                    f"{element_id} changed between observation and action: expected "
                    f"{expected.role}:{expected.name!r}, now {element.role}:{element.name!r}"
                ),
            )
        return GroundingResult(True, element=element)
    return GroundingResult(False, reason=f"{element_id} is no longer on screen")


def verify_effect(
    url_before: str,
    elements_before: list[ObservedElement],
    url_after: str,
    elements_after: list[ObservedElement],
) -> GroundingResult:
    """Did the action actually change anything observable?"""
    if url_before != url_after:
        return GroundingResult(True)
    before = {(element.role, element.name, element.value) for element in elements_before}
    after = {(element.role, element.name, element.value) for element in elements_after}
    if before != after:
        return GroundingResult(True)
    return GroundingResult(
        False,
        reason="the action produced no observable change in URL or on-screen elements",
    )
