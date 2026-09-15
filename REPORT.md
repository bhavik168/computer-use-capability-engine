# Design write-up

A system that gives an AI agent hands inside applications that have no API. An LLM works out
how to reach a goal in a live UI once; the successful run is compiled into a typed, versioned
**capability artifact**; that artifact then replays deterministically with no model in the
decision loop.

Deeper detail, diagrams and requirements traceability are in [`ARCHITECTURE.md`](ARCHITECTURE.md).
This document is the argument, not the manual.

---

## 1. Architecture

One Python process, no queues and no services. The brief explicitly does not reward scaling
infrastructure, and every boundary that would matter later is a module boundary today: the
seams are real even though the deployment is not.

```
goal ──► Discovery Engine ──► Recorder ──► Artifact Store
           (LLM in loop)                        │
                                                ▼
params ────────────────────────────────► Replay Engine ──► RunResult
                                          (no LLM at all)
```

Six components, each owning exactly one external dependency:

| Component | Responsibility |
|---|---|
| **Surface** | The only thing that touches the application. `observe()` → elements, plus `click`/`type`/`navigate`/`read_text`. |
| **Discovery Engine** | The observe → decide → act loop. The only place a model makes a decision. |
| **Recorder** | Compiles a successful trace into an artifact. Deliberately thin. |
| **Replay Engine** | Executes an artifact against params. Never imports an LLM client. |
| **Policy Gate** | Allowlist and risk classification, consulted before every action in both modes. |
| **Escalation Service** | Owns pause, human handoff, and resume of the live session. |

Three decisions carry most of the weight.

**The agent perceives through the accessibility tree, not the DOM.** The brief asks for an
approach that still works when there is no clean DOM. Roles and accessible names are what a
screen reader consumes, they exist on desktop platforms too (UIA, AX, AT-SPI), and they
survive the markup churn that CSS selectors do not. A CSS path is recorded as a *fallback*
only, and when replay falls back it logs a drift warning — a fallback that starts firing is
the early signal that an artifact needs re-recording.

**Discovery and replay are separate engines, not one engine with a flag.** The whole value
proposition is that production execution has no model in it. A shared code path with an
`if use_llm:` branch would make that a claim; two engines make it a property you can verify
by reading the imports. `replay` runs with `GEMINI_API_KEY` unset, which is the cheapest
possible proof.

**The engine contains no knowledge of any application.** It is given a URL and a sentence.
Where the login form is, what "no such member" looks like, which screens exist — all of it is
discovered or learned. Nothing under `engine/` names a screen, a route or a field, so pointing
it at a different application changes no engine code. The cost is that the system starts cold
and the first encounter with any refusal is a hard failure (§3).

**Trade-off accepted:** single process means no horizontal scale without rework, and the
accessibility-tree bet fails on a surface that exposes no usable tree — a Citrix-published app
rendered as pixels, say, which would need the screenshot-plus-coordinates Surface described
in §4.

---

## 2. Artifact schema

An artifact is a **contract**, not a macro. A calling agent has to be able to decide whether
to invoke it from the artifact alone, so the schema leads with the interface and treats the
steps as the implementation. It is a Pydantic model in `engine/schema/artifact.py`, serialised
one JSON file per capability — reviewable in a pull request, which is what a capability
library actually needs.

```jsonc
{
  "capability_id": "lookup_member_and_get_savings_balance",
  "version": "1.0.0",
  "status": "draft",                       // draft → approved gates unattended replay
  "description": "Look up a member by ID and return their current savings balance.",
  "target_app":  { "app_id": "127.0.0.1_5050", "surface_type": "web" },
  "requires":    ["operator_login"],       // capabilities, not a session mechanism
  "risk_class":  "safe",
  "provenance":  { "created_from_run": "discovery_…", "confidence": 1.0 },

  "input_params": [
    { "name": "member_id", "type": "string", "required": true, "secret": false }
  ],
  "outputs": [
    { "name": "savings_balance", "type": "currency", "source_step": "s4" }
  ],
  "steps": [
    { "step_id": "s3", "action": "click", "risk_class": "safe",
      "target": { "locator": {
        "primary":   { "strategy": "role+name", "value": "link:{{member_id}}" },
        "fallbacks": [ { "strategy": "css", "value": "…" } ] } } }
  ],
  "checkpoint": { "type": "element_present_with_pattern",
                  "pattern": "^[$€£]?[0-9,]+\\.[0-9]{2}$" },
  "known_outcomes": [ /* learned; see §3 */ ],
  "review": { "status": "not_reviewed", "optimization_notes": null }
}
```

Why it is shaped this way:

**Locators are a ranked list, and the ranking is the robustness story.** `role+name` first
because it is what survives; CSS last because it is what works when nothing else does. One
`strategy` field with an ordered fallback array means adding an image-match or
coordinate strategy for a desktop surface is a new enum value, not a schema change.

**Caller values are templated back out of locators.** During discovery the result link's
accessible name *is* `10001`. Frozen literally, the capability would silently resolve to one
member forever and ride positional fallbacks for everyone else — the most dangerous kind of
bug, because it looks like it works. The Recorder substitutes known parameter values back
into placeholders, so the step means *the member the caller named*, and a replay with a
different `member_id` genuinely goes somewhere different.

**`requires` names capabilities, not sessions.** Authentication is not an engine feature; it
is a recorded capability like any other, and `operator_login` is discovered by the same loop
against the same UI. A capability declares what must have happened first, and replay runs those
prerequisites and passes parameters through by name.

**`outputs` is the half of the contract that makes this callable.** A capability that
navigates correctly but hands nothing back is a script, not a capability. The extract step's
element is the source, the type is inferred, and the model names the field as it reads it —
`savings_balance`, not `result` — because that naming is the only point in the system where
the *meaning* of a table cell is known. This is enforced rather than hoped for: the discovery
loop refuses a completion when the goal names a value and no extract was recorded, and the
Recorder flags any artifact that still ends up with none.

**`checkpoint` asserts a shape, not an answer.** `text_contains: "$8,714.97"` is a recording
of one run's result and fails the moment a balance changes. The checkpoint records the
element the value came from plus a regex for its shape, which is a claim about *future* runs.
Where only text is available, the caller's parameters and record-specific data are stripped
first, so a confirmation checkpoint keeps the application's wording and drops the generated id.

**Trade-off accepted:** artifacts carry their own locators rather than referencing a shared
element registry in the Knowledge Base. `kb_element_id` is reserved for that and is null
everywhere today. Self-contained artifacts are reviewable in isolation, which matters more at
this size; the cost is that one element's drift is fixed per artifact instead of once.

---

## 3. Determinism & error handling

**Determinism.** Replay reads the artifact and nothing else: same artifact plus same params
means the same steps in the same order, with no model consulted. Each step resolves its
locator against the live page (primary, then each fallback in order), acts, and records which
strategy resolved it. Resolution happens through the same `observe()` the agent used, so
replay waits on the accessibility tree settling rather than on fixed sleeps. The run ends by
evaluating the checkpoint and returning declared outputs.

**The result contract is four-way, and that is the central design claim.** Collapsing these
into a boolean throws away exactly what the calling agent needs to decide what to do next.

| Status | Meaning | Caller should |
|---|---|---|
| `success` | Checkpoint held; outputs returned. | Use the outputs. |
| `business_outcome` | The application correctly said no. | Treat as an answer — "no such member" *is* the result. |
| `recoverable` | Something transient interrupted it. | Retry, possibly after re-running a prerequisite. |
| `hard_failure` | Something the artifact was never recorded to handle. | Stop; a human looks at it. |

**Known outcomes are learned, not pre-declared.** A discovery run only ever walks the success
path — it stops when the goal is met — so it structurally cannot observe how an application
fails. Those states are learned instead: a replay meets something unfamiliar, reports a hard
failure with candidate detectors already stripped of record data, a human names it once
(`that's permission_denied, it's a business outcome`), and the Knowledge Base remembers it for
that *application*. Every capability recorded afterwards inherits it; existing ones are patched
by name, with a version bump, because an artifact is a frozen contract and silently changing
what a reviewed capability detects would defeat the point of freezing it.

**The first "not found" being a hard failure is the design, not a gap.** The system has
genuinely never seen that state, and guessing that an unfamiliar red banner means "routine"
rather than "the database is down" is precisely the judgement it should not make alone.
Pre-loading the answer would make the first run look better and teach the system nothing.

**Drift, secondarily.** The UI is stable, so drift is the lesser problem — but a locator that
resolves via a fallback logs a warning, and one that resolves not at all fails the step with
what was expected, what was observed, and a screenshot.

---

## 4. Heterogeneity & multi-tenant

**The seam is `Surface`.** Perception and action live behind one interface — `observe()`
returning role/name/id elements, plus click, type, navigate, read. The artifact schema
references elements only through `strategy` + `value`, so it never names Playwright, a
selector language, or a browser.

- **Legacy web** (framesets, table soup, no test IDs) is the *same* Surface. This was tested
  by building the target app that way on purpose: nested tables, no test IDs, server-rendered.
  The accessibility tree still yields `button:Search` where CSS yields a positional path.
- **Desktop** is a new Surface implementation over UIA/AX, which expose the same role/name
  model. No change to the schema, Recorder, Replay Engine or agent loop.
- **A pixels-only surface** (Citrix, a remote desktop) needs a Surface whose `observe()` is
  OCR plus vision, emitting the same element shape and adding an `image_match` or
  `coordinates` locator strategy — the ordered-fallback design absorbs that as a new value.

**Multi-tenant reuse.** Hundreds of tenants run the same vendor product, branded and
configured differently. Re-recording per tenant does not scale, so the artifact is designed to
be *specialised* rather than copied. Three properties already carry most of that:

1. **Routes are relative.** `/members/search`, never `http://host:5050/…`, so the same
   artifact applies to any deployment of the app.
2. **Caller values are templated out**, so nothing is pinned to one record.
3. **Detectors are generalised before they are stored.** "Member 10007 is restricted" is kept
   as "is flagged as restricted access" — which matches the *next* restricted member, and the
   next tenant's wording if they share the vendor's string.

The extension I would build is a **base artifact plus per-tenant overlay**: one artifact keyed
to a vendor product and version, and a small per-tenant document overriding only what differs
(a renamed label, an extra interstitial, a changed route). Drift detection falls out of the
signal that already exists — replay records which locator strategy resolved each step, so a
tenant whose primary locators start failing over to CSS is the alert that their instance has
diverged, before anything breaks outright.

**Not built, deliberately:** no tenant dimension exists in the schema today. Building
multi-tenant plumbing for a single-tenant demo is the premature infrastructure the brief
explicitly does not reward. What matters is that `app_id` already scopes the Knowledge Base,
and nothing in the schema assumes one instance per application.

---

## 5. Escalation & handoff

**Detecting stuck.** Three distinct conditions, because they need different responses:

- **Discovery is stuck** — the Loop Guard trips on a step budget, a wall-clock timeout, or a
  repeated state hash (the same screen seen twice with a mutating action in between). Cycle
  detection deliberately ignores `type` and `extract`, which do not change the page's
  role/name structure; without that exemption a multi-field form halts one keystroke in.
- **Replay hit something unrecorded** — a `hard_failure`, which is the escalation trigger.
- **A risky step needs a decision** — the Policy Gate pauses before acting (§6).

**Taking control.** The browser runs **non-headless by default**, and this is the design
point that makes the handoff real rather than mocked: the window the agent is driving *is* the
window the human takes over. There is no second session to synchronise, no cookie transfer, no
co-browsing infrastructure. Control changes hands in the same live session because there is
only one.

The service writes an `InterventionRequest` carrying the capability, the goal, the current
step, why it stopped, and a redacted screenshot; prints that context; and blocks. The human
acts in the live window and presses Enter. The run resumes from whatever state they left
behind — re-observing rather than assuming, so the human may legitimately have navigated
somewhere the automation did not expect. What they did is recorded on the request and appears
in the run's evidence.

**What is mocked, and why that is the right cut.** The operator's *view* is a console summary
and a screenshot path rather than a web console — the brief puts a real-time co-browsing
console out of scope. The *mechanism* is not mocked: the run genuinely stops, the human
genuinely acts in the live session, and the run genuinely continues.

**Next:** a web operator console subscribing to the same `InterventionRequest` records, and a
`controlled_by` field making "who holds the session" explicit rather than implied by who is
blocked on whom — which is what multi-operator routing would need.

---

## 6. Safety

**Allowlist.** Configurable, and enforced in **both** modes — checked before every
navigation in discovery *and* in replay. The target's own host is always allowed and nothing
else is unless named, which is the right default for a system whose entire job is to stay
inside one application.

Enforcing it on the replay path matters more than it first appears. An artifact is *data*: it
can be hand-edited, copied between environments, or pulled from another tenant's catalogue,
and the URL that was checked when it was recorded is not the URL being loaded now. Replay is
also the mode that runs unattended. An artifact whose opening step is rewritten to an
off-allowlist host is refused at that step rather than followed.

**Risk classification, and why it is written in English rather than per-app.** Risky controls
are matched by substring against a control's accessible name — `confirm`, `submit`, `delete`,
`approve`, `transfer`, `authorize`… These are properties of *interface vocabulary*, not of any
one application: a banking console, a claims system and a hospital admin tool all label the
irreversible button the same way. That makes the list portable in a way an app-specific one is
not. A short `safe_action_names` list is checked first so "Submit search" does not pause.

A risky step during **discovery** pauses for a human. A risky step during **replay** is
permitted, because it was already reviewed when the artifact was approved — the gate is the
`draft → approved` transition, not every execution. Blocking risky actions at replay time
would mean no capability that writes anything could ever run unattended, which is most of the
useful ones.

**Secrets never reach the model or the disk.** The model is shown parameter *names* and types
`{{password}}`; substitution happens at the keyboard, inside the Surface call. So a credential
is never in an API request, never in the trace, never in the artifact, and never in a report.
Values marked `--secret` carry no `example` in the schema by rule.

**Limits, stated plainly.** The risky-name list is English-only and name-based: it misses an
irreversible control labelled with an icon, a localised string, or a euphemism ("Proceed",
"Finish"), and over-triggers on harmless ones. Over-triggering costs a pause; under-triggering
costs an unintended write — so the list leans long. `redact_screenshot` is currently identity,
which is honest rather than hidden: every record in the target app is fabricated, so there is
nothing to mask. What matters is that the boundary is real — every screenshot is written
through that function, so production masking is one implementation, not an audit of every call
site.

---

## 7. Cuts

**Deliberately not built:**

| Cut | Why | Cost |
|---|---|---|
| Multi-tenant dimension in the schema | Premature infrastructure for a single-tenant demo | §4 is a design answer, not a running one |
| Desktop Surface | The seam is the deliverable; a second driver proves nothing new | Accessibility-tree portability argued, not demonstrated |
| Web operator console | Brief puts real-time co-browsing out of scope | Handoff is terminal-driven |
| KB-owned element registry | Indirection with one consumer | Locator fixes are per-artifact |
| Screenshot redaction | No real PII exists to mask | Seam is real, implementation is identity |
| Automated optimization review | A second LLM pass on every run spends tokens to save tokens | Artifacts are not guaranteed minimal |
| Retry/backoff on transient loads | Not reached by the failure modes the target app produces | `recoverable` is classified but not auto-retried |

**What I would build next, in order:**

1. **Auto-retry on `recoverable`.** The status is classified and returned but the caller still
   has to act on it. Re-running the failed prerequisite and retrying once is a small,
   well-bounded change with immediate value.
2. **Base artifact plus per-tenant overlay** (§4), with the fallback-resolution signal wired
   into a real drift alert.
3. **`draft → approved` with a stability score.** The gate exists in the schema; the evidence
   to drive it — replay N times, report flakiness — does not.
4. **Bounded LLM recovery for a single failed step**, policy-checked and recorded as evidence.
   Explicitly never open-ended: one step, one attempt, or it escalates.

**The honest weak point.** The system is only as good as the discovery run behind an artifact,
and discovery is non-deterministic. The mitigations are structural rather than hopeful — the
model can only act on enumerated element ids, the loop refuses a completion that skips a
required extract, the Recorder flags what still looks wrong, and confidence is scored from the
call-to-step ratio. But a model that reaches the goal by a clumsy route produces a clumsy
capability, and today only a human reading the artifact catches that.
