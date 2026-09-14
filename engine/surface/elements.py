"""What a single observation of the target application looks like."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Accessible names that are really *data* rather than identity: currency figures, plain
# numbers, reference codes. Two places care — locator construction must not key an element
# on the value it exists to read, and the Knowledge Base must never persist one.
VALUE_LIKE_RE = re.compile(r"^[$€£]?[\d,]+(\.\d+)?$|^[A-Z]{2,}-[A-Z0-9-]+$")

# Roles that can be acted on. The Decide step's action list is built from these only.
INTERACTIVE_ROLES = frozenset(
    {"button", "link", "textbox", "tab", "checkbox", "combobox", "radio", "menuitem"}
)

# Roles that carry information worth reading but cannot be acted on. They are observed so
# the agent can see values it needs (a balance figure, a banner), and so that extraction
# locators have something to resolve against.
READABLE_ROLES = frozenset({"cell", "heading", "alert", "status", "rowheader"})


@dataclass(frozen=True)
class ObservedElement:
    """One element on the screen as it exists *right now*.

    ``element_id`` is synthetic and stable only within a single ``observe()`` call — it is
    reassigned by enumerating the accessibility tree in DOM order on every observation.
    That is deliberate: it is what constrains the LLM's Decide step to naming something
    that genuinely exists on the current screen. Persistent, replay-usable identity is a
    separate concept entirely — the artifact's `role+name` / CSS fallback chain.
    """

    element_id: str
    role: str
    name: str
    value: str | None = None
    disabled: bool = False
    frame_url: str | None = None
    interactive: bool = True

    def describe(self) -> str:
        bits = f"{self.element_id}: {self.role} {self.name!r}"
        if self.value:
            bits += f" (value={self.value!r})"
        if self.disabled:
            bits += " [disabled]"
        if not self.interactive:
            bits += " [read-only]"
        return bits
