# Evidence — the end-to-end thread

Every run here is real: a live Chromium against the Flask app in [`target_app/`](../../target_app),
captured on 2026-09-15. The discovery runs are genuine LLM-driven runs (Gemini,
`gemini-3.5-flash-lite`); the replay runs had no model in the decision path at all.

Each directory holds a `report.md` and a `screenshots/` folder with one screenshot per step.

## Discovery — the model works out how, once

These three produced the artifacts committed in [`data/artifacts/`](../../data/artifacts).
Each artifact's `provenance.created_from_run` names the run directory it came from, so the
artifact and the run that produced it can be checked against each other.

| Run | Goal | Shows |
|---|---|---|
| [`01-discovery-operator-login`](01-discovery-operator-login) | "Sign on to the console as the supplied operator" | Authentication discovered as an ordinary capability. The agent was never told there was a login form — it found the controls in the accessibility tree. The password was typed as `{{password}}` and never reached the model. |
| [`02-discovery-savings-balance`](02-discovery-savings-balance) | "Look up member 10001 and read their savings balance" | Search → result → detail → **extract**. The extract step is what gives the capability its typed output (`savings_balance`, `currency`). |
| [`03-discovery-create-subaccount`](03-discovery-create-subaccount) | "Open member 10002's savings account, create a new sub-account … and reach the confirmation screen" | A longer write flow: 7 steps, three caller parameters, ending on a confirmation screen. |

## Replay — deterministic execution, and the result contract

All four statuses, against the same capability, `lookup_member_and_get_savings_balance`.
This is the part worth reading closely: the distinction between them is the design claim.

| Run | Params | Result |
|---|---|---|
| [`04-replay-success`](04-replay-success) | `member_id=10001` | `success` — returns `{"savings_balance": "$8,714.97"}` |
| [`05-replay-success-different-member`](05-replay-success-different-member) | `member_id=10006` | `success` — returns `{"savings_balance": "$11,307.23"}` |
| [`06-replay-hard-failure-unknown-state`](06-replay-hard-failure-unknown-state) | `member_id=99999` | `hard_failure` — on a cold Knowledge Base |
| [`07-replay-business-outcome-not-found`](07-replay-business-outcome-not-found) | `member_id=99999` | `business_outcome` — `member_not_found`, after a human named it |
| [`08-replay-business-outcome-permission-denied`](08-replay-business-outcome-permission-denied) | `member_id=10005` | `business_outcome` — `permission_denied` |
| [`09-replay-recoverable-session-expired`](09-replay-recoverable-session-expired) | `member_id=10001` | `recoverable` — `session_expired` |

**04 and 05 are the same artifact.** A different `member_id` produces a different balance
because the recorded locator is `link:{{member_id}}`, not the member discovery happened to be
pointed at. A capability that quietly returned `$8,714.97` for every member would look
identical in a success log, which is why this run is here.

**06 and 07 are the same input against the same artifact**, and the difference between them is
the whole learning loop:

1. **06** — the application says "No matching member found." Nothing has ever named that
   state, so replay reports a `hard_failure` with what it expected, what it observed
   instead, and a screenshot. It also suggests detectors, already generalised (record ids
   stripped). Treating this as routine would mean guessing that an unfamiliar banner is not
   "the database is down".
2. A human names it once: `learn-outcome --code member_not_found --type business_outcome`.
   It is stored against the *application*, and the capability is patched to `v1.0.1`.
3. **07** — same input, same artifact, now a clean `business_outcome`. "No such member" is an
   answer the caller needs, not a crash.

**08 proves learning one outcome does not teach the others.** A restricted member was still a
`hard_failure` after `member_not_found` had been learned; it became a `business_outcome` only
once `permission_denied` was named in its turn (capability then at `v1.0.2`).

**09 is an injected condition.** The target app was run on port 5051 with
`SESSION_TIMEOUT_SECONDS=1` and the session was left to idle after sign-on, so it expired
mid-flow. The result is `recoverable`, not `business_outcome`: the caller learned nothing
about the member, and the artifact carries the recovery hint *"Replay the operator_login
prerequisite, then retry this capability."* That run also shows the same artifact executing
against a **second deployment on a different port** — artifacts store relative paths, not
hosts.

## Escalations

[`escalations/`](escalations) holds the screenshots captured at the moment each intervention
was raised. A `hard_failure` escalation carries candidate detectors for naming the state; a
`recoverable` one does not, because there is nothing to name — the run simply needs retrying.

These runs were non-interactive, so the intervention was recorded rather than blocking on a
terminal. Interactively (the default), the browser is **non-headless** and the run pauses with
the live window under the operator's control until they press Enter — the same window the
automation was driving, not a copy of it.

## Reproducing this

The committed artifacts ship **cold** — `known_outcomes` is empty — because a discovery run
only ever walks the success path and so cannot observe how an application fails. Running the
demo path in the [root README](../../README.md) reproduces this sequence from that cold start,
including step 06 legitimately failing before anything has been taught.
