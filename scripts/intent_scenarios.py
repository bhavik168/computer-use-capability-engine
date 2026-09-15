"""Dev utility: exercise the intent parser without touching a browser or a model.

    python -m scripts.intent_scenarios
    python -m scripts.intent_scenarios --live   # also parse one goal with the real model

The offline pass stubs the model with canned plan JSON, so what it checks is the
plumbing either side of the model: that a plan is validated before it can run, that
replay-vs-discover is decided by what is actually in the store, that secrets survive
into the preview redacted, and that a failed call stops the plan instead of feeding a
dead session to the call after it. `--live` adds one real parse against the fixture
catalog and prints the plan; it still executes nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from engine.config import load_env
from engine.intent import (
    IntentParseError,
    execute_plan,
    format_plan_for_confirmation,
    parse_intent,
    reconcile_with_store,
)
from engine.storage.artifact_store import ArtifactStore

FIXTURES = Path("fixtures/corebank")
TARGET = "http://127.0.0.1:5050"

GOAL = (
    "Sign on to the console as operator1/pass123, then look up member 10001 "
    "and get their savings balance"
)

PLAN_JSON = json.dumps(
    {
        "calls": [
            {
                "capability_id": "operator_login",
                "description": "Sign on to the operator console.",
                "goal": "Sign on to the console with the given username and password.",
                "params": [
                    {"name": "username", "value": "operator1", "secret": False},
                    {"name": "password", "value": "pass123", "secret": True},
                ],
                "requires": [],
                "matched_existing": True,
            },
            {
                "capability_id": "check_savings_balance",
                "description": "Read a member's savings balance.",
                "goal": "Look up the member by id and extract the savings balance.",
                "params": [{"name": "member_id", "value": "10001", "secret": False}],
                "requires": ["operator_login"],
                "matched_existing": True,
            },
        ]
    }
)


class StubCompleter:
    """Stands in for the Gemini adapter's one-shot `complete`."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.seen_user: str | None = None

    def complete(self, system: str, user: str) -> str:
        self.seen_user = user
        return self.raw


def _fixture_store() -> ArtifactStore:
    return ArtifactStore(FIXTURES)


def _check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return condition


def offline() -> bool:
    store = _fixture_store()
    catalog = store.list_capabilities()
    ok = _check(
        "catalog reads the fixture artifacts",
        {c["capability_id"] for c in catalog} >= {"operator_login", "check_savings_balance"},
        str([c["capability_id"] for c in catalog]),
    )

    stub = StubCompleter(PLAN_JSON)
    calls = parse_intent(GOAL, catalog, stub)
    reconcile_with_store(calls, store)

    ok &= _check("the catalog is put in front of the model", "check_savings_balance" in (stub.seen_user or ""))
    ok &= _check("the plan is two ordered calls", [c.capability_id for c in calls] ==
                 ["operator_login", "check_savings_balance"])
    ok &= _check("the lookup requires the sign-on", calls[1].requires == ["operator_login"])
    ok &= _check("the password is marked secret", any(p.secret for p in calls[0].params))
    ok &= _check("both calls resolve to stored artifacts", all(c.matched_existing for c in calls))

    preview = format_plan_for_confirmation(calls)
    ok &= _check("the preview redacts the password", "pass123" not in preview and "[REDACTED]" in preview)
    ok &= _check("the preview names the member id", "member_id=10001" in preview)
    print("\n" + preview + "\n")

    # A requirement the plan does not contain would reach the executor as a capability
    # id nobody in this plan produces; it is refused before anything opens a browser.
    dangling = json.dumps(
        {"calls": [{"capability_id": "check_savings_balance", "description": "d", "goal": "g",
                    "params": [], "requires": ["operator_login"], "matched_existing": True}]}
    )
    try:
        parse_intent(GOAL, catalog, StubCompleter(dangling))
        ok &= _check("a forward/dangling requirement is refused", False)
    except IntentParseError as exc:
        ok &= _check("a forward/dangling requirement is refused", True, str(exc))

    try:
        parse_intent(GOAL, catalog, StubCompleter("not json at all"))
        ok &= _check("unparseable model output is refused", False)
    except IntentParseError:
        ok &= _check("unparseable model output is refused", True)

    # Execution: the branch per call, and what happens after one does not succeed.
    ran: list[str] = []

    seen_params: dict[str, dict] = {}

    def fake_replay(*, capability_id, target, params):
        ran.append(f"replay:{capability_id}")
        seen_params[capability_id] = params
        status = "success" if capability_id == "operator_login" else "business_outcome"
        return {"kind": "replay", "capability_id": capability_id, "status": status,
                "outputs": {}, "message": "member not found", "outcome_code": "member_not_found"}

    def fake_discover(**kwargs):
        ran.append(f"discover:{kwargs['capability_id']}")
        return {"kind": "discovery", "capability_id": kwargs["capability_id"], "status": "success"}

    results = execute_plan(calls, TARGET, store, fake_discover, fake_replay)
    ok &= _check("stored capabilities replay rather than rediscover",
                 ran == ["replay:operator_login", "replay:check_savings_balance"], str(ran))
    ok &= _check("a business outcome is reported as itself",
                 results[1]["status"] == "business_outcome")
    # The lookup re-establishes the session itself, so it needs the sign-on's params.
    ok &= _check("a call inherits the parameters of what it requires",
                 seen_params["check_savings_balance"] ==
                 {"username": "operator1", "password": "pass123", "member_id": "10001"},
                 str(seen_params["check_savings_balance"]))

    # An unrecorded capability is the discovery branch, and a failure there stops the plan.
    unknown = json.loads(PLAN_JSON)
    unknown["calls"][0]["capability_id"] = "operator_signon_v2"
    unknown["calls"][1]["requires"] = ["operator_signon_v2"]
    fresh = reconcile_with_store(parse_intent(GOAL, catalog, StubCompleter(json.dumps(unknown))), store)
    ok &= _check("an unrecorded capability is planned as a discovery", not fresh[0].matched_existing)

    ran.clear()

    def failing_discover(**kwargs):
        ran.append(f"discover:{kwargs['capability_id']}")
        return {"kind": "discovery", "capability_id": kwargs["capability_id"],
                "status": "hard_failure", "message": "step budget exhausted"}

    results = execute_plan(fresh, TARGET, store, failing_discover, fake_replay)
    ok &= _check("a failed call stops the plan", ran == ["discover:operator_signon_v2"], str(ran))
    ok &= _check("calls that did not run are reported as skipped",
                 [r["status"] for r in results] == ["hard_failure", "skipped"])
    return bool(ok)


def live() -> bool:
    from engine.discovery.gemini_client import GeminiClient
    from engine.discovery.llm_client import LLMError

    load_env()
    try:
        llm = GeminiClient()
    except LLMError as exc:
        print(f"SKIP  live parse — {exc}")
        return True

    store = _fixture_store()
    calls = reconcile_with_store(parse_intent(GOAL, store.list_capabilities(), llm), store)
    print("\nLive plan for the same goal:\n" + format_plan_for_confirmation(calls))
    return _check(
        "the live parse reuses the recorded capabilities rather than minting new ones",
        all(c.matched_existing for c in calls),
        str([c.capability_id for c in calls]),
    )


def main() -> None:
    ok = offline()
    if "--live" in sys.argv:
        ok = live() and ok
    print("\nAll checks passed." if ok else "\nSome checks failed.")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
