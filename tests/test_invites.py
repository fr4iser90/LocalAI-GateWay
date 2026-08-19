"""Invite-only registration when self-registration is off."""

from __future__ import annotations

import re

from app.data.backends import upsert_source
from app.data.db import hash_api_key
from app.data.models import WebUser, ApiKey, RegistrationInvite
from tests.constants import BOOTSTRAP_PASSWORD, BOOTSTRAP_USER


def _login_admin(client) -> None:
    resp = client.post(
        "/login",
        data={"username": BOOTSTRAP_USER, "password": BOOTSTRAP_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _disable_self_registration() -> None:
    from app.data import db as dbmod

    assert dbmod.SessionLocal is not None
    with dbmod.SessionLocal() as db:
        from app.web.accounts import get_auth_settings

        auth = get_auth_settings(db)
        auth.allow_self_registration = False
        db.commit()


def _seed_source() -> None:
    from app.data import db as dbmod

    assert dbmod.SessionLocal is not None
    with dbmod.SessionLocal() as db:
        upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
        db.commit()


def _complete_setup() -> None:
    """Skip setup wizard redirect so admin routes like /users work."""
    from app.data import db as dbmod

    assert dbmod.SessionLocal is not None
    with dbmod.SessionLocal() as db:
        admin = db.query(WebUser).filter(WebUser.username == BOOTSTRAP_USER).first()
        assert admin is not None
        upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
        if not db.query(ApiKey).filter(ApiKey.owner_user_id == admin.id).first():
            db.add(
                ApiKey(
                    label="setup",
                    key_hash=hash_api_key("gw_invite_test_setup"),
                    key_prefix="gw_inv",
                    owner_user_id=admin.id,
                    is_active=True,
                )
            )
        db.commit()


def _create_invite_via_api(client, *, note: str = "") -> str:
    resp = client.post(
        "/users/invites",
        data={"note": note},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/users").text
    match = re.search(r"/register\?invite=([A-Za-z0-9_-]+)", page)
    assert match, page[:500]
    return match.group(1)


def test_register_closed_without_invite(gateway_client):
    _disable_self_registration()
    resp = gateway_client.get("/register")
    assert resp.status_code == 403
    assert "disabled" in resp.text.lower() or "invite" in resp.text.lower()

    resp = gateway_client.post(
        "/register",
        data={
            "username": "eve",
            "password": "EvePass1!",
            "password2": "EvePass1!",
            "email": "eve@example.test",
        },
    )
    assert resp.status_code == 403


def test_invite_link_allows_registration(gateway_client):
    _disable_self_registration()
    _complete_setup()
    _login_admin(gateway_client)

    token = _create_invite_via_api(gateway_client, note="friend")

    gateway_client.post("/logout", follow_redirects=False)

    resp = gateway_client.get(f"/register?invite={token}", follow_redirects=False)
    assert resp.status_code == 200
    assert 'name="invite"' in resp.text

    resp = gateway_client.post(
        "/register",
        data={
            "username": "friend1",
            "password": "FriendPass1!",
            "password2": "FriendPass1!",
            "email": "friend@example.test",
            "invite": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/me"

    from app.data import db as dbmod

    with dbmod.SessionLocal() as db:
        u = db.query(WebUser).filter(WebUser.username == "friend1").first()
        assert u is not None
        inv = (
            db.query(RegistrationInvite)
            .filter(RegistrationInvite.used_by_user_id == u.id)
            .first()
        )
        assert inv is not None
        assert inv.used_at is not None


def test_invite_single_use(gateway_client):
    _disable_self_registration()
    _complete_setup()
    _login_admin(gateway_client)

    token = _create_invite_via_api(gateway_client)
    gateway_client.post("/logout", follow_redirects=False)

    gateway_client.post(
        "/register",
        data={
            "username": "first",
            "password": "FirstPass1!",
            "password2": "FirstPass1!",
            "email": "first@example.test",
            "invite": token,
        },
    )

    resp = gateway_client.post(
        "/register",
        data={
            "username": "second",
            "password": "SecondPass1!",
            "password2": "SecondPass1!",
            "email": "second@example.test",
            "invite": token,
        },
    )
    assert resp.status_code == 403


def test_revoke_invite(gateway_client):
    _disable_self_registration()
    _complete_setup()
    _login_admin(gateway_client)

    token = _create_invite_via_api(gateway_client, note="x")

    from app.data import db as dbmod

    with dbmod.SessionLocal() as db:
        inv = (
            db.query(RegistrationInvite)
            .order_by(RegistrationInvite.id.desc())
            .first()
        )
        assert inv is not None
        inv_id = inv.id

    gateway_client.post(f"/users/invites/{inv_id}/revoke", follow_redirects=False)
    gateway_client.post("/logout", follow_redirects=False)
    resp = gateway_client.get(f"/register?invite={token}", follow_redirects=False)
    assert resp.status_code == 403
