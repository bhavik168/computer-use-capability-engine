"""The redaction boundary that sits ahead of every write to the Evidence Store.

Right now this is a genuine no-op, and saying so plainly is better than pretending
otherwise: every record in the target app is fabricated, so there is nothing on screen worth
masking. What matters is that the *seam* is real — every screenshot in this system is
written through this function, so a production deployment adds masking in one place rather
than auditing every call site that ever touched an image.

A production implementation would blur or block out known-sensitive regions here (account
numbers, member names, balances) before anything reaches disk, driven by the same
accessibility-tree coordinates the rest of the system already works in. The Report Generator
never sees an unredacted image, because it only ever reads what this function returned.

Note what is *not* handled here, because it is handled by not collecting it at all: the
Knowledge Base stores roles, labels and locators and never element values, so there is no
redaction step needed on that path (see `knowledge_base/service.py`).
"""

from __future__ import annotations

import re

# Runs of digits long enough to be a record id, account number or amount. Masked before any
# observed string is persisted.
_DIGIT_RUN = re.compile(r"\d[\d,]{2,}(?:\.\d+)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact_text(text: str) -> str:
    """Mask record-identifying values in a string before it is stored or shown.

    This does double duty, which is why it is worth doing properly rather than stripping the
    string entirely. It keeps account numbers, amounts and addresses out of the escalation
    store and the Knowledge Base — and it *generalises* the string at the same time. An
    observed banner "Account 10007 is restricted" becomes "Account {n} is restricted", which
    is both safe to persist and the detector you actually want: one that matches the next
    restricted member too, not only the one that happened to fail today.
    """
    return _DIGIT_RUN.sub("{n}", _EMAIL.sub("{email}", text))


def redact_screenshot(image_bytes: bytes) -> bytes:
    """Return the image as it may be persisted. Currently identity; see module docstring."""
    return image_bytes
