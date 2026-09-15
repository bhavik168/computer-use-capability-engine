# App 1 — Member Servicing Portal

The baseline app: search → detail → action, with inline validation, a not-found
outcome, a permission-denied outcome on restricted members, and a session that
expires on inactivity.

Server-rendered Flask + Jinja2, session-based operator sign-on, no `data-testid` or
other automation-convenience attributes, table-based layout, no CSS framework, no
JavaScript.

## Run

```bash
./run.sh          # http://127.0.0.1:5001
```

`run.sh` creates `.venv` if missing, installs `requirements.txt`, copies
`.env.example` to `.env` on first run, frees `$PORT` if something is already holding
it, and starts the app.

## Test

```bash
./test.sh
```

Runs `smoke_test.py` — Flask test-client acceptance checks covering every outcome in
the taxonomy below, the unauthenticated-access guards, the inactivity timeout and every
validation-error path. When the store is backed by MongoDB the suite re-seeds it first
so runs are repeatable.

## Database

`db.py` holds the canonical seed data as module-level constants and two stores with
identical method signatures:

| Store | `backend` | Notes |
|---|---|---|
| `MemoryStore` | `in-memory` | Process-local, no external dependency. Every accessor returns copies. |
| `MongoStore` | `mongodb` | Reads/writes the `operators`, `members`, `accounts` and `counters` collections. |

`get_store()` reads `MONGODB_URI` from the environment. If it is unset, still the
`.env.example` placeholder, or unreachable, it prints a notice on stderr and falls back
to `MemoryStore`. On a successful first connect to an empty database it seeds
automatically. `seed.py` re-seeds a configured instance on demand and exits 1 with a
clear message if `MONGODB_URI` is not set.

The sub-account sequence lives in `counters` on MongoDB and on the store instance
in memory, so generated sub-account ids stay unique either way.

## Sign-on credentials

| Username | Password |
|---|---|
| `operator1` | `pass123` |
| `operator2` | `creditunion1` |

Sessions expire after `SESSION_TIMEOUT_SECONDS` (default 90) of inactivity and redirect
to `/session-expired`. Unauthenticated access redirects to `/login`.

## Routes

| Method | Route | Screen |
|---|---|---|
| GET | `/healthz` | Liveness probe, returns `OK` |
| GET | `/` | Redirects to member search |
| GET, POST | `/login` | Operator sign-on |
| GET | `/session-expired` | Inactivity timeout screen |
| GET | `/logout` | Clears the session |
| GET, POST | `/members/search` | Search by member ID, name, or partial SSN |
| GET | `/members/<member_id>` | Member detail and linked accounts |
| GET, POST | `/members/<member_id>/edit` | Edit address and phone |
| GET | `/accounts/<account_id>` | Account detail, sub-accounts, transaction history |
| POST | `/accounts/<account_id>/subaccount` | Open a sub-account |

## Outcome taxonomy

| Outcome | How to reach it | What renders |
|---|---|---|
| Search hit | Search a member ID, a name fragment, or 4+ SSN characters | Results table with Active/RESTRICTED status |
| No matching member | Search a string that matches nothing | Inline notice banner, "normal search result, not an error" |
| Member not found | `/members/99999` | Inline notice banner |
| Permission denied | Open member `10005` or `10010` | Red deny banner; no member data shown |
| Account not found | `/accounts/CHK-99999` | Inline notice banner |
| Validation error — missing contact field | Save the edit form with a blank address or phone | "Address and Phone are both required fields." |
| Validation error — implausible phone | Save a phone containing no digits | "Phone number does not appear to be valid." |
| Contact updated | Save a valid address and phone | Green banner, change persisted |
| Validation error — missing purpose | Open a sub-account with a blank purpose | "Purpose is a required field." |
| Validation error — missing deposit | Open a sub-account with a blank deposit | "Initial deposit is a required field." |
| Validation error — non-numeric deposit | Deposit `abc` | "Initial deposit must be a valid number." |
| Validation error — negative deposit | Deposit `-25` | "Initial deposit cannot be negative." |
| Sub-account created | Open a sub-account with a valid purpose and deposit | Confirmation screen with the generated `SUB-…` id |
| Session expired | Idle past `SESSION_TIMEOUT_SECONDS`, then navigate | Redirect to `/session-expired` |

## Seed data

- 17 members `10001` … `10017`, with full contact details and SSNs.
- 2 restricted members — `10005` (Restricted Holdings LLC) and `10010`
  (Second Restricted Trust): the permission-denied cases. Restricted members hold
  no accounts.
- 30 accounts, a `CHK-<member_id>` and a `SAV-<member_id>` per non-restricted member,
  deterministically generated with 10–20 transactions each.
- `SAV-10001` starts with one sub-account, `SUB-10001-01` (Holiday Fund, $340.00).
- 2 operators.
- A partial SSN search needs at least `MIN_SSN_QUERY_LENGTH` (4) characters.

## Verification

```bash
./test.sh                                  # 39 acceptance checks
curl -s localhost:5001/healthz             # -> OK
```
