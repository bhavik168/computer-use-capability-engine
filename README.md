# Computer-Use Capability Engine

An automation system that lets an AI agent operate back-office applications that expose no
API — the common case at banks and credit unions, where the only reliable interface is the
one a human operator sees and uses.

It runs in two modes. **Discovery** is an LLM-driven observe → decide → act loop that
explores a live UI until a goal is reached, and compiles the successful run into a typed,
versioned *capability artifact*. **Replay** executes that artifact deterministically, with
no LLM anywhere in the decision loop. The model discovers once; the artifact it produces
becomes a reusable capability, and deterministic replay is how that capability is invoked
thereafter — cheaply, repeatably, and without re-reasoning about the UI on every call.

Design rationale, trade-offs and requirements traceability: [`ARCHITECTURE.md`](ARCHITECTURE.md)
and [`REPORT.md`](REPORT.md).

## What the engine is told, and what it works out

It is told two things: a **URL** and a **goal in plain language**. Nothing else. There is no
profile, no site map, no list of screens, no description of the login form, no catalogue of
what "not found" looks like on this particular system. If a goal needs a session, the agent
finds the sign-on controls in the accessibility tree and operates them like any other
control — authentication is a capability that gets discovered and recorded, not a feature of
the engine.

Everything the system comes to know about an application it learned from a run:

| Knowledge | Where it comes from |
|---|---|
| Which screens exist, what is on them | Recorded into the KB on every `observe()` during discovery. |
| How to reach a goal | Discovered once by the LLM, frozen into an artifact. |
| How to identify a control | Built at act time from the accessibility tree, with a DOM path as fallback. |
| What "no such record" looks like | **Learned.** The first time a replay meets it, it is a hard failure; a human names it; the KB remembers it for that application. |
| What must not be clicked without a human | Operator policy — generic English verbs (`confirm`, `delete`, `approve`…), not app-specific strings. |

Point it at a different application, or a different domain entirely, and none of that changes.

## Layout

| Path | What it is |
|---|---|
| `engine/` | The capability engine. **Contains no knowledge of any application.** |
| `engine/policy/defaults.yaml` | Operator safety policy. Application-agnostic; override with `--policy`. |
| `target_app/` | CoreBank Servicing Console — a legacy-styled Flask app used as *a* target. Swappable. |
| `fixtures/` | Hand-written artifacts for one target, used to build and prove Replay before Discovery existed. Not discovery output. |
| `data/` | What the system has learned: `artifacts/` (capabilities), `kb/` (screens, elements, learned outcomes), `escalations/`. Empty on a fresh checkout. |
| `evidence/` | Per-run reports with per-step screenshots, for every outcome. Empty until you run something. |

## Setup

**1. Stand up the target app** (its own instructions are in
[`target_app/README.md`](target_app/README.md)):

```bash
cd target_app && ./run.sh          # serves CoreBank on http://127.0.0.1:5050
```

**2. Install the engine**, from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium      # one-time browser download

cp .env.example .env                       # then paste your key into .env
```

`.env` is gitignored; `.env.example` is the committed template. Anything in it can equally be
exported as a shell variable, and an exported value wins over the file — a value you set
deliberately should not be overridden by a stale file on disk. Loading is done by
`engine/config.py` (about 20 lines, no extra dependency) and happens once, in the CLI, before
any command reads configuration.

Only `GEMINI_API_KEY` is required, and only for `discover`. **`replay` needs no key at all** —
it never calls a model, which is the whole point of the record-once/replay-many split, and
the easiest way to see that claim is true is that replay keeps working with the key removed.

The model provider is Gemini (`gemini-2.5-pro`; override with `GEMINI_MODEL`). It is reached
through one adapter, `engine/discovery/gemini_client.py`, behind the `LLMClient` seam in
`engine/discovery/llm_client.py` — the discovery loop itself deals in provider-neutral
`Decision` objects and never assembles a vendor's message format, so swapping providers is
one file.

The browser runs **non-headless by default** — the visible window is what a human takes
control of during an escalation handoff. Set `ENGINE_HEADLESS=1` (or pass `--headless`) for
unattended runs.

Operator credentials default to `operator1` / `pass1234`; override with `COREBANK_USERNAME`
and `COREBANK_PASSWORD`.

## Demo path

```bash
export ENGINE_TARGET=http://127.0.0.1:5050

# 1. Discover how to sign on. The agent is given a URL and a sentence; it finds the form.
#    --secret means the value is typed but never written to the artifact, logs or reports.
.venv/bin/python -m engine.cli discover \
    --goal "Sign on to the console as the supplied operator" \
    --target $ENGINE_TARGET \
    --param username=operator1 --param password=pass1234 --secret password \
    --capability-id operator_login --description "Establish an operator session."

# 2. Discover the real goal, starting from a session the recorded login establishes.
.venv/bin/python -m engine.cli discover \
    --goal "Look up member 10001 and read their savings balance" \
    --target $ENGINE_TARGET --requires operator_login \
    --param username=operator1 --param password=pass1234 --secret password \
    --capability-id check_savings_balance \
    --description "Look up a member by ID and read their current savings balance."

# 3. Replay it — deterministic, no LLM anywhere in this path.
.venv/bin/python -m engine.cli replay --capability check_savings_balance \
    --param member_id=10001 --param username=operator1 --param password=pass1234

# 4. A member that does not exist. On a cold system this is a HARD FAILURE — nothing has
#    told the engine what "not found" means here — and the report suggests detectors.
.venv/bin/python -m engine.cli replay --capability check_savings_balance \
    --param member_id=99999 --param username=operator1 --param password=pass1234

# 5. Name it once. The Knowledge Base keeps it for this application from now on.
.venv/bin/python -m engine.cli learn-outcome --app-id 127.0.0.1_5050 \
    --code member_not_found --type business_outcome \
    --text-contains "No member found with that ID" \
    --capability check_savings_balance

# 6. Re-run step 4. Same input, same artifact — now a clean business outcome.

.venv/bin/python -m engine.cli list-artifacts
```

Step 4 reporting a hard failure is the design working, not a bug. The system has genuinely
never seen that state, and guessing that an unfamiliar red banner means "routine" rather
than "the database is down" is exactly the judgement it should not make on its own.

Every run — success, business outcome, escalation or hard failure — writes a report with
per-step screenshots under `evidence/discovery_runs/<run_id>/` or
`evidence/replay_runs/<run_id>/`.

## Outcomes

Replay reports one of four statuses, and the distinction is the point:

| Status | Meaning | Example |
|---|---|---|
| `success` | Every step ran and the checkpoint held. | Balance read for member `10001`. |
| `business_outcome` | The application correctly said no. | `member_not_found` for `99999`; `permission_denied` for `10007`. |
| `recoverable` | Something interrupted the run that a retry could survive. | `session_expired` mid-transfer for `10003`. |
| `hard_failure` | Something the artifact was never recorded to handle. | A transfer over 1,000,000 crashes the app; no detector matches. |

## Development utilities

```bash
.venv/bin/python -m engine.schema.validate fixtures/corebank/check_savings_balance.json
ENGINE_HEADLESS=1 .venv/bin/python -m engine.surface._manual_check    # observe the live app
ENGINE_HEADLESS=1 .venv/bin/python -m scripts.replay_scenarios        # the six replay outcomes
```

The two JSON files in `fixtures/corebank/` are **hand-crafted fixtures** used to build
and prove Replay before Discovery existed — not discovery output. Genuine discovery output
lands in `data/artifacts/`; see `fixtures/corebank/README.md`.
