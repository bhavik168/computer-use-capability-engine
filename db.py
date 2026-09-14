"""Database access for the CoreBank Servicing Console.

Primary store is MongoDB (Atlas free tier) via MONGODB_URI. If that variable is
absent or the cluster is unreachable, the app falls back to an in-process
dictionary store holding the same seed data, so the automation target can always
be brought up. The fallback is chatty about itself on stdout on purpose.
"""

import os
import sys

STAFF_USERS = [
    {
        "username": "operator1",
        "password": "pass1234",
        "display_name": "Jordan Lee (Teller)",
    },
    {
        "username": "operator2",
        "password": "pass5678",
        "display_name": "Priya Nair (Servicing Specialist)",
    },
]

CUSTOMERS = [
    {
        "customer_id": "10001",
        "name": "Alice Johnson",
        "email": "alice.johnson@example.test",
        "phone": "(555) 010-1001",
        "status": "active",
        "savings_balance": 4210.55,
        "checking_balance": 1120.30,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10002",
        "name": "Bob Smith",
        "email": "bob.smith@example.test",
        "phone": "(555) 010-1002",
        "status": "active",
        "savings_balance": 980.10,
        "checking_balance": 415.75,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10003",
        "name": "Carol Diaz",
        "email": "carol.diaz@example.test",
        "phone": "(555) 010-1003",
        "status": "active",
        "savings_balance": 15230.00,
        "checking_balance": 6140.20,
        "session_expires_on_confirm": True,
    },
    {
        "customer_id": "10004",
        "name": "Daniel Kim",
        "email": "daniel.kim@example.test",
        "phone": "(555) 010-1004",
        "status": "active",
        "savings_balance": 2450.00,
        "checking_balance": 890.00,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10005",
        "name": "Elena Petrov",
        "email": "elena.petrov@example.test",
        "phone": "(555) 010-1005",
        "status": "active",
        "savings_balance": 175.40,
        "checking_balance": 62.15,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10006",
        "name": "Frank Osei",
        "email": "frank.osei@example.test",
        "phone": "(555) 010-1006",
        "status": "active",
        "savings_balance": 8320.90,
        "checking_balance": 2210.45,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10007",
        "name": "Grace Liu",
        "email": "grace.liu@example.test",
        "phone": "(555) 010-1007",
        "status": "restricted",
        "savings_balance": None,
        "checking_balance": None,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10008",
        "name": "Henry Novak",
        "email": "henry.novak@example.test",
        "phone": "(555) 010-1008",
        "status": "active",
        "savings_balance": 610.00,
        "checking_balance": 133.90,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10009",
        "name": "Isabel Rossi",
        "email": "isabel.rossi@example.test",
        "phone": "(555) 010-1009",
        "status": "restricted",
        "savings_balance": None,
        "checking_balance": None,
        "session_expires_on_confirm": False,
    },
    {
        "customer_id": "10010",
        "name": "Jamal Carter",
        "email": "jamal.carter@example.test",
        "phone": "(555) 010-1010",
        "status": "active",
        "savings_balance": 3040.75,
        "checking_balance": 1575.60,
        "session_expires_on_confirm": False,
    },
]


class MemoryStore:
    """Stand-in for the two Mongo collections."""

    backend = "in-memory"

    def __init__(self):
        self.staff_users = [dict(u) for u in STAFF_USERS]
        self.customers = [dict(c) for c in CUSTOMERS]

    def find_staff_user(self, username, password):
        for user in self.staff_users:
            if user["username"] == username and user["password"] == password:
                return dict(user)
        return None

    def find_customer(self, customer_id):
        for customer in self.customers:
            if customer["customer_id"] == customer_id:
                return dict(customer)
        return None

    def apply_transfer(self, customer_id, amount):
        for customer in self.customers:
            if customer["customer_id"] == customer_id:
                customer["savings_balance"] = round(
                    (customer["savings_balance"] or 0) - amount, 2
                )
                return dict(customer)
        return None


class MongoStore:
    backend = "mongodb"

    def __init__(self, client, db_name="corebank"):
        self.client = client
        self.db = client[db_name]

    def find_staff_user(self, username, password):
        return self.db.staff_users.find_one(
            {"username": username, "password": password}, {"_id": 0}
        )

    def find_customer(self, customer_id):
        return self.db.customers.find_one({"customer_id": customer_id}, {"_id": 0})

    def apply_transfer(self, customer_id, amount):
        self.db.customers.update_one(
            {"customer_id": customer_id}, {"$inc": {"savings_balance": -amount}}
        )
        return self.find_customer(customer_id)

    def seed(self):
        self.db.staff_users.delete_many({})
        self.db.staff_users.insert_many([dict(u) for u in STAFF_USERS])
        self.db.customers.delete_many({})
        self.db.customers.insert_many([dict(c) for c in CUSTOMERS])


def get_store():
    """Return a MongoStore when MONGODB_URI works, else a MemoryStore."""
    uri = (os.environ.get("MONGODB_URI") or "").strip()
    if not uri or uri.startswith("mongodb+srv://<"):
        print(
            "[corebank] MONGODB_URI not set - using in-memory store "
            "(data resets on restart).",
            file=sys.stderr,
        )
        return MemoryStore()

    try:
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001 - any connection problem falls back
        print(
            f"[corebank] MongoDB unavailable ({exc.__class__.__name__}: {exc}) - "
            "falling back to in-memory store.",
            file=sys.stderr,
        )
        return MemoryStore()

    store = MongoStore(client)
    if store.db.customers.count_documents({}) == 0:
        store.seed()
        print("[corebank] Seeded MongoDB with staff users and customers.", file=sys.stderr)
    print("[corebank] Connected to MongoDB.", file=sys.stderr)
    return store
