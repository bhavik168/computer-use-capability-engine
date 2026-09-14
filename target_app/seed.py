"""Re-seed the MongoDB collections from the canonical data in db.py.

Usage:  python seed.py
Requires MONGODB_URI. Wipes and re-inserts staff_users and customers.
"""

import os
import sys

from dotenv import load_dotenv

import db

load_dotenv()


def main():
    uri = (os.environ.get("MONGODB_URI") or "").strip()
    if not uri or uri.startswith("mongodb+srv://<"):
        print("MONGODB_URI is not set - nothing to seed.", file=sys.stderr)
        print("The app will run against its in-memory store instead.", file=sys.stderr)
        return 1

    store = db.get_store()
    if store.backend != "mongodb":
        print("Could not reach MongoDB - see the message above.", file=sys.stderr)
        return 1

    store.seed()
    print(f"Seeded {len(db.STAFF_USERS)} staff users and {len(db.CUSTOMERS)} customers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
