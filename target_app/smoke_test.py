"""
Acceptance smoke tests for the Member Servicing Portal.

Covers every outcome in the app's taxonomy: search hit by id, name and partial
SSN, the no-match outcome, member and account not-found, the permission-denied
outcome on restricted members, the contact-edit validation errors and success,
and every sub-account validation error plus the creation confirmation. Auth
guards and the inactivity timeout are checked too.
"""
import sys
import time

import app as application
import db

FAILURES = []


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def sign_in(client, username="operator1", password="pass123"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def text(response):
    return response.get_data(as_text=True)


def main():
    if application.store.backend == "mongodb":
        application.store.seed()
        print("(re-seeded MongoDB for a repeatable run)")
    print(f"(store backend: {application.store.backend})\n")

    application.app.config["TESTING"] = True
    restricted = next(m for m in db.MEMBERS if m["restricted"])
    plain = next(m for m in db.MEMBERS if not m["restricted"])

    # -- health and auth guards ---------------------------------------------
    with application.app.test_client() as c:
        r = c.get("/healthz")
        check("GET /healthz returns OK, 200", r.status_code == 200 and text(r) == "OK")

    with application.app.test_client() as c:
        for path in ("/", "/members/search", f"/members/{plain['id']}",
                     f"/members/{plain['id']}/edit", f"/accounts/CHK-{plain['id']}"):
            r = c.get(path)
            if not (r.status_code == 302 and "/login" in r.headers["Location"]):
                check(f"unauthenticated {path} redirects to /login", False)
                break
        else:
            check("unauthenticated GET routes all redirect to /login", True)
        r = c.post(f"/accounts/CHK-{plain['id']}/subaccount", data={})
        check("unauthenticated sub-account POST redirects to /login",
              r.status_code == 302 and "/login" in r.headers["Location"])

    with application.app.test_client() as c:
        r = c.post("/login", data={"username": "operator1", "password": "wrong"})
        check("bad credentials show an error", "Invalid username or password" in text(r))
        r = sign_in(c, "operator2", "creditunion1")
        check("the second seeded operator can sign on", "Member Search" in text(r))

    # -- inactivity timeout --------------------------------------------------
    with application.app.test_client() as c:
        sign_in(c)
        with c.session_transaction() as s:
            s["last_active"] = time.time() - application.SESSION_TIMEOUT_SECONDS - 1
        r = c.get("/members/search")
        check("an idle session redirects to /session-expired",
              r.status_code == 302 and "/session-expired" in r.headers["Location"])
        check("the session-expired screen explains the timeout",
              "expired due to inactivity" in text(c.get("/session-expired")))

    # -- member search -------------------------------------------------------
    with application.app.test_client() as c:
        sign_in(c)
        r = c.post("/members/search", data={"query": plain["id"]})
        check("search by member id finds the member",
              plain["id"] in text(r) and plain["name"] in text(r))
        r = c.post("/members/search", data={"query": plain["name"].split()[0].lower()})
        check("search by name is case-insensitive", plain["name"] in text(r))
        r = c.post("/members/search", data={"query": plain["ssn"][-4:]})
        check("search by a partial SSN of 4+ characters finds the member",
              plain["id"] in text(r))
        r = c.post("/members/search", data={"query": "1"})
        check("a partial SSN shorter than the minimum does not match on SSN",
              plain["name"] not in text(r))
        r = c.post("/members/search", data={"query": "no-such-member"})
        check("a search with no matches shows the no-results outcome",
              "No matching member found" in text(r)
              and "normal search result, not an error" in text(r))
        r = c.post("/members/search", data={"query": restricted["name"]})
        check("a restricted member appears in results flagged RESTRICTED",
              "RESTRICTED" in text(r))

    # -- member detail, not found, permission denied -------------------------
    with application.app.test_client() as c:
        sign_in(c)
        body = text(c.get(f"/members/{plain['id']}"))
        check("member detail shows the member's contact info",
              plain["address"] in body and plain["phone"] in body)
        check("member detail masks the SSN",
              f"***-**-{plain['ssn'][-4:]}" in body and plain["ssn"] not in body)
        check("member detail lists both linked accounts",
              f"CHK-{plain['id']}" in body and f"SAV-{plain['id']}" in body)

        r = c.get("/members/99999")
        check("unknown member shows the not-found outcome",
              "Member not found" in text(r) and "normal result, not an error" in text(r))

        r = c.get(f"/members/{restricted['id']}")
        body = text(r)
        check("a restricted member shows the permission-denied outcome",
              "Permission Denied" in body and "restricted access" in body)
        check("the permission-denied screen leaks no member data",
              restricted["ssn"] not in body and restricted["address"] not in body)

    # -- contact edit: validation and success --------------------------------
    with application.app.test_client() as c:
        sign_in(c)
        target = db.MEMBERS[1]
        r = c.post(f"/members/{target['id']}/edit",
                   data={"address": "", "phone": "555-0000"})
        check("editing with a blank address is a validation error",
              "both required fields" in text(r))
        r = c.post(f"/members/{target['id']}/edit",
                   data={"address": "1 New St", "phone": ""})
        check("editing with a blank phone is a validation error",
              "both required fields" in text(r))
        r = c.post(f"/members/{target['id']}/edit",
                   data={"address": "1 New St", "phone": "no-digits-here"})
        check("a phone with no digits is a validation error",
              "does not appear to be valid" in text(r))
        check("no failed edit was persisted",
              application.store.find_member(target["id"])["address"]
              == target["address"])

        r = c.post(f"/members/{target['id']}/edit",
                   data={"address": "900 Updated Ave, Springfield, IL",
                         "phone": "555-9999"})
        check("a valid edit reports success", "updated successfully" in text(r))
        updated = application.store.find_member(target["id"])
        check("the edit is persisted",
              updated["address"] == "900 Updated Ave, Springfield, IL"
              and updated["phone"] == "555-9999")

        r = c.get("/members/99999/edit")
        check("editing an unknown member shows the not-found outcome",
              "Member not found" in text(r))

    # -- account detail ------------------------------------------------------
    with application.app.test_client() as c:
        sign_in(c)
        body = text(c.get("/accounts/SAV-10001"))
        check("account detail shows the seeded sub-account",
              "SUB-10001-01" in body and "Holiday Fund" in body)
        check("account detail lists transaction history",
              "Transaction History" in body
              and len(application.store.find_account("SAV-10001")["transactions"]) >= 10)
        check("account detail renders negative amounts with a leading minus",
              "-$" in body or all(t["amount"] >= 0 for t in
                                  application.store.find_account("SAV-10001")["transactions"]))
        body = text(c.get(f"/accounts/CHK-{plain['id']}"))
        check("an account with no sub-accounts shows None", ">None<" in body)
        r = c.get("/accounts/CHK-99999")
        check("unknown account shows the not-found outcome",
              "Account not found" in text(r))

    # -- sub-account creation: validation and success ------------------------
    with application.app.test_client() as c:
        sign_in(c)
        acct = f"CHK-{plain['id']}"
        for label, payload, expected in (
            ("a missing purpose", {"purpose": "", "deposit": "100"},
             "Purpose is a required field."),
            ("a missing deposit", {"purpose": "Vacation", "deposit": ""},
             "Initial deposit is a required field."),
            ("a non-numeric deposit", {"purpose": "Vacation", "deposit": "abc"},
             "must be a valid number"),
            ("a negative deposit", {"purpose": "Vacation", "deposit": "-25"},
             "cannot be negative"),
        ):
            r = c.post(f"/accounts/{acct}/subaccount", data=payload)
            if expected not in text(r):
                check(f"creating a sub-account with {label} is a validation error", False)
                break
        else:
            check("every sub-account validation error is reported inline", True)

        before = len(application.store.find_account(acct)["sub_accounts"])
        check("no failed sub-account was created", before == 0)

        r = c.post(f"/accounts/{acct}/subaccount",
                   data={"purpose": "Holiday Fund 2027", "deposit": "250.75"})
        body = text(r)
        check("a valid sub-account shows the creation confirmation",
              "successfully created" in body and "$250.75" in body
              and "Holiday Fund 2027" in body)
        subs = application.store.find_account(acct)["sub_accounts"]
        check("the sub-account is persisted against the account", len(subs) == 1)
        check("the new sub-account id is derived from the member id",
              subs[0]["id"].startswith(f"SUB-{plain['id']}-"))
        check("a zero deposit is accepted",
              "successfully created" in text(
                  c.post(f"/accounts/{acct}/subaccount",
                         data={"purpose": "Zero Start", "deposit": "0"})))
        check("sub-account ids stay unique",
              len({s["id"] for s in
                   application.store.find_account(acct)["sub_accounts"]}) == 2)

        r = c.post("/accounts/CHK-99999/subaccount",
                   data={"purpose": "X", "deposit": "1"})
        check("creating a sub-account on an unknown account shows not-found",
              "Account not found" in text(r))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
