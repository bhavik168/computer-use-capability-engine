"""
Member Servicing Portal (App 1) — Flask routes.

The baseline app: search -> detail -> action, with inline validation, a
not-found outcome, a permission-denied outcome on restricted members, and a
session that expires on inactivity.

    ./run.sh    -> http://127.0.0.1:5050   (operator1/pass123)
"""
import os
import time

from flask import Flask, redirect, render_template, request, session, url_for

import db

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-dev-secret")
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "90"))
PORT = int(os.environ.get("PORT", "5050"))

store = db.get_store()


@app.template_filter("money")
def money(value):
    return f"${float(value):,.2f}"


@app.context_processor
def inject_operator():
    return {"operator": session.get("operator")}


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------

def touch_session():
    session["last_active"] = time.time()


def session_is_live():
    if not session.get("operator"):
        return False
    if time.time() - session.get("last_active", 0) > SESSION_TIMEOUT_SECONDS:
        session.clear()
        # Remember that there *was* a session, so the guard can tell an expired
        # operator (-> /session-expired) from one who never signed on (-> /login).
        session["_had"] = True
        return False
    touch_session()
    return True


@app.before_request
def guard():
    if request.endpoint in ("login", "static", "session_expired", "healthz"):
        return
    if not session_is_live():
        if session.get("_had"):
            return redirect(url_for("session_expired"))
        return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Session routes
# ---------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return "OK", 200


@app.route("/")
def index():
    return redirect(url_for("member_search"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        record = store.find_operator(request.form.get("username", ""),
                                     request.form.get("password", ""))
        if record:
            session.clear()
            session["operator"] = record["username"]
            session["_had"] = True
            touch_session()
            return redirect(url_for("member_search"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/session-expired")
def session_expired():
    session.clear()
    return render_template("session_expired.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

@app.route("/members/search", methods=["GET", "POST"])
def member_search():
    query = ""
    results = None
    if request.method == "POST":
        query = request.form.get("query", "").strip()
        results = store.search_members(query)
    return render_template("search.html", results=results, query=query)


@app.route("/members/<member_id>")
def member_detail(member_id):
    member = store.find_member(member_id)
    if not member:
        return render_template("member_not_found.html")
    if member["restricted"]:
        return render_template("permission_denied.html", member_id=member_id)
    return render_template("member_detail.html", member=member,
                           accounts=store.list_accounts_for_member(member_id))


@app.route("/members/<member_id>/edit", methods=["GET", "POST"])
def edit_member(member_id):
    member = store.find_member(member_id)
    if not member:
        return render_template("member_not_found.html")
    error = None
    saved = False
    if request.method == "POST":
        address = request.form.get("address", "").strip()
        phone = request.form.get("phone", "").strip()
        if not address or not phone:
            error = "Address and Phone are both required fields."
        elif not any(ch.isdigit() for ch in phone):
            error = "Phone number does not appear to be valid."
        else:
            member = store.update_member_contact(member_id, address, phone)
            saved = True
    return render_template("edit_member.html", member=member, error=error, saved=saved)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@app.route("/accounts/<account_id>")
def account_detail(account_id):
    account = store.find_account(account_id)
    if not account:
        return render_template("account_not_found.html")
    return render_template("account_detail.html", account=account,
                           member=store.find_member(account["member_id"]))


@app.route("/accounts/<account_id>/subaccount", methods=["POST"])
def create_subaccount(account_id):
    account = store.find_account(account_id)
    if not account:
        return render_template("account_not_found.html")

    purpose = request.form.get("purpose", "").strip()
    deposit = request.form.get("deposit", "").strip()
    error = None
    amount = None
    if not purpose:
        error = "Purpose is a required field."
    elif not deposit:
        error = "Initial deposit is a required field."
    else:
        try:
            amount = float(deposit)
            if amount < 0:
                error = "Initial deposit cannot be negative."
        except ValueError:
            error = "Initial deposit must be a valid number."
    if error:
        return render_template("subaccount_error.html", account_id=account_id,
                               error=error)

    sub = store.create_sub_account(account_id, purpose, amount)
    return render_template("subaccount_created.html", account_id=account_id, sub=sub)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
