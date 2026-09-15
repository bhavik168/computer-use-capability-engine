# Computer Use Capability Engine

An AI agent needs to use a business application that has no API. The only way in is the screens
a human operator uses.

This system solves that once per task, then stops paying for it. **Discovery** gives an LLM a
URL and a goal in plain language; it observes the live screen, takes one action, looks again,
and keeps going until the goal is met. The successful run is compiled into a **capability
artifact**, a typed and versioned JSON contract. **Replay** then executes that artifact with no
model anywhere in the decision path, for the cost of a page load, the same way every time.

The engine knows nothing about any particular application. It gets a URL and a sentence.

* How it is built: [`ARCHITECTURE.md`](ARCHITECTURE.md)
* Why it is built that way: [`REPORT.md`](REPORT.md)
* Committed proof that it runs: [`evidence/submission/`](evidence/submission)

---

## Running it: the exact steps

You need Python 3.11 or newer and two terminals. A model key is needed only for step 5.

### Step 1. Start the sample application (terminal 1)

```bash
cd target_app
./run.sh
```

It creates its own virtual environment, installs Flask, and serves the Member Servicing
Console on <http://127.0.0.1:5050>. Leave it running. Sign on by hand if you want a look
around: `operator1` / `pass123`.

### Step 2. Install the engine (terminal 2, at the repository root)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

The last line downloads the browser Playwright drives. It is a one time download.

### Step 3. Create your configuration file

```bash
cp .env.example .env
```

`.env` is gitignored. Anything in it can also be exported as a normal shell variable, and an
exported value wins over the file.

| Setting | Needed for | Default |
|---|---|---|
| `GEMINI_API_KEY` | `discover` and `run` only | empty, get one at <https://aistudio.google.com/apikey> |
| `GEMINI_MODEL` | `discover` and `run` only | `gemini-3.5-flash-lite` |
| `ENGINE_TARGET` | `replay` and `run`, unless you pass `--target` | `http://127.0.0.1:5050` |
| `ENGINE_HEADLESS` | set to `1` to hide the browser window | unset, so the window is visible |
| `COREBANK_USERNAME`, `COREBANK_PASSWORD` | the sample application's operator | `operator1` / `pass123` |

Leave `ENGINE_HEADLESS` unset for a first run. The visible window is the point: it is what a
human takes control of during an escalation.

### Step 4. Replay a capability, with no model key at all

Five artifacts are committed under `data/artifacts/`, and every one of them was produced by a
real discovery run. So the production half of the system works immediately:

```bash
.venv/bin/python -m engine.cli list-artifacts

.venv/bin/python -m engine.cli replay \
    --capability lookup_member_and_get_savings_balance \
    --target http://127.0.0.1:5050 \
    --param member_id=10001 --param username=operator1 --param password=pass123
```

A browser window opens, signs on, searches, opens the member and reads the balance:

```
Status  : success
Outputs : {"savings_balance": "$8,714.97"}
Report  : evidence/replay_runs/replay_lookup_member_and_get_savings_balance_.../report.md
```

Change `member_id` to `10006` and the answer changes with it, because the recorded locator is
`link:{{member_id}}` rather than the member discovery happened to be pointed at. Try it:

```bash
.venv/bin/python -m engine.cli replay \
    --capability lookup_member_and_get_savings_balance \
    --target http://127.0.0.1:5050 \
    --param member_id=10006 --param username=operator1 --param password=pass123
```

That the key is absent and this still works is the claim the whole design rests on.

### Step 5. Discover a new capability (needs a model key)

Put a working key in `.env`, then let the agent work out a goal it has never seen. It is told
a URL and a sentence, nothing else:

```bash
export ENGINE_TARGET=http://127.0.0.1:5050

# 5a. Discover how to sign on. Nobody told it there is a login form.
#     --secret means the value is typed but never written to the artifact, logs or reports.
.venv/bin/python -m engine.cli discover \
    --goal "Sign on to the console as the supplied operator" \
    --target $ENGINE_TARGET \
    --param username=operator1 --param password=pass123 --secret password \
    --capability-id operator_login \
    --description "Establish an operator session."

# 5b. Discover the real goal, reusing the login capability as a prerequisite.
.venv/bin/python -m engine.cli discover \
    --goal "Look up member 10001 and read their savings balance" \
    --target $ENGINE_TARGET --requires operator_login \
    --param username=operator1 --param password=pass123 --secret password \
    --capability-id lookup_member_and_get_savings_balance \
    --description "Look up a member by ID and read their current savings balance."
```

Each run prints the status, the number of steps and model calls, the artifact it saved and the
report it wrote. Watch the browser window while it goes: that is the loop observing, deciding
and acting.

Useful options: `--max-steps` (default 25), `--timeout` in seconds (default 180),
`--no-escalation` to halt on a risky control instead of pausing for a human.

### Step 6. Teach the system what a refusal looks like

The committed artifacts ship **cold**: `known_outcomes` is empty, because a discovery run only
ever walks the success path and so never sees the application refuse. Watch what happens with
a member that does not exist:

```bash
.venv/bin/python -m engine.cli replay \
    --capability lookup_member_and_get_savings_balance \
    --target http://127.0.0.1:5050 \
    --param member_id=99999 --param username=operator1 --param password=pass123
```

This reports `hard_failure`, and that is the design working rather than a bug. The system has
genuinely never seen that screen, and guessing that an unfamiliar banner means "routine"
instead of "the database is down" is exactly the judgement it should not make alone. The report
suggests detectors you could name it with.

Name it once, and the Knowledge Base keeps it for this application from now on:

```bash
.venv/bin/python -m engine.cli learn-outcome \
    --app-id 127.0.0.1_5050 \
    --code member_not_found --type business_outcome \
    --text-contains "No matching member found" \
    --capability lookup_member_and_get_savings_balance
```

Run the same replay again. Same input, same artifact, now a clean `business_outcome`, and the
capability has been bumped to `v1.0.1`. That is the whole learning loop.

### Step 7. Drive it with one sentence instead of flags

`run` turns a sentence into an ordered plan with a single model call, replaying what is already
recorded and discovering only what is not. It prints the plan and waits for confirmation before
a browser opens.

```bash
.venv/bin/python -m engine.cli run --target $ENGINE_TARGET \
    --prompt "Sign on to the console as operator1/pass123, then look up member 10001 \
              and get their savings balance"
```

```
Plan (2 calls) against http://127.0.0.1:5050:
1. [REPLAY (existing)] operator_login(username=operator1, password=[REDACTED])
2. [REPLAY (existing)] lookup_member_and_get_savings_balance(member_id=10001) [requires: operator_login]

Proceed? [y/N]:
```

A credential in the prompt is marked secret by the parser and stays redacted through the run.
Add `--dry-run` to print the plan and stop without opening a browser, or `--yes` to skip the
confirmation in scripts and CI.

---

## Command reference

| Command | What it does |
|---|---|
| `discover` | Run the agent loop against a live UI and record a capability. Needs a model key. |
| `replay` | Execute a recorded capability deterministically. Needs no model key. |
| `run` | Turn one sentence into a plan, then replay or discover each step of it. |
| `learn-outcome` | Name a state the system met but did not recognise, for this application. |
| `list-artifacts` | Show every capability in the store, with version, status and outcome count. |
| `list-escalations` | Show intervention requests, including any waiting for a human. |

Global options go before the command: `--verbose` for debug logging, `--headless` to hide the
browser, `--policy PATH` to supply your own safety policy.

Run `.venv/bin/python -m engine.cli COMMAND --help` for the full list of options.

## What the four replay statuses mean

| Status | Meaning | Example |
|---|---|---|
| `success` | Every step ran and the checkpoint held. | Balance read for member `10001`. |
| `business_outcome` | The application correctly said no. | `member_not_found` for `99999`; `permission_denied` for restricted member `10005`. |
| `recoverable` | Something transient interrupted the run. | The operator session expired part way through. |
| `hard_failure` | A state the artifact was never recorded to handle. | The first `99999` lookup, before anyone has named it. |

## Where the output goes

| Path | What lands there |
|---|---|
| `data/artifacts/` | One JSON capability per file. Committed, because it is what a reviewer reads. |
| `data/kb/` | What the system has learned about each application. Empty on a fresh checkout. |
| `data/escalations/` | Open intervention requests. |
| `evidence/discovery_runs/`, `evidence/replay_runs/` | Your own runs: a report plus a screenshot per step. Gitignored. |
| `evidence/submission/` | The committed demonstration: discovery, replay, all four statuses, an escalation. |

## When something goes wrong

| Symptom | Fix |
|---|---|
| `Connection refused` on 127.0.0.1:5050 | The sample application is not running. Go back to step 1. |
| `Executable doesn't exist` from Playwright | You skipped `.venv/bin/playwright install chromium` in step 2. |
| `GEMINI_MODEL is required` | Set it in `.env`. There is no default in the code on purpose, so a retired model is refused at startup rather than part way through a run. |
| `Invalid username or password`, or any setting that seems ignored | An exported shell variable wins over `.env`. Check with `env \| grep COREBANK` and unset the stale one. |
| A 404 or 429 from the model | Check the name and your tier with `.venv/bin/python -m scripts.probe_models flash`. |
| Port 5050 already in use | `run.sh` frees it for you; to use another port, set `PORT` in `target_app/.env` and pass the new URL as `--target`. |
| The run pauses and asks you to press Enter | That is an escalation. The visible window is yours: act in it, then press Enter and the run resumes from what you left behind. |

## Checks you can run

```bash
# Every branch of the replay result contract, against a live browser.
# Runs hermetically: a temporary artifact store and a temporary Knowledge Base.
ENGINE_HEADLESS=1 .venv/bin/python -m scripts.replay_scenarios

# Intent parsing, offline. No browser, no model. Add --live to parse for real.
.venv/bin/python -m scripts.intent_scenarios

# The sample application's own acceptance tests.
cd target_app && ./test.sh
```

Other utilities:

```bash
.venv/bin/python -m scripts.probe_models flash        # which models this key can use
.venv/bin/python -m engine.schema.validate data/artifacts/operator_login.json
ENGINE_HEADLESS=1 .venv/bin/python -m engine.surface._manual_check   # observe the live app
```

## Repository layout

| Path | What it is |
|---|---|
| `engine/` | The capability engine. Contains no knowledge of any application. |
| `engine/schema/artifact.py` | The artifact contract: steps, typed inputs and outputs, checkpoint, known outcomes. |
| `engine/discovery/` | The agent loop, grounding, loop guard, model client. |
| `engine/replay/` | Deterministic execution. There is no model call anywhere in this package. |
| `engine/surface/` | The seam that touches the application. Playwright today, desktop later. |
| `engine/policy/defaults.yaml` | Operator safety policy. Applies to any application; override with `--policy`. |
| `target_app/` | Member Servicing Portal, a Flask app styled like a legacy system, used as one target. Swappable. |
| `scripts/` | Offline and live check suites, and the model prober. |
