# Target Application Specification — CoreBank Servicing Console

## 1. Purpose

This is the **target application** for the computer-use automation take-home project. It is not
a product in its own right — it exists purely to stand in for a real bank/credit union back-office
system: a legacy-styled, server-rendered web app with **no public API** that an AI agent must
operate the way a human staff member would.

Everything about this spec is written to preserve that constraint. If at any point an
implementation detail would make it easier to automate by exposing a clean API or JSON endpoints,
that detail is wrong for this project — the whole point is to force the automation layer to drive
the UI itself.

This document is a complete, standalone spec intended to be handed to a coding agent (e.g. Claude
Code) for implementation. It assumes no other context.

---

## 2. Hard Design Constraints

These are non-negotiable for this app to serve its purpose:

1. **No JSON/REST API for any banking action.** Search, viewing account details, and transferring
   funds must all happen through server-rendered HTML pages and standard form submissions
   (`GET`/`POST` with form-encoded bodies). No `/api/...` routes returning JSON for these actions.
2. **A single entry point.** The only way into the system is the login page. There are no deep
   links, no alternate routes, no "quick access" shortcuts that bypass login or search.
3. **Simple authentication.** Username + password, submitted via a plain HTML form, checked against
   stored credentials, session tracked via a standard server-side session cookie. No OAuth, no JWT,
   no MFA, no "forgot password" flow, no email verification. This is a staff/operator login for
   internal use, not a customer-facing signup flow.
4. **Server-rendered only.** No single-page app framework, no client-side routing, no fetch-based
   dynamic updates. Every page load is a full HTML response from the server, in the style of a
   pre-2015 enterprise web application.
5. **Legacy-styled markup.** Table-based layout, minimal or no CSS classes/IDs on interactive
   elements, at least one section rendered inside an `<iframe>`. Interactive elements should still
   have real, human-readable labels/button text (so the app is genuinely usable and automatable via
   accessible name), but should not carry `data-testid` or similarly automation-friendly hooks —
   that absence is the point.

---

## 3. Who Uses This App

This is a **back-office servicing console** used by bank/credit union staff, not a customer-facing
app. The person logging in is an employee (an "operator") looking up member/customer accounts on
behalf of callers or in-branch visitors. This framing matters because it's what the AI agent is
standing in for — it authenticates once as a staff user, then performs lookups and actions per
member on request.

---

## 4. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python, Flask | Lightweight, server-rendered by default, no scaffolding pressure toward a JSON API |
| Templates | Jinja2 (Flask's default) | Full server-side HTML rendering |
| Frontend | Plain HTML + minimal inline/embedded CSS | No JS framework; keeps the markup genuinely legacy-feeling |
| Auth/session | Flask's built-in server-side session (signed cookie) | Enough for a single staff login; no external auth provider needed |
| Database | MongoDB (cloud-hosted, free tier — see Section 9) | Flexible schema, trivial to seed a small dataset, no local DB setup required |

Do not introduce a frontend build step (no Webpack/Vite/React/Vue). Do not add a REST layer even
if it feels like "good practice" — for this project it actively works against the goal.

---

## 5. Data Model

Two collections, both in MongoDB.

### `staff_users` — operator login credentials

| Field | Type | Notes |
|---|---|---|
| `username` | string | e.g. `operator1` |
| `password` | string | Plain text is acceptable for this internal test app — do not add hashing complexity; this is not a security exercise. If a hashing library is trivially available, a simple hash is fine, but not required. |
| `display_name` | string | e.g. `"Jordan Lee (Teller)"` |

Seed with **2 staff accounts** — one is enough to log in with, a second exists so the app doesn't
feel artificially single-purpose.

### `customers` — the member/account records

| Field | Type | Notes |
|---|---|---|
| `customer_id` | string | Used for search, e.g. `"12345"` |
| `name` | string | Fabricated name |
| `email` | string | Fabricated, `@example.test` domain |
| `phone` | string | Fabricated |
| `status` | string | `"active"` or `"restricted"` |
| `savings_balance` | number or null | `null` if `status` is `restricted` |
| `checking_balance` | number or null | `null` if `status` is `restricted` |
| `session_expires_on_confirm` | boolean | If true, confirming a transfer for this customer redirects to the login page instead of succeeding — simulates a session timeout mid-flow. Only one seed record should have this set to `true`. |

**Seed exactly 10 customer records.** No more — this app only needs to exercise a handful of
distinct outcomes, not simulate a real customer base. Suggested seed set:

| customer_id | name | status | savings_balance | notes |
|---|---|---|---|---|
| 10001 | Alice Johnson | active | 4210.55 | normal case |
| 10002 | Bob Smith | active | 980.10 | normal case |
| 10003 | Carol Diaz | active | 15230.00 | `session_expires_on_confirm: true` |
| 10004 | Daniel Kim | active | 2450.00 | normal case |
| 10005 | Elena Petrov | active | 175.40 | normal case |
| 10006 | Frank Osei | active | 8320.90 | normal case |
| 10007 | Grace Liu | restricted | null | triggers permission-denied outcome |
| 10008 | Henry Novak | active | 610.00 | normal case |
| 10009 | Isabel Rossi | restricted | null | triggers permission-denied outcome |
| 10010 | Jamal Carter | active | 3040.75 | normal case |

A search for any `customer_id` not in this list (e.g. `"99999"`) should produce a clean "not found"
result — this doesn't need its own seed record, it's simply the absence of a match.

---

## 6. Screens & Navigation Flow

Only these pages exist. No others.

1. **Login** (`GET`/`POST /login`)
   Username + password fields, a submit button. On success, create a session and redirect to
   Search. On failure, redisplay the form with an inline error message. This is the only entry
   point into the system.

2. **Member Search** (`GET`/`POST /search`) — requires an active session
   A single text field for Customer ID, a submit button. On submit:
   - Match found, active → redirect to that member's Profile tab.
   - Match found, restricted → redisplay the search page with a "this account is restricted"
     banner.
   - No match → redisplay the search page with a "no member found with that ID" banner.

3. **Member view, tabbed** (`GET /members/<customer_id>?tab=profile|account|transfer`)
   Three tabs, navigated via plain links (not JS tab-switching):
   - **Profile** — table of name, email, phone, customer ID.
   - **Account** — the balance summary rendered inside an `<iframe>` pointing at a small internal
     page (e.g. `/members/<customer_id>/balance-frame`) that just shows savings/checking balances
     in a table. This is the one deliberate frameset-style page in the app.
   - **Transfer** — a link/button through to the transfer form below.

4. **Transfer form** (`GET`/`POST /members/<customer_id>/transfer`)
   Two fields: amount, target account number. Submitting does **not** execute the transfer — it
   renders a confirmation screen showing what's about to happen.

5. **Transfer confirmation** (`POST /members/<customer_id>/transfer/confirm`)
   A "Confirm" button that actually executes the transfer.
   - Normal case → render a Transfer Success page with a confirmation message.
   - If the customer record has `session_expires_on_confirm: true` → instead of succeeding,
     redirect to `/login` (simulating the session having expired mid-flow). This is intentional
     and is the one built-in "recoverable" failure condition in the app.

6. **Logout** (`GET /logout`) — clears the session, redirects to Login.

There is no dashboard, no account creation, no settings page, no admin panel. Keep the surface area
exactly this small.

---

## 7. Deliberate "Hostile Legacy" Markup Choices

These are intentional and should not be "cleaned up" during implementation:

- Use `<table>`-based layout for structural page layout, not just for genuinely tabular data.
- Do not add `id` or `class` attributes to interactive elements purely for automation convenience.
  Real `<label for="...">` associations and real button/link text are fine and expected — that's
  what makes the app usable at all — but no `data-testid`, no semantically meaningful `id="submit-btn"`
  style hooks.
- The Account tab's balance display must be inside an `<iframe>`, not inline in the main page.
- Keep styling minimal and dated (basic inline `<style>`, default fonts, visible table borders) —
  no CSS framework, no modern layout (no flexbox/grid needed).

---

## 8. Outcome Taxonomy This App Must Support

The app exists to let an automation layer exercise three categories of run outcome. Each must be
reliably and deterministically reproducible by searching for a specific, known `customer_id`:

| Outcome type | How to trigger | Example customer_id |
|---|---|---|
| Business outcome — not found | Search for an ID not in the seed set | `99999` |
| Business outcome — permission denied | Search for a `restricted` customer | `10007` or `10009` |
| Recoverable — session expired mid-flow | Confirm a transfer for the flagged customer | `10003` |
| Success (read) | Search any active customer, view balance | `10001` |
| Success (multi-step, with confirmation) | Search any active customer, submit a transfer, confirm | `10001`, `10002`, etc. |
| Hard failure — unhandled error | Submit a transfer with amount > 1,000,000 for any active customer | `10001` |

---

## 9. Database: Free Cloud Hosting Options

**Recommended: MongoDB Atlas (free M0 tier).**
- Free forever tier, no credit card required for the M0 cluster.
- 512 MB storage — vastly more than 10 small customer records will ever need.
- Gives you a standard `mongodb+srv://...` connection string to drop into an environment variable.
- Setup: create a free account at mongodb.com/atlas → create a free M0 cluster → create a database
  user (username/password) → under Network Access, allow your current IP (or `0.0.0.0/0` for
  simplicity in a throwaway test project) → copy the connection string into a `.env` file as
  `MONGODB_URI`.

**Alternatives, if MongoDB Atlas doesn't suit:**
- **Firebase Firestore** (Google) — also free tier, also a flexible document store, slightly
  different query model but equally simple for 10 records.
- **Supabase** (free tier, Postgres-based) — if a relational table with 10 rows is preferred over a
  document store, this is the easiest managed-Postgres free option with a generous free tier.

Given the dataset is tiny (10 customers, 2 staff users), any of these will work identically well in
practice — MongoDB Atlas is recommended mainly because "document store, flexible schema" matches
the spirit of "keep it simple, unstructured" most directly.

**Running the app itself:** run the Flask app locally (`python app.py`, e.g. on
`http://127.0.0.1:5050`) rather than deploying it publicly. The automation layer (Playwright) will
point at this local address. There's no benefit to hosting the web app itself remotely for this
project — only the database needs to be cloud-hosted, and only because it's genuinely easier than
running a local Mongo instance.

---

## 10. Environment Variables

```
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/corebank
FLASK_SECRET_KEY=<any random string, used to sign the session cookie>
```

No other configuration should be required to run the app locally.

---

## 11. Explicit Non-Goals

- No REST/JSON API for banking actions (a plain `/healthz` returning `200 OK` for uptime checking
  is fine if genuinely needed, but nothing beyond that).
- No client-side JavaScript framework.
- No real payment rails, no real financial integration of any kind.
- No real customer data — every seed record is fabricated.
- No password hashing complexity, no MFA, no OAuth — plain session auth is sufficient and correct
  for this project's purpose.
- No admin panel, no customer self-service, no account creation flow.

---

## 12. What "Done" Looks Like

A working instance of this app should let someone:
1. Open `http://127.0.0.1:5050`, get redirected to `/login`.
2. Log in with a seeded staff username/password.
3. Search for `10001`, land on that member's Profile tab.
4. Click through to the Account tab and see the balance rendered inside an iframe.
5. Click through to Transfer, submit an amount and target account, see the confirmation screen,
   click Confirm, and see a success message.
6. Separately, search for `99999` and see a "not found" banner; search for `10007` and see a
   "restricted" banner; transfer-confirm for `10003` and get redirected back to the login page.

If all six of those work, the app is complete and ready to be the automation target.
