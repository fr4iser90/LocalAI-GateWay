"""Admin create without username → email login → password change (username optional)."""

from __future__ import annotations

from app.data.backends import upsert_source
from app.data.db import hash_api_key
from app.data.models import WebUser, ApiKey
from app.web.accounts import (
    is_pending_username,
    pending_username_for_id,
    user_needs_onboarding,
    user_needs_username,
    validate_username,
)
from tests.constants import BOOTSTRAP_PASSWORD, BOOTSTRAP_USER


def _login(client, username: str, password: str):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _complete_setup() -> None:
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
                    key_hash=hash_api_key("gw_create_flow_setup"),
                    key_prefix="gw_cre",
                    owner_user_id=admin.id,
                    is_active=True,
                )
            )
        db.commit()


def test_validate_username_rules():
    assert validate_username("ab") is not None
    assert validate_username("pending-1") is not None
    assert validate_username("a@b.com") is not None
    assert validate_username("good_user.1") is None


def test_admin_create_without_username_email_login_only(gateway_client):
    _complete_setup()
    assert _login(gateway_client, BOOTSTRAP_USER, BOOTSTRAP_PASSWORD).status_code == 303

    resp = gateway_client.post(
        "/users/new",
        data={
            "email": "friend@example.com",
            "password": "Temp-pass-123A!",
            "must_change_password": "on",
            "services": ["chat"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from app.data import db as dbmod

    assert dbmod.SessionLocal is not None
    with dbmod.SessionLocal() as db:
        u = db.query(WebUser).filter(WebUser.email == "friend@example.com").first()
        assert u is not None
        assert is_pending_username(u.username)
        assert u.username == pending_username_for_id(u.id)
        assert u.must_change_password is True
        assert user_needs_username(u)
        assert user_needs_onboarding(u)
        uid = u.id
        pending = u.username

    gateway_client.post("/logout", follow_redirects=False)
    login = _login(gateway_client, "friend@example.com", "Temp-pass-123A!")
    assert login.status_code == 303
    assert login.headers["location"] == "/account"

    blocked = gateway_client.get("/me", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/account"

    # Password only — username stays optional / pending
    claim = gateway_client.post(
        "/account/update",
        data={
            "password": "New-pass-123A!",
            "password2": "New-pass-123A!",
        },
        follow_redirects=False,
    )
    assert claim.status_code == 303
    assert claim.headers["location"] == "/me"

    with dbmod.SessionLocal() as db:
        u = db.get(WebUser, uid)
        assert u is not None
        assert u.username == pending
        assert u.must_change_password is False
        assert user_needs_username(u)
        assert not user_needs_onboarding(u)

    me = gateway_client.get("/me", follow_redirects=False)
    assert me.status_code == 200
    assert "friend@example.com" in me.text


def test_optional_username_can_be_set_later(gateway_client):
    _complete_setup()
    assert _login(gateway_client, BOOTSTRAP_USER, BOOTSTRAP_PASSWORD).status_code == 303
    gateway_client.post(
        "/users/new",
        data={
            "email": "later@example.com",
            "password": "Temp-pass-123A!",
            "must_change_password": "on",
            "services": ["chat"],
        },
        follow_redirects=False,
    )
    gateway_client.post("/logout", follow_redirects=False)
    assert _login(gateway_client, "later@example.com", "Temp-pass-123A!").status_code == 303
    assert (
        gateway_client.post(
            "/account/update",
            data={"password": "New-pass-123A!", "password2": "New-pass-123A!"},
            follow_redirects=False,
        ).headers["location"]
        == "/me"
    )

    # Later: claim username with current password
    set_name = gateway_client.post(
        "/account/update",
        data={
            "username": "lateruser",
            "email": "later@example.com",
            "current_password": "New-pass-123A!",
        },
        follow_redirects=False,
    )
    assert set_name.status_code == 303

    from app.data import db as dbmod

    with dbmod.SessionLocal() as db:
        u = db.query(WebUser).filter(WebUser.email == "later@example.com").first()
        assert u is not None
        assert u.username == "lateruser"
        assert not user_needs_username(u)


def test_admin_create_welcome_email_when_requested(gateway_client, monkeypatch):
    _complete_setup()
    assert _login(gateway_client, BOOTSTRAP_USER, BOOTSTRAP_PASSWORD).status_code == 303

    sent: list[dict] = []

    def _fake_send_mail(db, *, to_email, subject, body_text):
        sent.append({"to": to_email, "subject": subject, "body": body_text})

    import app.mailer as mailer_mod

    monkeypatch.setattr(mailer_mod, "send_mail", _fake_send_mail)
    monkeypatch.setattr(mailer_mod, "smtp_ready", lambda cfg: True)
    monkeypatch.setattr(
        mailer_mod,
        "get_smtp",
        lambda db: type(
            "Cfg",
            (),
            {
                "enabled": True,
                "host": "smtp.test",
                "from_email": "gw@test",
                "public_base_url": "https://ai.example.com",
            },
        )(),
    )

    resp = gateway_client.post(
        "/users/new",
        data={
            "email": "welcome@example.com",
            "password": "Temp-pass-123A!",
            "must_change_password": "on",
            "send_welcome_email": "on",
            "welcome_note": "Hello from admin — TTS Thomas is enabled.",
            "services": ["chat"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(sent) == 1
    assert sent[0]["to"] == "welcome@example.com"
    assert "Your OnPrem AI Gateway account" in sent[0]["subject"]
    assert "Temp-pass-123A!" in sent[0]["body"]
    assert "https://ai.example.com/login" in sent[0]["body"]
    assert "YOUR_API_KEY" in sent[0]["body"]
    assert "/v1" in sent[0]["body"]
    assert "Enabled sources:" in sent[0]["body"] or "chat" in sent[0]["body"]
    assert "Hello from admin" in sent[0]["body"]
    assert "gw_" not in sent[0]["body"]  # no real key
    assert "optional" in sent[0]["body"].lower()


def test_welcome_mail_body_includes_personal_note():
    from app.data.models import WebUser
    from app.web.platform.users import _welcome_mail_body

    u = WebUser(
        id=1,
        username="pending-1",
        email="a@b.com",
        password_hash="x",
        is_platform_admin=False,
        must_change_password=True,
    )
    body = _welcome_mail_body(
        target=u,
        email="a@b.com",
        password="Temp-pass-123A!",
        login_url="https://ai.example.com/login",
        keys_url="https://ai.example.com/keys",
        api_base="https://ai.example.com/v1",
        must_change_password=True,
        personal_note="Bring headphones.",
    )
    assert "Bring headphones." in body
    assert "YOUR_API_KEY" in body
    assert "https://ai.example.com/v1" in body
