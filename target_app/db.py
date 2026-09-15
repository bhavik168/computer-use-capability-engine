"""
Data layer for the Member Servicing Portal.

Canonical seed data lives here as module-level constants; MemoryStore and
MongoStore expose the same method surface so the routes never know which is in
use. `get_store()` picks between them from MONGODB_URI.
"""
import os
import random
import sys
from copy import deepcopy
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Canonical seed data
# ---------------------------------------------------------------------------

OPERATORS = [
    {"username": "operator1", "password": "pass123"},
    {"username": "operator2", "password": "creditunion1"},
]

MEMBERS = [
    {"id": "10001", "name": "Alice Johnson", "dob": "1985-03-12", "ssn": "123-45-6789",
     "address": "412 Maple St, Springfield, IL", "phone": "555-0101", "restricted": False},
    {"id": "10002", "name": "Bob Smith", "dob": "1979-11-02", "ssn": "234-56-7890",
     "address": "88 Oak Ave, Springfield, IL", "phone": "555-0102", "restricted": False},
    {"id": "10003", "name": "Carol Diaz", "dob": "1990-06-23", "ssn": "345-67-8901",
     "address": "19 Birch Ln, Shelbyville, IL", "phone": "555-0103", "restricted": False},
    {"id": "10004", "name": "David Chen", "dob": "1972-01-17", "ssn": "456-78-9012",
     "address": "77 Pine Rd, Shelbyville, IL", "phone": "555-0104", "restricted": False},
    {"id": "10005", "name": "Restricted Holdings LLC", "dob": "1960-01-01", "ssn": "000-00-0000",
     "address": "1 Confidential Way, Springfield, IL", "phone": "555-0105", "restricted": True},
    {"id": "10006", "name": "Elena Ruiz", "dob": "1995-09-30", "ssn": "567-89-0123",
     "address": "202 Elm St, Capital City, IL", "phone": "555-0106", "restricted": False},
    {"id": "10007", "name": "Frank Osei", "dob": "1988-04-05", "ssn": "678-90-1234",
     "address": "5 Cedar Ct, Capital City, IL", "phone": "555-0107", "restricted": False},
    {"id": "10008", "name": "Grace Kim", "dob": "1993-12-19", "ssn": "789-01-2345",
     "address": "310 Willow Dr, Springfield, IL", "phone": "555-0108", "restricted": False},
    {"id": "10009", "name": "Henry Wallace", "dob": "1966-07-08", "ssn": "890-12-3456",
     "address": "64 Aspen Way, Shelbyville, IL", "phone": "555-0109", "restricted": False},
    {"id": "10010", "name": "Second Restricted Trust", "dob": "1955-05-05", "ssn": "111-11-1111",
     "address": "2 Confidential Way, Springfield, IL", "phone": "555-0110", "restricted": True},
    {"id": "10011", "name": "Isla Fontaine", "dob": "1998-02-14", "ssn": "901-23-4567",
     "address": "9 Hemlock Ave, Capital City, IL", "phone": "555-0111", "restricted": False},
    {"id": "10012", "name": "Jamal Price", "dob": "1982-10-27", "ssn": "012-34-5678",
     "address": "455 Sycamore Blvd, Springfield, IL", "phone": "555-0112", "restricted": False},
    {"id": "10013", "name": "Karen Novak", "dob": "1977-08-11", "ssn": "123-98-7654",
     "address": "18 Poplar St, Shelbyville, IL", "phone": "555-0113", "restricted": False},
    {"id": "10014", "name": "Liam O'Brien", "dob": "1991-03-03", "ssn": "234-87-6543",
     "address": "70 Magnolia Ln, Capital City, IL", "phone": "555-0114", "restricted": False},
    {"id": "10015", "name": "Mia Torres", "dob": "1984-06-30", "ssn": "345-76-5432",
     "address": "12 Redwood Rd, Springfield, IL", "phone": "555-0115", "restricted": False},
    {"id": "10016", "name": "Noah Petrov", "dob": "1969-12-01", "ssn": "456-65-4321",
     "address": "3 Dogwood Ct, Shelbyville, IL", "phone": "555-0116", "restricted": False},
    {"id": "10017", "name": "Olivia Wren", "dob": "1996-04-22", "ssn": "567-54-3210",
     "address": "588 Chestnut Ave, Capital City, IL", "phone": "555-0117", "restricted": False},
]

TXN_DESCRIPTIONS = ["POS Purchase", "ACH Deposit", "ATM Withdrawal", "Check Deposit",
                    "Fee Assessed", "Interest Credit", "Transfer In", "Transfer Out"]

# A partial SSN search needs at least this many characters to match.
MIN_SSN_QUERY_LENGTH = 4


def _txn_history(rng):
    """One account's transaction history, built from the shared seeded RNG."""
    txns = []
    d = datetime(2026, 8, 1)
    for _ in range(rng.randint(10, 20)):
        d = d + timedelta(days=rng.randint(1, 3))
        txns.append({"date": d.strftime("%Y-%m-%d"),
                     "description": rng.choice(TXN_DESCRIPTIONS),
                     "amount": round(rng.uniform(-400, 900), 2)})
    return txns


def _build_accounts():
    """Deterministic build of the canonical account list (fixed RNG seed).

    Restricted members hold no accounts, which is what makes the
    permission-denied screen the only thing reachable for them.
    """
    rng = random.Random(20260801)
    accounts = []
    for member in MEMBERS:
        if member["restricted"]:
            continue
        accounts.append({"id": f"CHK-{member['id']}", "member_id": member["id"],
                         "type": "checking", "balance": round(rng.uniform(200, 5000), 2),
                         "transactions": _txn_history(rng), "sub_accounts": []})
        accounts.append({"id": f"SAV-{member['id']}", "member_id": member["id"],
                         "type": "savings", "balance": round(rng.uniform(500, 20000), 2),
                         "transactions": _txn_history(rng), "sub_accounts": []})

    # One member starts with an existing sub-account so the list is never empty
    # everywhere at once.
    by_id = {a["id"]: a for a in accounts}
    by_id["SAV-10001"]["sub_accounts"] = [
        {"id": "SUB-10001-01", "purpose": "Holiday Fund", "balance": 340.00}
    ]
    return accounts


ACCOUNTS = _build_accounts()

# The sub-account sequence the seeded SUB-10001-01 was issued from.
SUB_ACCOUNT_SEQ_START = 1


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class MemoryStore:
    """Process-local store. Every accessor returns copies, never live refs."""

    backend = "in-memory"

    def __init__(self):
        self._operators = deepcopy(OPERATORS)
        self._members = deepcopy(MEMBERS)
        self._accounts = deepcopy(ACCOUNTS)
        self._sub_account_seq = SUB_ACCOUNT_SEQ_START

    # -- operators ----------------------------------------------------------

    def find_operator(self, username, password):
        for op in self._operators:
            if op["username"] == username and op["password"] == password:
                return deepcopy(op)
        return None

    # -- members ------------------------------------------------------------

    def find_member(self, member_id):
        for m in self._members:
            if m["id"] == member_id:
                return deepcopy(m)
        return None

    def _raw_member(self, member_id):
        for m in self._members:
            if m["id"] == member_id:
                return m
        return None

    def search_members(self, query):
        """Match on exact member id, name substring, or a partial SSN."""
        results = []
        for m in self._members:
            if (query == m["id"]
                    or query.lower() in m["name"].lower()
                    or (len(query) >= MIN_SSN_QUERY_LENGTH and query in m["ssn"])):
                results.append(m)
        return deepcopy(results)

    def update_member_contact(self, member_id, address, phone):
        m = self._raw_member(member_id)
        if m is None:
            return None
        m["address"] = address
        m["phone"] = phone
        return deepcopy(m)

    # -- accounts -----------------------------------------------------------

    def find_account(self, account_id):
        for a in self._accounts:
            if a["id"] == account_id:
                return deepcopy(a)
        return None

    def _raw_account(self, account_id):
        for a in self._accounts:
            if a["id"] == account_id:
                return a
        return None

    def list_accounts_for_member(self, member_id):
        return deepcopy([a for a in self._accounts if a["member_id"] == member_id])

    def create_sub_account(self, account_id, purpose, deposit):
        account = self._raw_account(account_id)
        if account is None:
            return None
        self._sub_account_seq += 1
        sub = {"id": f"SUB-{account['member_id']}-{self._sub_account_seq:02d}",
               "purpose": purpose, "balance": deposit}
        account["sub_accounts"].append(sub)
        return deepcopy(sub)


# ---------------------------------------------------------------------------
# MongoDB store
# ---------------------------------------------------------------------------

class MongoStore:
    """Same surface as MemoryStore, backed by real collections."""

    backend = "mongodb"

    def __init__(self, database):
        self.db = database

    def seed(self):
        self.db.operators.delete_many({})
        self.db.operators.insert_many(deepcopy(OPERATORS))
        self.db.members.delete_many({})
        self.db.members.insert_many(deepcopy(MEMBERS))
        self.db.accounts.delete_many({})
        self.db.accounts.insert_many(deepcopy(ACCOUNTS))
        self.db.counters.delete_many({})
        self.db.counters.insert_one({"name": "sub_account_seq",
                                     "value": SUB_ACCOUNT_SEQ_START})

    @staticmethod
    def _clean(doc):
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc

    # -- operators ----------------------------------------------------------

    def find_operator(self, username, password):
        return self._clean(self.db.operators.find_one(
            {"username": username, "password": password}))

    # -- members ------------------------------------------------------------

    def find_member(self, member_id):
        return self._clean(self.db.members.find_one({"id": member_id}))

    def search_members(self, query):
        import re
        clauses = [{"id": query},
                   {"name": re.compile(re.escape(query), re.IGNORECASE)}]
        if len(query) >= MIN_SSN_QUERY_LENGTH:
            clauses.append({"ssn": re.compile(re.escape(query))})
        return [self._clean(d)
                for d in self.db.members.find({"$or": clauses}).sort("id", 1)]

    def update_member_contact(self, member_id, address, phone):
        self.db.members.update_one({"id": member_id},
                                   {"$set": {"address": address, "phone": phone}})
        return self.find_member(member_id)

    # -- accounts -----------------------------------------------------------

    def find_account(self, account_id):
        return self._clean(self.db.accounts.find_one({"id": account_id}))

    def list_accounts_for_member(self, member_id):
        return [self._clean(d)
                for d in self.db.accounts.find({"member_id": member_id}).sort("id", 1)]

    def create_sub_account(self, account_id, purpose, deposit):
        account = self.find_account(account_id)
        if account is None:
            return None
        counter = self.db.counters.find_one_and_update(
            {"name": "sub_account_seq"}, {"$inc": {"value": 1}},
            upsert=True, return_document=True)
        seq = counter["value"] if counter else SUB_ACCOUNT_SEQ_START + 1
        sub = {"id": f"SUB-{account['member_id']}-{seq:02d}",
               "purpose": purpose, "balance": deposit}
        self.db.accounts.update_one({"id": account_id},
                                    {"$push": {"sub_accounts": sub}})
        return sub


# ---------------------------------------------------------------------------
# Store selection
# ---------------------------------------------------------------------------

PLACEHOLDER_MARKERS = ("<user>", "<password>", "<cluster>", "<db_name>")


def _notice(message):
    print(f"[db] {message} Falling back to the in-memory store.", file=sys.stderr)


def get_store():
    """Return a MongoStore when MONGODB_URI points at a reachable instance,
    otherwise a MemoryStore with a notice on stderr."""
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        _notice("MONGODB_URI is not set.")
        return MemoryStore()
    if any(marker in uri for marker in PLACEHOLDER_MARKERS):
        _notice("MONGODB_URI is still the .env.example placeholder.")
        return MemoryStore()
    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        database = client.get_default_database()
        if database is None:
            database = client["member_servicing"]
        store = MongoStore(database)
        if database.members.count_documents({}) == 0:
            store.seed()
        return store
    except Exception as exc:  # pragma: no cover - depends on the environment
        _notice(f"MONGODB_URI is unreachable ({exc.__class__.__name__}).")
        return MemoryStore()
