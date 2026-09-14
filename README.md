# CoreBank Servicing Console

The **target application** for the computer-use automation take-home. It stands in for a
legacy bank back-office system: server-rendered Flask + Jinja2, table-based markup, one
`<iframe>`, and **no JSON/REST API for any banking action**. Full spec: [`TARGET_APP.md`](TARGET_APP.md).

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill in MONGODB_URI + FLASK_SECRET_KEY
python app.py               # http://127.0.0.1:5050
```

`MONGODB_URI` is optional. If it is unset or the cluster is unreachable, the app logs a
notice and falls back to an equivalent in-process store holding the same seed data, so the
automation target always comes up. With Mongo configured, collections are seeded on first
run; `python seed.py` re-seeds them from scratch.

## Sign-on credentials

| Username | Password | Display name |
|---|---|---|
| `operator1` | `pass1234` | Jordan Lee (Teller) |
| `operator2` | `pass5678` | Priya Nair (Servicing Specialist) |

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Redirects to `/login` — the single entry point |
| `/login` | GET, POST | Operator sign-on |
| `/logout` | GET | Clears the session |
| `/search` | GET, POST | Member lookup by exact Customer ID |
| `/members/<id>?tab=profile\|account\|transfer` | GET | Tabbed member record (tabs are plain links) |
| `/members/<id>/balance-frame` | GET | Balance table rendered inside the Account tab's iframe |
| `/members/<id>/transfer` | GET, POST | Transfer form; POST renders the confirmation screen only |
| `/members/<id>/transfer/confirm` | POST | Actually posts the transfer |
| `/healthz` | GET | `200 OK` uptime check (the only non-HTML route) |

## Outcome taxonomy for the automation layer

| Outcome | How to trigger |
|---|---|
| Success (read) | Search `10001`, open the Account tab, read the iframe |
| Success (multi-step) | Search `10001`, submit a transfer, click **Confirm Transfer** |
| Business — not found | Search `99999` → "No member found with that ID" banner |
| Business — permission denied | Search `10007` or `10009` → restricted banner |
| Recoverable — session expired mid-flow | Confirm a transfer for `10003` → redirect to `/login` |

## Seed data

10 customers (`10001`–`10010`), 2 staff users — all fabricated, all in `db.py`.
`10007` and `10009` are restricted (null balances); `10003` carries
`session_expires_on_confirm: true`.

## Verification

```bash
python smoke_test.py
```

Exercises all six acceptance criteria from §12 of the spec plus the auth guards, the
transfer validation paths, and the absence of an `/api` surface.

## Deliberate design notes

- No `data-testid` and no automation-convenience `id`/`class` on interactive elements.
  `<label for="f1">` associations use meaningless ids on purpose — the accessible name and
  the button text are the only handles an agent gets.
- The Account tab's balances exist **only** inside the iframe, never inline on the parent page.
- `POST /members/<id>/transfer` never moves money; it renders a review screen. Only
  `POST .../transfer/confirm` debits savings.
- Transfer validation (non-numeric amount, zero/negative, missing target account,
  insufficient funds) re-renders the form with an inline error. This is beyond the letter of
  the spec but does not interfere with any listed outcome — normal test amounts pass through.
