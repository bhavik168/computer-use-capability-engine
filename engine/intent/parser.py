"""
Intent parser — the front door for a plain-language goal.

Turns a prompt like:

    "Sign on to the console as operator1/pass123, then look up
    member 10001 and get their savings balance"

into an ordered list of CapabilityCall objects that the existing
engine.cli discover/replay machinery already knows how to execute.
This is the piece ARCHITECTURE.md's "Capability API" was always
supposed to be the front of — it decides which capability(ies) a goal
needs, in what order, and whether each one is a fresh discovery or a
replay of something already recorded, so a human (or an AI agent)
never hand-assembles --param/--capability-id/--requires flags.

Design notes
------------
- One LLM call, structured JSON output, no tool loop. Parsing a goal
  into a plan is a different problem than discovering how to execute
  it, and doesn't need the Decide/Observe/Act machinery.

- The parser is handed the current capability catalog (id,
  description, input param names) specifically so it can match new
  phrasing against an existing capability rather than minting a
  duplicate. "Look up a member and read their balance" and "check
  member X's savings balance" should both resolve to the same
  check_savings_balance capability_id, not fork into two artifacts
  that do the same thing.

- Secret detection is a first pass, not the last line of defense.
  Params named password/secret/token/pin/credential, or explicitly
  called out as sensitive in the prompt, get secret=True here — but
  this is upstream of the parameterization/redaction guards already
  in the engine, which are what actually stop a secret from being
  written into an artifact or log. This parser being wrong about a
  secret should degrade to "caught downstream," not "leaked."

- Parsing produces a plan; it does not execute it. Keeping propose
  and execute as separate steps means the plan can be printed and
  confirmed before anything touches a live system — the same
  propose -> gate -> act shape as the Discovery Engine's own policy
  gate, just one level up.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Protocol

log = logging.getLogger(__name__)

# Names that mean "this value must never reach an artifact, a log or a report",
# regardless of what the model decided. Applied after parsing as a floor under the
# model's own judgement — the model may add secrets, it may not remove these.
SECRET_NAME_RE = re.compile(r"password|secret|token|\bpin\b|credential|api[_-]?key", re.I)


class TextCompleter(Protocol):
    """The one model call this module needs.

    Deliberately not folded into `LLMClient`: that ABC is the contract of the
    discovery *loop* (start / decide / record_result, with a forced grounded tool
    call every turn), and parsing a goal is a single stateless completion. The
    Gemini adapter satisfies both, so there is still only one integration path to
    the model API, but a provider that only ever parsed intent would not have to
    implement an agent loop to do it.
    """

    def complete(self, system: str, user: str) -> str: ...


@dataclass
class ParsedParam:
    name: str
    value: str
    secret: bool = False


@dataclass
class CapabilityCall:
    capability_id: str
    description: str
    goal: str                        # imperative phrasing for Discover, only used if a fresh run is needed
    params: List[ParsedParam]
    requires: List[str] = field(default_factory=list)
    matched_existing: bool = False   # True if this reused a catalog id instead of minting a new one


class IntentParseError(RuntimeError):
    """The model did not return a plan this engine can execute."""


PARSE_SYSTEM_PROMPT = """You convert a plain-language operator goal into an ordered list of \
capability calls for a computer-use automation engine.

You are given:
- the user's natural-language goal
- the catalog of capabilities that already exist (id, description, input parameter names)

Rules:
- Break the goal into the minimum number of ordered capability calls needed. A goal that \
implies "first authenticate, then do X" is two calls, with the second requiring the first's \
capability_id.
- For each call, if it matches an EXISTING catalog capability closely enough that the same \
recorded flow would satisfy it (same underlying action, same or overlapping required params), \
reuse that capability_id exactly and set matched_existing=true. Do not mint a new capability_id \
for something that already exists under different phrasing.
- If nothing in the catalog matches, propose a new capability_id: lowercase, underscore-\
separated, action-oriented (e.g. check_savings_balance, operator_login). Set matched_existing=false.
- Extract every concrete value the user actually supplied as a named param (member id, \
username, password, amount, etc). Never invent a value that wasn't given.
- Mark a param secret=true if its name matches password/secret/token/pin/credential, or if the \
user's phrasing asks for it to be kept secret, even if the field name itself doesn't say so.
- "requires" lists capability_id(s) of OTHER CALLS IN THIS SAME PLAN that must run first (e.g. a \
lookup requires the login call's capability_id). Never reference a capability outside this plan.
- "goal" is imperative phrasing for an agent driving the UI, used only if a fresh discovery run \
is needed for this call. If the call implies reading a value back, the phrasing must explicitly \
say "...and extract the X" rather than "...and check X" — vague phrasing has previously caused \
weaker models to look at a value without extracting it.

Return ONLY valid JSON, no prose, matching this shape:
{
  "calls": [
    {
      "capability_id": "...",
      "description": "...",
      "goal": "...",
      "params": [{"name": "...", "value": "...", "secret": false}],
      "requires": ["..."],
      "matched_existing": false
    }
  ]
}
"""


def parse_intent(
    prompt: str,
    known_capabilities: List[Dict[str, Any]],
    llm_client: TextCompleter,
) -> List[CapabilityCall]:
    """
    known_capabilities: [{"capability_id": ..., "description": ..., "input_params": [...]}]
    read from the Artifact Store's catalog listing.

    llm_client: the same thin LLM wrapper the Discovery Engine's
    Decide step already uses — reused here rather than introducing a
    second integration path to the model API.
    """
    catalog_text = json.dumps(known_capabilities, indent=2)
    user_content = f"Goal:\n{prompt}\n\nExisting capability catalog:\n{catalog_text}"

    raw = llm_client.complete(
        system=PARSE_SYSTEM_PROMPT,
        user=user_content,
        # low temperature / structured-output mode if your client
        # supports it — this is parsing, not open-ended reasoning
    )

    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise IntentParseError(
            f"the model did not return valid JSON for this goal: {exc}"
        ) from exc

    try:
        calls = [
            CapabilityCall(
                capability_id=c["capability_id"],
                description=c["description"],
                goal=c["goal"],
                params=[
                    ParsedParam(
                        name=p["name"],
                        value=str(p["value"]),
                        # The model's secret flag is a floor, not a ceiling: a name that
                        # looks like a credential is treated as one even if the model
                        # said otherwise. Being wrong in this direction costs a redacted
                        # line in a report; being wrong in the other costs a leak.
                        secret=bool(p.get("secret")) or bool(SECRET_NAME_RE.search(p["name"])),
                    )
                    for p in c.get("params", [])
                ],
                requires=list(c.get("requires", [])),
                matched_existing=bool(c.get("matched_existing", False)),
            )
            for c in data["calls"]
        ]
    except (KeyError, TypeError) as exc:
        raise IntentParseError(f"plan JSON was missing a required field: {exc}") from exc

    _check_plan(calls)
    return calls


def _strip_code_fence(raw: str) -> str:
    """Tolerate ```json fencing around an otherwise fine plan."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _check_plan(calls: List[CapabilityCall]) -> None:
    """Reject a plan that cannot be executed as written, before anything runs.

    Two failures matter here. A `requires` naming something outside the plan would
    reach the executor as a capability id that may not exist in the store at all; and
    a requirement pointing *forward* in the order would have the engine try to
    establish a session with a capability that has not run yet. Both are cheap to
    catch here and confusing to diagnose three browser windows later.
    """
    if not calls:
        raise IntentParseError("the model returned an empty plan for this goal")

    seen: set[str] = set()
    for call in calls:
        for requirement in call.requires:
            if requirement == call.capability_id:
                raise IntentParseError(f"{call.capability_id} requires itself")
            if requirement not in seen:
                raise IntentParseError(
                    f"{call.capability_id} requires {requirement!r}, which is not an "
                    "earlier call in this plan"
                )
        seen.add(call.capability_id)


def reconcile_with_store(calls: List[CapabilityCall], artifact_store) -> List[CapabilityCall]:
    """Correct the model's `matched_existing` against what is actually stored.

    The parser sets that flag by reading the catalog, which makes it a claim about
    intent; the executor branches on whether an artifact is on disk, which is a fact.
    Reconciling before the plan is printed means the preview a human confirms says
    DISCOVER exactly when a discovery run is what will happen — including the case the
    model gets wrong, where it invents a new id for something already recorded and the
    preview would otherwise under-report what the run is about to do.
    """
    for call in calls:
        call.matched_existing = artifact_store.get_latest(call.capability_id) is not None
    return calls


# ---------------------------------------------------------------------
# Execution — turns a parsed plan into discover/replay calls. Kept
# separate from parsing so the plan can be printed and confirmed
# before anything runs against a live target.
# ---------------------------------------------------------------------

def execute_plan(
    calls: List[CapabilityCall],
    target: str,
    artifact_store,
    discover_fn: Callable[..., dict],   # your existing discover(), called in-process
    replay_fn: Callable[..., dict],     # your existing replay(), called in-process
) -> List[dict]:
    """Run each call in order, stopping at the first one that does not succeed.

    Stopping is not an optimisation. The calls in a plan are ordered because they
    depend on each other; running a member lookup after the sign-on it requires has
    already failed produces a second, less informative failure that buries the first.
    Every call that did not run is reported as `skipped` so the caller can see the
    shape of the plan it got, not just the part that executed.
    """
    by_id = {call.capability_id: call for call in calls}
    results: List[dict] = []
    for index, call in enumerate(calls):
        # A call is handed its own parameters *and* those of everything it requires.
        # The plan puts the credentials on the sign-on call, because that is where the
        # operator's sentence put them — but the call that runs second re-establishes
        # that session as a prerequisite, in its own browser, and needs the same
        # username and password to do it. Inheriting along `requires` is what makes the
        # second call executable; each engine then drops whatever it does not declare.
        params: Dict[str, str] = {}
        secret_names: set[str] = set()
        for source in _with_requirements(call, by_id):
            params.update({p.name: p.value for p in source.params})
            secret_names |= {p.name for p in source.params if p.secret}

        artifact = artifact_store.get_latest(call.capability_id)

        if artifact is not None:
            result = replay_fn(
                capability_id=call.capability_id,
                target=target,
                params=params,
            )
        else:
            result = discover_fn(
                goal=call.goal,
                target=target,
                capability_id=call.capability_id,
                description=call.description,
                params=params,
                secret_params=secret_names,
                requires=call.requires,
            )

        results.append(result)
        if result.get("status") != "success":
            log.warning(
                "%s did not succeed (%s); skipping the rest of the plan",
                call.capability_id,
                result.get("status"),
            )
            results.extend(
                {"status": "skipped", "message": f"not run: {call.capability_id} did not succeed"}
                for _ in calls[index + 1:]
            )
            break
    return results


def _with_requirements(
    call: CapabilityCall, by_id: Dict[str, CapabilityCall]
) -> List[CapabilityCall]:
    """`call` preceded by every call it requires, transitively, requirements first.

    Order matters only for parameter precedence: a call's own value for a name wins
    over one inherited from something it requires. The plan is already validated to
    have no cycles and no forward references, so this terminates.
    """
    chain: List[CapabilityCall] = []
    for requirement in call.requires:
        required = by_id.get(requirement)
        if required is None:
            continue
        for member in _with_requirements(required, by_id):
            if member not in chain:
                chain.append(member)
    chain.append(call)
    return chain


def format_plan_for_confirmation(calls: List[CapabilityCall]) -> str:
    """
    Human-readable plan preview, printed before execute_plan runs.
    Secrets are shown redacted even at this stage — the confirmation
    step is meant to let a human sanity-check WHAT will run and in
    what order, not to double as a place a password gets echoed back.
    """
    lines = []
    for i, call in enumerate(calls, 1):
        action = "REPLAY (existing)" if call.matched_existing else "DISCOVER (new)"
        param_str = ", ".join(
            f"{p.name}={'[REDACTED]' if p.secret else p.value}"
            for p in call.params
        )
        req_str = f" [requires: {', '.join(call.requires)}]" if call.requires else ""
        lines.append(f"{i}. [{action}] {call.capability_id}({param_str}){req_str}")
    return "\n".join(lines)
