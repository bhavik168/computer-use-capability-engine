"""End-to-end smoke test for the six acceptance criteria in TARGET_APP.md.

Runs against the Flask test client (no server needed):  python smoke_test.py
"""

from app import app

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def sign_on(client, username="operator1", password="pass1234"):
    return client.post(
        "/login", data={"username": username, "password": password}
    )


def main():
    app.config["TESTING"] = True

    # 1. Root redirects to login.
    with app.test_client() as c:
        r = c.get("/")
        check("1. / redirects to /login", r.status_code == 302 and "/login" in r.headers["Location"])

        # Bad credentials stay on the form with an error.
        r = c.post("/login", data={"username": "operator1", "password": "wrong"})
        check("   bad password shows inline error", b"Sign-on failed" in r.data)

        # Unauthenticated access to search bounces to login.
        r = c.get("/search")
        check("   /search without session redirects to /login", r.status_code == 302 and "/login" in r.headers["Location"])

    # 2. Log in with a seeded staff user.
    with app.test_client() as c:
        r = sign_on(c)
        check("2. login redirects to /search", r.status_code == 302 and "/search" in r.headers["Location"])

        # 3. Search 10001 -> profile tab.
        r = c.post("/search", data={"customer_id": "10001"})
        check("3. search 10001 redirects to profile tab",
              r.status_code == 302 and "/members/10001" in r.headers["Location"]
              and "tab=profile" in r.headers["Location"])
        r = c.get("/members/10001?tab=profile")
        check("   profile shows name/email/phone/id",
              all(s in r.data for s in [b"Alice Johnson", b"alice.johnson@example.test",
                                        b"(555) 010-1001", b"10001"]))

        # 4. Account tab renders an iframe; frame shows balances.
        r = c.get("/members/10001?tab=account")
        check("4. account tab contains an iframe",
              b"<iframe" in r.data and b"/members/10001/balance-frame" in r.data)
        check("   balances are NOT inline on the account page", b"4,210.55" not in r.data)
        r = c.get("/members/10001/balance-frame")
        check("   balance frame shows savings and checking",
              b"4,210.55" in r.data and b"1,120.30" in r.data)

        # 5. Transfer -> confirmation -> success.
        r = c.get("/members/10001/transfer")
        check("5. transfer form renders", b"Review Transfer" in r.data)
        r = c.post("/members/10001/transfer", data={"amount": "150.00", "target_account": "998877"})
        check("   review screen shown, nothing posted yet",
              b"Confirm Transfer" in r.data and b"has not been posted" in r.data)
        r = c.get("/members/10001/balance-frame")
        check("   balance unchanged before confirm", b"4,210.55" in r.data)
        r = c.post("/members/10001/transfer/confirm",
                   data={"amount": "150.00", "target_account": "998877"})
        check("   confirm posts the transfer",
              r.status_code == 200 and b"Transfer completed successfully" in r.data)
        check("   remaining balance is debited", b"4,060.55" in r.data)

        # Validation paths on the transfer form.
        r = c.post("/members/10002/transfer", data={"amount": "abc", "target_account": "1"})
        check("   non-numeric amount rejected", b"as a number" in r.data)
        r = c.post("/members/10002/transfer", data={"amount": "10", "target_account": ""})
        check("   missing target account rejected", b"target account number" in r.data)
        r = c.post("/members/10002/transfer", data={"amount": "999999", "target_account": "1"})
        check("   over-balance transfer rejected", b"Insufficient funds" in r.data)

        # 6a. Not found.
        r = c.post("/search", data={"customer_id": "99999"})
        check("6a. 99999 shows not-found banner",
              r.status_code == 200 and b"No member found with that ID" in r.data)

        # 6b. Restricted.
        r = c.post("/search", data={"customer_id": "10007"})
        check("6b. 10007 shows restricted banner",
              r.status_code == 200 and b"is restricted" in r.data)
        r = c.post("/search", data={"customer_id": "10009"})
        check("    10009 shows restricted banner", b"is restricted" in r.data)
        r = c.get("/members/10007?tab=profile")
        check("    restricted member page is not reachable directly",
              r.status_code == 302 and "/search" in r.headers["Location"])

    # 6c. Session expires mid-flow for 10003.
    with app.test_client() as c:
        sign_on(c)
        c.post("/search", data={"customer_id": "10003"})
        r = c.post("/members/10003/transfer", data={"amount": "50", "target_account": "12345"})
        check("6c. 10003 reaches the confirmation screen", b"Confirm Transfer" in r.data)
        r = c.post("/members/10003/transfer/confirm",
                   data={"amount": "50", "target_account": "12345"})
        check("    confirm redirects to /login (session expired)",
              r.status_code == 302 and "/login" in r.headers["Location"])
        r = c.get("/search")
        check("    session really is cleared",
              r.status_code == 302 and "/login" in r.headers["Location"])

    # Logout.
    with app.test_client() as c:
        sign_on(c, "operator2", "pass5678")
        r = c.get("/logout")
        check("7. second staff account can sign on and off",
              r.status_code == 302 and "/login" in r.headers["Location"])

    # No JSON API surface beyond healthz.
    with app.test_client() as c:
        r = c.get("/healthz")
        check("8. /healthz returns 200 OK", r.status_code == 200 and r.data == b"OK")
        r = c.get("/api/customers")
        check("   no /api routes exist", r.status_code == 404)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
