#!/usr/bin/env python3
"""One-shot dev seed: demo users + usage. Not for production.

Usage (local repo):
  python scripts/seed_demo.py

Usage (running compose container):
  docker exec llm-auth-gateway python scripts/seed_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.data import db as dbmod
from app.demo_seed import DEMO_PASSWORD, clear_demo_world, seed_demo_world


def main() -> int:
    settings = get_settings()
    dbmod.init_db(settings)
    assert dbmod.SessionLocal is not None

    clear = "--clear" in sys.argv
    with dbmod.SessionLocal() as db:
        if clear:
            result = clear_demo_world(db)
            db.commit()
            print(f"Cleared {result['events']} demo events, {result['users']} demo users.")
            return 0

        result = seed_demo_world(db, count=280)
        db.commit()

    print(f"Seeded {result['events']} demo usage events.")
    print(f"Password for all demo users: {DEMO_PASSWORD}")
    print("Log in at /login as:")
    for u in result["users"]:
        flag = " (must change password)" if u["must_change_password"] else ""
        print(f"  - {u['username']}{flag}")
    print("Global stats on Overview are enabled for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
