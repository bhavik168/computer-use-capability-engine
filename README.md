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
| `data/artifacts/` | The capability catalogue — every artifact here was produced by a real discovery run. Committed, because it is what a reviewer reads to judge the schema. |
| `data/kb/`, `data/escalations/` | What the system has learned about an application, and open intervention requests. Machine-local; empty on a fresh checkout. |
| `scripts/intent_scenarios.py` | Offline checks for the intent parser: plan validation, replay-vs-discover, secret redaction. No browser, no model. |
| `scripts/replay_scenarios.py` | Every branch of the replay result contract, against a live browser. Runs hermetically. |
| `evidence/submission/` | The committed end-to-end demonstration: discovery, replay, an error state, and an escalation. |
| `evidence/discovery_runs/`, `evidence/replay_runs/` | Where your own runs land. Gitignored scratch; empty until you run something. |

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

The model provider is Gemini, and the model is named by `GEMINI_MODEL` — required, with no
default compiled in, so a retired model is refused at startup rather than mid-run. It is reached
through one adapter, `engine/discovery/gemini_client.py`, behind the `LLMClient` seam in
`engine/discovery/llm_client.py` — the discovery loop itself deals in provider-neutral
`Decision` objects and never assembles a vendor's message format, so swapping providers is
one file.

The browser runs **non-headless by default** — the visible window is what a human takes
control of during an escalation handoff. Set `ENGINE_HEADLESS=1` (or pass `--headless`) for
unattended runs.

Operator credentials default to `operator1` / `pass123`; override with `COREBANK_USERNAME`
and `COREBANK_PASSWORD`.

## Run it without a model key

The capability catalogue in `data/artifacts/` is committed, and every artifact in it was
produced by a real discovery run against the app in `target_app/`. So the whole replay half of
the system — the production path — can be exercised with no `GEMINI_API_KEY` at all:

```bash
cd target_app && ./run.sh          # terminal 1
.venv/bin/python -m engine.cli list-artifacts
.venv/bin/python -m engine.cli replay --capability lookup_member_and_get_savings_balance \
    --param member_id=10001 --param username=operator1 --param password=pass123
# → Status  : success
# → Outputs : {"savings_balance": "$8,714.97"}
```

Change `member_id` and the answer changes with it — the recorded locator is
`link:{{member_id}}`, not the member discovery happened to be pointed at.

The committed artifacts ship **cold**: `known_outcomes` is empty, because a discovery run only
ever walks the success path and so cannot observe how an application fails. Teaching it is
what the demo below shows.

## Demo path

```bash
export ENGINE_TARGET=http://127.0.0.1:5050

# 1. Discover how to sign on. The agent is given a URL and a sentence; it finds the form.
#    --secret means the value is typed but never written to the artifact, logs or reports.
.venv/bin/python -m engine.cli discover \
    --goal "Sign on to the console as the supplied operator" \
    --target $ENGINE_TARGET \
    --param username=operator1 --param password=pass123 --secret password \
    --capability-id operator_login --description "Establish an operator session."

# 2. Discover the real goal, starting from a session the recorded login establishes.
.venv/bin/python -m engine.cli discover \
    --goal "Look up member 10001 and read their savings balance" \
    --target $ENGINE_TARGET --requires operator_login \
    --param username=operator1 --param password=pass123 --secret password \
    --capability-id lookup_member_and_get_savings_balance \
    --description "Look up a member by ID and read their current savings balance."

# 3. Replay it — deterministic, no LLM anywhere in this path.
.venv/bin/python -m engine.cli replay --capability lookup_member_and_get_savings_balance \
    --param member_id=10001 --param username=operator1 --param password=pass123

# 4. A member that does not exist. On a cold system this is a HARD FAILURE — nothing has
#    told the engine what "not found" means here — and the report suggests detectors.
.venv/bin/python -m engine.cli replay --capability lookup_member_and_get_savings_balance \
    --param member_id=99999 --param username=operator1 --param password=pass123

# 5. Name it once. The Knowledge Base keeps it for this application from now on.
.venv/bin/python -m engine.cli learn-outcome --app-id 127.0.0.1_5050 \
    --code member_not_found --type business_outcome \
    --text-contains "No matching member found" \
    --capability lookup_member_and_get_savings_balance

# 6. Re-run step 4. Same input, same artifact — now a clean business outcome.

.venv/bin/python -m engine.cli list-artifacts
```

Step 4 reporting a hard failure is the design working, not a bug. The system has genuinely
never seen that state, and guessing that an unfamiliar red banner means "routine" rather
than "the database is down" is exactly the judgement it should not make on its own.

Every run — success, business outcome, escalation or hard failure — writes a report with
per-step screenshots under `evidence/discovery_runs/<run_id>/` or
`evidence/replay_runs/<run_id>/`.

## One sentence instead of the flags

`run` is steps 1–3 without hand-assembling any of it. One model call turns the sentence into
an ordered plan — which capabilities, in what order, replaying what is already recorded and
discovering only what is not — and prints it for confirmation before a browser opens. Nothing
about *how* to drive the application is decided here; a recorded capability still replays with
no LLM in the loop.

```bash
.venv/bin/python -m engine.cli run --target $ENGINE_TARGET \
    --prompt "Sign on to the console as operator1/pass123, then look up member 10001 \
              and get their savings balance"

Plan (2 calls) against http://127.0.0.1:5050:
1. [REPLAY (existing)] operator_login(username=operator1, password=[REDACTED])
2. [REPLAY (existing)] lookup_member_and_get_savings_balance(member_id=10001) [requires: operator_login]

Proceed? [y/N]:
```

A value the prompt supplies for a credential is marked secret by the parser and stays that way
through the run: redacted in the plan preview, and never written to the artifact, the logs or
the reports. `--dry-run` prints the plan and stops without opening a browser; `--yes` skips the
confirmation, for scripted and CI use.

## Outcomes

Replay reports one of four statuses, and the distinction is the point:

| Status | Meaning | Example |
|---|---|---|
| `success` | Every step ran and the checkpoint held. | Balance read for member `10001`. |
| `business_outcome` | The application correctly said no. | `member_not_found` for `99999`; `permission_denied` for restricted member `10005`. |
| `recoverable` | Something interrupted the run that a retry could survive. | The operator session expired mid-flow. |
| `hard_failure` | Something the artifact was never recorded to handle. | The first `99999` lookup, before anyone has named "not found". |

## Development utilities

```bash
.venv/bin/python -m scripts.probe_models flash   # which models this key can use now
.venv/bin/python -m engine.schema.validate data/artifacts/lookup_member_and_get_savings_balance.json
ENGINE_HEADLESS=1 .venv/bin/python -m engine.surface._manual_check    # observe the live app
ENGINE_HEADLESS=1 .venv/bin/python -m scripts.replay_scenarios        # every replay outcome class
.venv/bin/python -m scripts.intent_scenarios           # plan parsing, offline (--live to parse for real)
```

`replay_scenarios` runs hermetically: it copies the artifact catalogue into a temporary store
and uses a temporary Knowledge Base, because two of its scenarios *teach* the system an
outcome and one bumps an artifact's version. A dev sweep must not rewrite the committed
catalogue as a side effect of being run, and it must start from the same cold KB every time —
otherwise the "first encounter is a hard failure" scenarios would pass only once.
