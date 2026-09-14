"""CoreBank Servicing Console.

A deliberately legacy-styled, server-rendered back-office app. No JSON API for
any banking action, one entry point (login), full page loads only.
"""

import os

from dotenv import load_dotenv
from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "corebank-dev-secret"

store = db.get_store()


def current_operator():
    return session.get("operator")


def money(value):
    if value is None:
        return "--"
    return f"{value:,.2f}"


app.jinja_env.filters["money"] = money


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = store.find_staff_user(username, password)
        if user:
            session.clear()
            session["operator"] = {
                "username": user["username"],
                "display_name": user["display_name"],
            }
            return redirect(url_for("search"))
        return render_template(
            "login.html",
            error="Sign-on failed. Check the user ID and password and try again.",
            username=username,
        )
    return render_template("login.html", error=None, username="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET", "POST"])
def search():
    operator = current_operator()
    if not operator:
        return redirect(url_for("login"))

    if request.method == "POST":
        customer_id = (request.form.get("customer_id") or "").strip()
        if not customer_id:
            return render_template(
                "search.html",
                operator=operator,
                message="Enter a Customer ID to run a member lookup.",
                message_kind="warning",
                customer_id="",
            )

        customer = store.find_customer(customer_id)
        if not customer:
            return render_template(
                "search.html",
                operator=operator,
                message=f"No member found with that ID ({customer_id}).",
                message_kind="error",
                customer_id=customer_id,
            )
        if customer["status"] == "restricted":
            return render_template(
                "search.html",
                operator=operator,
                message=(
                    f"Account {customer_id} is restricted. "
                    "You do not have permission to service this member."
                ),
                message_kind="error",
                customer_id=customer_id,
            )
        return redirect(url_for("member", customer_id=customer_id, tab="profile"))

    return render_template(
        "search.html",
        operator=operator,
        message=None,
        message_kind=None,
        customer_id="",
    )


@app.route("/members/<customer_id>")
def member(customer_id):
    operator = current_operator()
    if not operator:
        return redirect(url_for("login"))

    customer = store.find_customer(customer_id)
    if not customer or customer["status"] == "restricted":
        return redirect(url_for("search"))

    tab = request.args.get("tab", "profile")
    if tab not in ("profile", "account", "transfer"):
        tab = "profile"

    return render_template(
        "member.html", operator=operator, customer=customer, tab=tab
    )


@app.route("/members/<customer_id>/balance-frame")
def balance_frame(customer_id):
    if not current_operator():
        return render_template("frame_expired.html")

    customer = store.find_customer(customer_id)
    if not customer or customer["status"] == "restricted":
        return render_template("frame_expired.html")

    return render_template("balance_frame.html", customer=customer)


@app.route("/members/<customer_id>/transfer", methods=["GET", "POST"])
def transfer(customer_id):
    operator = current_operator()
    if not operator:
        return redirect(url_for("login"))

    customer = store.find_customer(customer_id)
    if not customer or customer["status"] == "restricted":
        return redirect(url_for("search"))

    if request.method == "POST":
        raw_amount = (request.form.get("amount") or "").strip()
        target_account = (request.form.get("target_account") or "").strip()
        error = None
        amount = None

        try:
            amount = float(raw_amount.replace(",", ""))
        except ValueError:
            error = "Enter the transfer amount as a number, for example 125.00."

        if error is None and amount <= 0:
            error = "The transfer amount must be greater than zero."
        if error is None and not target_account:
            error = "Enter the target account number."
        if error is None and amount > (customer["savings_balance"] or 0):
            error = (
                "Insufficient funds in savings for this transfer. "
                f"Available balance is {money(customer['savings_balance'])}."
            )

        if error:
            return render_template(
                "transfer_form.html",
                operator=operator,
                customer=customer,
                error=error,
                amount=raw_amount,
                target_account=target_account,
            )

        return render_template(
            "transfer_confirm.html",
            operator=operator,
            customer=customer,
            amount=amount,
            target_account=target_account,
        )

    return render_template(
        "transfer_form.html",
        operator=operator,
        customer=customer,
        error=None,
        amount="",
        target_account="",
    )


@app.route("/members/<customer_id>/transfer/confirm", methods=["POST"])
def transfer_confirm(customer_id):
    operator = current_operator()
    if not operator:
        return redirect(url_for("login"))

    customer = store.find_customer(customer_id)
    if not customer or customer["status"] == "restricted":
        return redirect(url_for("search"))

    # Simulated mid-flow session timeout: the one built-in recoverable failure.
    if customer.get("session_expires_on_confirm"):
        session.clear()
        return redirect(url_for("login"))

    try:
        amount = float((request.form.get("amount") or "0").replace(",", ""))
    except ValueError:
        amount = 0.0
    target_account = (request.form.get("target_account") or "").strip()

    updated = store.apply_transfer(customer_id, amount) or customer
    digits = "".join(ch for ch in target_account if ch.isdigit()) or "0"
    reference = "TRF-{}-{}{}".format(customer_id, digits[-4:], int(amount * 100) % 10000)

    return render_template(
        "transfer_success.html",
        operator=operator,
        customer=updated,
        amount=amount,
        target_account=target_account,
        reference=reference,
    )


@app.route("/healthz")
def healthz():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
