"""Re-seed a configured MongoDB instance from the canonical constants in db.py."""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db


def main():
    if not os.environ.get("MONGODB_URI", "").strip():
        print("MONGODB_URI is not set. Copy .env.example to .env and point "
              "MONGODB_URI at your MongoDB instance before seeding.", file=sys.stderr)
        return 1
    store = db.get_store()
    if store.backend != "mongodb":
        print("Could not connect to MongoDB with the configured MONGODB_URI. "
              "Nothing was seeded.", file=sys.stderr)
        return 1
    store.seed()
    print(f"Seeded {len(db.MEMBERS)} members, {len(db.ACCOUNTS)} accounts and "
          f"{len(db.OPERATORS)} operators.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
