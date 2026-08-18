"""Load-aware + grant-aware model routing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.data.backends import resolve_source_for_kind, upsert_source
from app.data.db import hash_api_key
from app.data.models import (
    AdminUser,
    ApiKey,
    Base,
    CatalogModel,
    ServiceGrant,
    make_engine,
    make_session_factory,
)
from app.data.source_load import SourceLoadSnapshot, load_cache
from app.routing import allowed_services_for_key, resolve_routed_source


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "lr.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def _cat(db, source: str, model_id: str):
    db.add(
        CatalogModel(
            source_name=source,
            kind="chat",
            model_id=model_id,
            enabled=True,
        )
    )


def _snap(state: str, *, idle: int | None = None, loaded: bool | None = None):
    return SourceLoadSnapshot(
        state=state,
        slots_idle=idle,
        model_loaded=loaded,
        probed_at=1.0,
    )


def test_load_aware_picks_least_busy(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="srv1", kind="chat", address="h1:1", is_default=True)
    upsert_source(db, name="srv2", kind="chat", address="h2:1", is_default=False)
    _cat(db, "srv1", "jarvis")
    _cat(db, "srv2", "jarvis")
    db.commit()

    def fake_snapshot(src, *, kind, model=None):
        if src.name == "srv1":
            return _snap("busy", idle=0)
        return _snap("ok", idle=3)

    with patch.object(load_cache, "snapshot_for", side_effect=fake_snapshot):
        picked = resolve_source_for_kind(
            db, "chat", model="jarvis", load_aware=True
        )
    assert picked is not None
    assert picked.name == "srv2"


def test_load_aware_prefers_model_loaded_on_ollama(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="a", kind="chat", address="h1:1", is_default=True)
    upsert_source(db, name="b", kind="chat", address="h2:1", is_default=False)
    _cat(db, "a", "jarvis")
    _cat(db, "b", "jarvis")
    db.commit()

    def fake_snapshot(src, *, kind, model=None):
        if src.name == "a":
            return _snap("ok", idle=2, loaded=True)
        return _snap("ok", idle=4, loaded=False)

    with patch.object(load_cache, "snapshot_for", side_effect=fake_snapshot):
        picked = resolve_source_for_kind(
            db, "chat", model="jarvis", load_aware=True
        )
    assert picked.name == "a"


def test_grant_aware_limits_candidates(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="gpu", kind="chat", address="h1:1", is_default=True)
    upsert_source(db, name="nano", kind="chat", address="h2:1", is_default=False)
    _cat(db, "gpu", "jarvis")
    _cat(db, "nano", "jarvis")
    user = AdminUser(
        username="friend",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(ServiceGrant(user_id=user.id, service="nano"))
    raw = "gw_route_test_key"
    db.add(
        ApiKey(
            label="k",
            key_hash=hash_api_key(raw),
            key_prefix="gw_rout",
            owner_user_id=user.id,
            is_active=True,
        )
    )
    db.commit()

    assert allowed_services_for_key(db, raw) == {"nano"}

    def fake_snapshot(src, *, kind, model=None):
        if src.name == "gpu":
            return _snap("ok", idle=99)
        return _snap("ok", idle=1)

    with patch.object(load_cache, "snapshot_for", side_effect=fake_snapshot):
        picked = resolve_routed_source(
            db, "chat", model="jarvis", raw_key=raw, load_aware=True
        )
    assert picked is not None
    assert picked.name == "nano"


def test_load_aware_off_ties_break_by_name(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="h1:1")
    upsert_source(db, name="fast", kind="chat", address="h2:1")
    _cat(db, "chat", "jarvis")
    _cat(db, "fast", "jarvis")
    db.commit()

    picked = resolve_source_for_kind(
        db, "chat", model="jarvis", load_aware=False
    )
    assert picked is not None
    assert picked.name == "chat"
