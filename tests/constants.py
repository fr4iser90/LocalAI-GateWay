"""Fake credentials and env defaults for pytest — not for production.

Unit / TestClient tests must never depend on the developer's .env.
conftest.py applies these via monkeypatch; HTTP tests import the same names here.
"""

from __future__ import annotations

# App bootstrap (init_db → platform admin)
BOOTSTRAP_USER = "admin"
BOOTSTRAP_PASSWORD = "test-admin-pass"

# Isolated install env (conftest data_dir fixture)
TEST_SESSION_SECRET = "test-session-secret-not-for-prod"
TEST_DOMAIN = "example.test"

# Extra users seeded in TestClient flows (not created by bootstrap)
USER1_NAME = "user1"
USER1_PASSWORD = "user-pass-123A!"

FORCED_USER_NAME = "forced"
FORCED_OLD_PASSWORD = "Temp-pass-123A!"
FORCED_NEW_PASSWORD = "New-pass-123A!"

# Security authz matrix
ALICE_NAME = "alice"
ALICE_PASSWORD = "alice-pass"
BOB_NAME = "bob"
BOB_PASSWORD = "bob-pass"
ALICE_API_KEY = "gw_sec_alice_chat_only_0001"
BOB_API_KEY = "gw_sec_bob_chat_only_0002"
