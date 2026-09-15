# Design writeup

An LLM works out how to reach a goal in a live UI once; the successful run is compiled into a
typed, versioned **capability artifact**; that artifact replays deterministically with no model
in the decision loop.

Fuller detail and requirements traceability: [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. Architecture

One Python process, no queues or services. The brief does not reward scaling infrastructure,
and every boundary that would matter later is already a module boundary.

```
goal ──► Discovery Engine ──► Recorder ──► Artifact Store
           (LLM in loop)                        │
                                                ▼
params ────────────────────────────────► Replay Engine ──► RunResult
                                          (no LLM at all)
```

`Surface` is the only thing that touches the application. The Policy Gate is consulted before
every action in both modes; the Escalation Service owns pause, handoff and resume.

**Three decisions carry the weight.**

*Perception is the accessibility tree, not the DOM.* Roles and accessible names are what a screen
reader consumes, exist on desktop platforms too (UIA, AX, AT-SPI), and survive markup churn that
CSS selectors do not. CSS is a *fallback* only, and a replay that falls back logs a drift
warning: a fallback that starts firing is the early signal an artifact needs recording again.

*Discovery and replay are separate engines, not one engine with a flag.* An `if use_llm:` branch
would make "no model in production" a claim; two engines make it verifiable by reading the
imports. Replay runs with no API key.

*The engine contains no knowledge of any application.* It gets a URL and a sentence; nothing under
`engine/` names a screen, route or field. The cost: the system starts cold, and the first
encounter with any refusal is a hard failure (§3).

**Accepted cost:** no horizontal scale without rework, and the bet on the accessibility tree
fails on a surface exposing no usable tree, such as a Citrix app rendered as pixels, which needs
the Surface in §4.

---

## 2. Artifact schema

An artifact is a **contract**, not a macro: a calling agent must be able to decide whether to
invoke it from the artifact alone. Pydantic model in `engine/schema/artifact.py`, one JSON file
per capability, reviewable in a pull request, which is what a capability library needs.

```jsonc
{
  "capability_id": "lookup_member_and_get_savings_balance",
  "version": "1.0.0",
  "status": "draft",                       // draft → approved gates unattended replay
  "target_app": { "app_id": "127.0.0.1_5050", "surface_type": "web" },
  "requires":   ["operator_login"],        // capabilities, not a session mechanism
  "input_params": [ { "name": "member_id", "type": "string", "secret": false } ],
  "outputs":      [ { "name": "savings_balance", "type": "currency", "source_step": "s4" } ],
  "steps": [
    { "step_id": "s3", "action": "click", "risk_class": "safe",
      "target": { "locator": {
        "primary":   { "strategy": "role+name", "value": "link:{{member_id}}" },
        "fallbacks": [ { "strategy": "css", "value": "…" } ] } } }
  ],
  "checkpoint": { "type": "element_present_with_pattern",
                  "pattern": "^[$€£]?[0-9,]+\\.[0-9]{2}$" },
  "known_outcomes": [ /* learned; see §3 */ ]
}
```

**Locators are a ranked list, and the ranking is the robustness story.** `role+name` first
because it survives; CSS last because it works when nothing else does. Adding an image match or
coordinate strategy for a desktop surface is a new enum value, not a schema change.

**Caller values are templated out of locators.** During discovery the result link's accessible
name *is* `10001`. Frozen literally, the capability resolves to one member forever and rides
positional fallbacks for everyone else, the dangerous kind of bug, because it looks like it
works. Substituting the value back to `link:{{member_id}}` makes the step mean *the member the
caller named*.

**`requires` names capabilities, not sessions.** Authentication is a recorded capability
discovered by the same loop, not an engine feature. Replay runs prerequisites first and passes
parameters through by name.

**`outputs` is the half of the contract that makes this callable.** A capability that navigates
correctly but hands nothing back is a script. The model names the field as it reads it,
`savings_balance` rather than `result`, that being the only point where the *meaning* of a table
cell is known. Enforced, not hoped for: the loop refuses a completion when the goal names a value
and no extract was recorded, and the Recorder flags any artifact that still ends up with none.

**`checkpoint` asserts a shape, not an answer.** `text_contains: "$8,714.97"` records one run's
result and fails the moment a balance changes. The checkpoint stores the element the value came
from plus a regex for its shape, a claim about *future* runs. Where only text is available,
caller parameters and data specific to one record are stripped first, so a confirmation keeps the
application's wording and drops the generated id.

**Accepted cost:** artifacts carry their own locators rather than referencing a shared element
registry. `kb_element_id` is reserved for that and is null everywhere today. Artifacts that stand
alone are reviewable in isolation, which matters more at this size; the cost is that one
element's drift is fixed per artifact instead of once.

---

## 3. Determinism & error handling

Replay reads the artifact and nothing else: same artifact plus same params, same steps, no model
consulted. Each step resolves its locator (primary, then fallbacks in order), acts, and records
which strategy resolved it. Resolution goes through the same `observe()` the agent used, so
replay waits on the accessibility tree settling rather than fixed sleeps.

**The result contract has four outcomes, and that is the central claim.** Collapsing these into a
boolean throws away what the caller needs to decide what to do next.

| Status | Meaning | Caller should |
|---|---|---|
| `success` | Checkpoint held; outputs returned. | Use the outputs. |
| `business_outcome` | The application correctly said no. | Treat as an answer: "no such member" *is* the result. |
| `recoverable` | Something transient interrupted it. | Retry, possibly after running a prerequisite again. |
| `hard_failure` | Something the artifact was never recorded to handle. | Stop; a human looks at it. |

**Known outcomes are learned, not declared in advance.** A discovery run only walks the success
path, stopping when the goal is met, so it structurally cannot observe how an application fails.
Instead: a replay meets something unfamiliar, reports a hard failure with candidate detectors
already stripped of record data, a human names it once, and the Knowledge Base remembers it for
that *application*. Capabilities recorded afterwards inherit it; existing ones are patched by
name with a version bump, because an artifact is a frozen contract and silently changing what a
reviewed capability detects would defeat the point of freezing it.

**The first "not found" being a hard failure is the design.** The system has genuinely never
seen that state, and guessing that an unfamiliar red banner means "routine" rather than "the
database is down" is exactly the judgement it should not make alone.
`evidence/submission/06`→`07` shows the same input crossing that line once a human names it.

---

## 4. Heterogeneity & many tenants

**The seam is `Surface`**: `observe()` returning role/name/id elements, plus click, type,
navigate, read. The schema references elements only through `strategy` + `value`, so it never
names Playwright, a selector language, or a browser.

- **Legacy web** is the *same* Surface. Tested deliberately: the target app is nested tables,
  rendered on the server, with no test IDs. The accessibility tree still yields `button:Search`.
- **Desktop** is a new Surface over UIA/AX, which expose the same role/name model. No change to
  the schema, Recorder, Replay Engine or loop.
- **Pixels only** (Citrix) needs a Surface whose `observe()` is OCR plus vision, emitting the
  same element shape and adding an `image_match` locator strategy, absorbed as a new value.

**Reuse across tenants.** Recording again for every tenant does not scale, so artifacts are
designed to be *specialised* rather than copied. Three properties already carry most of it:
routes are relative (`/members/search`, never a host), caller values are templated out, and
detectors are generalised before storage. "Member 10007 is restricted" is kept as "is flagged as
restricted access", which matches the next restricted member and the next tenant sharing the
vendor's string.

The extension I would build is a **base artifact plus an overlay per tenant**: one artifact keyed
to vendor product and version, plus a small document per tenant overriding only what differs.
Drift detection falls out of a signal that already exists: replay records which locator strategy
resolved each step, so a tenant whose primary locators start failing over to CSS is the alert
that their instance has diverged, before anything breaks.

**Not built, deliberately:** no tenant dimension in the schema. Building plumbing for many
tenants into a demo that has one is the premature infrastructure the brief does not reward. What
matters is that `app_id` already scopes the Knowledge Base and nothing assumes one instance per
app.

---

## 5. Escalation & handoff

**Detecting stuck**: three conditions, because they need different responses: the Loop Guard
trips on step budget, timeout, or a repeated state hash (cycle detection ignores `type` and
`extract`, which do not change role/name structure; without that a form with several fields halts
one keystroke in); a replay `hard_failure`; or a risky step needing a decision (§6).

**Taking control.** The browser runs **visible rather than headless by default**, and this is
what makes the handoff real rather than mocked: the window the agent is driving *is* the window
the human takes over. No second session to synchronise, no cookie transfer, no shared browsing
infrastructure: control changes hands in the same live session because there is only one.

The service writes an `InterventionRequest` carrying capability, goal, current step, why it
stopped, and a redacted screenshot; prints that context; and blocks. The human acts and presses
Enter. The run resumes by observing again rather than assuming, since the human may legitimately
have navigated somewhere unexpected. What they did is recorded and appears in the evidence.

**What is mocked:** the operator's *view* is a console summary and screenshot path, not a web
console; the brief puts live shared browsing out of scope. The *mechanism* is not mocked.

**Next:** a web console subscribing to the same records, and a `controlled_by` field making "who
holds the session" explicit rather than implied by who is blocked on whom.

---

## 6. Safety

**Allowlist**, configurable and enforced in **both** modes, before every navigation in discovery
*and* replay. Enforcing it on the replay path matters more than it first appears: an artifact is
*data*, it can be edited by hand or copied between environments, the URL checked at record time
is not the URL being loaded now, and replay is the mode that runs unattended.

**Risk classification is written in English, not per app.** Risky controls match by substring
against a control's accessible name: `confirm`, `submit`, `delete`, `approve`, `transfer`.
These are properties of *interface vocabulary*: a banking console, a claims system and a hospital
admin tool all label the irreversible button the same way, which makes the list portable in a way
one written for a single app is not. A short `safe_action_names` list is checked first so "Submit
search" does not pause.

A risky step during **discovery** pauses for a human. During **replay** it is permitted, because
it was reviewed when the artifact was approved: the gate is `draft → approved`, not every
execution. Blocking risky actions at replay would mean no capability that writes anything could
run unattended, which is most of the useful ones.

**Secrets never reach the model or disk.** The model sees parameter *names* and types
`{{password}}`; substitution happens at the keyboard inside the Surface call. A credential is
never in an API request, the trace, the artifact or a report.

**Limits, plainly.** The list of risky names is English only and matches on names: it misses an
irreversible control labelled with an icon, a localised string, or a euphemism ("Proceed"), and
it fires on harmless ones. Firing too often costs a pause, failing to fire costs an unintended
write, so it leans long. Artifacts recorded before a policy change keep the classification they
were recorded under until they are recorded again, which is the same frozen contract rule that
governs `known_outcomes`. `redact_screenshot` is identity, which is honest rather than hidden:
every record in the target app is fabricated. What matters is that the boundary is real: every
screenshot goes through that function, so production masking is one implementation rather than an
audit of every call site.

---

## 7. Cuts

| Cut | Why | Cost |
|---|---|---|
| Tenant dimension in the schema | Premature for a demo with one tenant | §4 is a design answer, not a running one |
| Desktop Surface | The seam is the deliverable; a second driver proves nothing new | Portability argued, not demonstrated |
| Web operator console | Brief puts live shared browsing out of scope | Handoff is driven from the terminal |
| Element registry owned by the Knowledge Base | Indirection with one consumer | Locator fixes are per artifact |
| Screenshot redaction | No real PII to mask | Seam real, implementation identity |
| Automated optimization review | A second LLM pass per run spends tokens to save tokens | Artifacts not guaranteed minimal |
| Retry/backoff on transient loads | Not reached by this app's failure modes | `recoverable` is classified, not retried automatically |

**Next, in order:** (1) automatic retry on `recoverable`, since the status is returned but the
caller still has to act on it; (2) base artifact plus an overlay per tenant, with the fallback
resolution signal wired into a real drift alert; (3) `draft → approved` gated on a stability
score from replaying N times; (4) bounded LLM recovery, checked against policy, for a single
failed step: one step, one attempt, or it escalates.

**The honest weak point.** The system is only as good as the discovery run behind an artifact, and
discovery is not deterministic. The mitigations are structural: the model can only act on
enumerated element ids, the loop refuses a completion that skips a required extract, the Recorder
flags what still looks wrong, confidence is scored from the ratio of calls to steps. But a model
that reaches the goal by a clumsy route produces a clumsy capability, and today only a human
reading the artifact catches that.
