"""Dev utility: find out which Gemini models this key can actually use, right now.

    python -m scripts.probe_models              # probe every candidate
    python -m scripts.probe_models 3.8 flash    # probe only names matching a substring

Model availability is not static. A name can be listed by the API and still refuse the
call, for three different reasons that need three different responses:

    404  retired          — the name is dead; pick another.
    429  limit: 0         — real, but not on this tier; enable billing or pick another.
    503  high demand      — fine, just busy; worth retrying later.

So this probes the model the way the discovery loop actually uses it — a forced tool call
through the real `GeminiClient`, not a bare `generate_content` — because a model that
answers prose but cannot honour forced tool use is no good to the loop either.
"""

from __future__ import annotations

import os
import sys

from engine.config import load_env

load_env()

from engine.discovery.gemini_client import GeminiClient  # noqa: E402  (needs env first)

# One tiny screen, two turns: enough to prove forced tool use and history threading work.
SCHEMA = {
    "name": "act",
    "description": "Take one action on the current screen.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["click", "type", "finish"]},
            "element_id": {"type": "string", "enum": ["e1", "e2"]},
            "rationale": {"type": "string"},
        },
        "required": ["action"],
    },
}
SCREEN = "Screen: login page.\n  e1 textbox 'Username'\n  e2 button 'Sign on'"


def candidates(filters: list[str]) -> list[str]:
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    names = []
    for m in client.models.list():
        if "generateContent" not in (getattr(m, "supported_actions", None) or []):
            continue
        name = m.name.removeprefix("models/")
        # Image, TTS and embedding variants cannot drive a UI loop.
        if any(skip in name for skip in ("image", "tts", "embedding", "aqa", "lyria")):
            continue
        if filters and not any(f in name for f in filters):
            continue
        names.append(name)
    return sorted(names)


def probe(name: str) -> tuple[str, str]:
    os.environ["GEMINI_MODEL"] = name
    try:
        client = GeminiClient()
        client.start("You operate a UI. Always answer with the `act` tool.")
        first = client.decide(SCREEN, SCHEMA)
        client.record_result("typed ok")
        second = client.decide(SCREEN.replace("login page", "username filled"), SCHEMA)
        return "OK", f"{first.action}/{first.element_id} then {second.action}/{second.element_id}"
    except Exception as exc:  # noqa: BLE001 — the reason is the result here
        text = str(exc)
        for code, label in (
            ("404", "retired"),
            ("429", "no quota on this tier"),
            ("503", "busy, retry later"),
        ):
            if code in text:
                return code, label
        return "ERR", text[:70]


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set.")
        return 2

    filters = sys.argv[1:]
    names = candidates(filters)
    if not names:
        print("No models matched.")
        return 1

    print(f"Probing {len(names)} model(s) with a real forced tool call.\n")
    usable = []
    for name in names:
        status, detail = probe(name)
        print(f"  [{status:>3}] {name:<34} {detail}")
        if status == "OK":
            usable.append(name)

    print()
    if usable:
        print("Usable now:")
        for name in usable:
            print(f"  GEMINI_MODEL={name}")
    else:
        print("Nothing usable right now. 503s are transient — try again shortly.")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
