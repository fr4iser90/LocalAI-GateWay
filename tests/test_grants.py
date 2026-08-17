"""Access grants / ceilings for users and teams."""

from __future__ import annotations

from pathlib import Path

from app.data.backends import upsert_source
from app.data.db import hash_api_key
from app.data.grants import (
    apply_default_grant,
    clamp_models,
    clamp_services,
    configured_default_sources,
    display_default_models,
    display_default_sources,
    display_enabled_models_for_services,
    effective_models,
    effective_services,
    ceiling_from_team,
    ceiling_from_user,
    save_default_grant,
    sync_user_grants,
    sync_user_models,
    sync_user_models_for_service,
)
from app.data.models import (
    AdminUser,
    ApiKey,
    Base,
    CatalogModel,
    ModelAllowlist,
    ServiceGrant,
    Team,
    make_engine,
    make_session_factory,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "grants.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_user_grant_ceiling_and_key_subset(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    upsert_source(db, name="embed", kind="embed", address="127.0.0.1:2", is_default=True)
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="a", enabled=True))
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="b", enabled=True))
    user = AdminUser(
        username="alice",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    sync_user_grants(db, user, ["chat"])
    sync_user_models(db, user, [("chat", "a")])
    db.commit()
    db.refresh(user)

    ceil = ceiling_from_user(user)
    assert ceil.services == {"chat"}
    assert ceil.models_for("chat") == {"a"}

    key = ApiKey(
        label="k",
        key_hash=hash_api_key("gw_test"),
        key_prefix="gw_t",
        owner_user_id=user.id,
        is_active=True,
    )
    db.add(key)
    db.flush()
    # empty key services → inherit grant
    assert effective_services(db, key) == {"chat"}
    assert effective_models(db, key, "chat") == {"a"}
    assert effective_models(db, key, "embed") == set()

    # key tries to exceed → clamp
    assert clamp_services(["chat", "embed"], ceil, db) == ["chat"]
    assert clamp_models([("chat", "a"), ("chat", "b")], ceil) == [("chat", "a")]


def test_empty_grant_models_means_all(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    user = AdminUser(
        username="bob",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    sync_user_grants(db, user, ["chat"])
    # no model rows → all models for chat
    db.commit()
    db.refresh(user)
    ceil = ceiling_from_user(user)
    assert ceil.models_for("chat") is None

    key = ApiKey(
        label="k",
        key_hash=hash_api_key("gw_bob"),
        key_prefix="gw_b",
        owner_user_id=user.id,
        is_active=True,
    )
    db.add(key)
    db.flush()
    assert effective_models(db, key, "chat") is None


def test_team_grant_is_ceiling(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    team = Team(name="tier-2")
    db.add(team)
    db.flush()
    db.add(ServiceGrant(team_id=team.id, service="chat"))
    db.add(ModelAllowlist(team_id=team.id, service="chat", model_name="only"))
    db.commit()
    db.refresh(team)

    ceil = ceiling_from_team(team)
    assert ceil.services == {"chat"}
    assert ceil.models_for("chat") == {"only"}

    key = ApiKey(
        label="k",
        key_hash=hash_api_key("gw_team"),
        key_prefix="gw_t",
        team_id=team.id,
        is_active=True,
    )
    db.add(key)
    db.flush()
    key.team = team
    assert effective_services(db, key) == {"chat"}
    assert effective_models(db, key, "chat") == {"only"}

    # key subset further restricts
    db.add(ServiceGrant(api_key_id=key.id, service="chat"))
    db.add(ModelAllowlist(api_key_id=key.id, service="chat", model_name="nope"))
    db.commit()
    db.refresh(key)
    key.team = team
    assert effective_models(db, key, "chat") == set()  # nope ∩ {only} = empty


def test_platform_admin_unrestricted(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    admin = AdminUser(
        username="admin",
        password_hash="x",
        is_platform_admin=True,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    ceil = ceiling_from_user(admin)
    assert ceil.unrestricted is True
    key = ApiKey(
        label="k",
        key_hash=hash_api_key("gw_adm"),
        key_prefix="gw_a",
        owner_user_id=admin.id,
        is_active=True,
    )
    db.add(key)
    db.flush()
    key.owner = admin
    assert "chat" in effective_services(db, key)


def test_configured_default_grant_template(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    upsert_source(db, name="chat2", kind="chat", address="127.0.0.1:2", is_default=False)
    db.commit()
    assert configured_default_sources(db) == []
    assert display_default_sources(db) == ["chat", "chat2"]
    assert display_default_models(db) == set()
    save_default_grant(db, [], [])
    db.commit()
    assert configured_default_sources(db) == []
    assert display_default_sources(db) == []
    save_default_grant(db, ["chat", "chat2"], [("chat", "only-this")])
    db.commit()
    assert configured_default_sources(db) == ["chat", "chat2"]
    user = AdminUser(
        username="bob",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    apply_default_grant(db, user)
    db.commit()
    db.refresh(user)
    assert {g.service for g in user.service_grants} == {"chat", "chat2"}
    assert {(m.service, m.model_name) for m in user.model_allowlists} == {
        ("chat", "only-this")
    }

    save_default_grant(db, [], [])
    db.commit()
    user2 = AdminUser(
        username="empty",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user2)
    db.flush()
    apply_default_grant(db, user2)
    db.commit()
    db.refresh(user2)
    assert user2.service_grants == []


def test_catalog_groups_include_stt_tts(tmp_path: Path):
    from app.data.grants import AccessCeiling, catalog_groups_for_ceiling

    db = _session(tmp_path)
    upsert_source(db, name="tts", kind="tts", address="127.0.0.1:1", is_default=True)
    upsert_source(db, name="stt", kind="stt", address="127.0.0.1:2", is_default=True)
    db.add(
        CatalogModel(
            source_name="tts",
            kind="tts",
            model_id="de_DE-thorsten-high",
            enabled=True,
        )
    )
    db.add(
        CatalogModel(source_name="stt", kind="stt", model_id="stt", enabled=True)
    )
    db.commit()
    groups = dict(
        catalog_groups_for_ceiling(db, AccessCeiling(unrestricted=True))
    )
    assert "tts" in groups
    assert [m.model_id for m in groups["tts"]] == ["de_DE-thorsten-high"]
    assert "stt" in groups
    assert [m.model_id for m in groups["stt"]] == ["stt"]


def test_display_default_models_all_checked_when_unset(tmp_path: Path):
    from app.data.grants import configured_default_models

    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    db.add(
        CatalogModel(source_name="chat", kind="chat", model_id="a", enabled=True),
    )
    db.add(
        CatalogModel(source_name="chat", kind="chat", model_id="b", enabled=True),
    )
    db.commit()
    assert display_default_models(db) == {"chat:a", "chat:b"}
    save_default_grant(db, ["chat"], [("chat", "a"), ("chat", "b")])
    db.commit()
    assert configured_default_models(db) == []


def test_display_enabled_models_for_services(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="tts", kind="tts", address="127.0.0.1:1", is_default=True)
    db.add(
        CatalogModel(source_name="tts", kind="tts", model_id="voice-a", enabled=True),
    )
    db.commit()
    assert display_enabled_models_for_services(db, ["tts"]) == {"tts:voice-a"}


def test_normalize_model_allowlist_all_selected_is_empty(tmp_path: Path):
    from app.data.grants import normalize_model_allowlist

    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="a", enabled=True))
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="b", enabled=True))
    db.commit()
    assert normalize_model_allowlist(db, ["chat"], [("chat", "a"), ("chat", "b")]) == []
    assert normalize_model_allowlist(db, ["chat"], [("chat", "a")]) == [("chat", "a")]


def test_sync_user_models_for_service_keeps_other_sources(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    upsert_source(db, name="chat2", kind="chat", address="127.0.0.1:2", is_default=False)
    user = AdminUser(
        username="cara",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    sync_user_grants(db, user, ["chat", "chat2"])
    sync_user_models(db, user, [("chat", "a"), ("chat", "b"), ("chat2", "x")])
    db.commit()

    sync_user_models_for_service(db, user, "chat", ["a"])
    db.commit()
    db.refresh(user)
    assert {(m.service, m.model_name) for m in user.model_allowlists} == {
        ("chat", "a"),
        ("chat2", "x"),
    }

    sync_user_models_for_service(db, user, "chat2", None)
    db.commit()
    db.refresh(user)
    assert {(m.service, m.model_name) for m in user.model_allowlists} == {("chat", "a")}
