"""Unit tests for key/team model allowlist checkbox parsing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.web.shared import (
    _catalog_for_allowlist,
    _collect_models_from_form,
    _parse_model_checks,
    _parse_services,
    _sync_key_models,
)
from app.data.backends import upsert_source
from app.data.db import hash_api_key
from app.data.models import (
    ApiKey,
    Base,
    CatalogModel,
    ModelAllowlist,
    make_engine,
    make_session_factory,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "a.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_parse_model_checks():
    assert _parse_model_checks(["chat:alpha", "embed:nomic", "bad", ":x", "y:"]) == [
        ("chat", "alpha"),
        ("embed", "nomic"),
    ]


def test_parse_services_filters_unknown():
    assert _parse_services(["chat", "nope", "embed"], ["chat", "embed"]) == [
        "chat",
        "embed",
    ]


def test_collect_models_from_checkboxes_only(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    db.commit()
    form = SimpleNamespace(
        getlist=lambda name: ["chat:alpha", "chat:beta"] if name == "models" else [],
        get=lambda name, default=None: default,
    )
    pairs = _collect_models_from_form(form, db)
    assert pairs == [("chat", "alpha"), ("chat", "beta")]


def test_sync_key_models_empty_means_unrestricted(tmp_path: Path):
    db = _session(tmp_path)
    key = ApiKey(
        label="t",
        key_hash=hash_api_key("gw_x"),
        key_prefix="gw_x",
        is_active=True,
    )
    db.add(key)
    db.flush()
    db.add(ModelAllowlist(api_key_id=key.id, service="chat", model_name="old"))
    db.commit()

    _sync_key_models(db, key, [])
    db.commit()
    assert db.query(ModelAllowlist).filter(ModelAllowlist.api_key_id == key.id).count() == 0

    _sync_key_models(db, key, [("chat", "alpha")])
    db.commit()
    rows = db.query(ModelAllowlist).filter(ModelAllowlist.api_key_id == key.id).all()
    assert [(r.service, r.model_name) for r in rows] == [("chat", "alpha")]


def test_catalog_for_allowlist_keeps_orphans(tmp_path: Path):
    db = _session(tmp_path)
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="alpha", enabled=True))
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="hidden", enabled=False))
    db.commit()
    groups = dict(_catalog_for_allowlist(db, {"chat:legacy", "chat:hidden"}))
    ids = {m.model_id for m in groups["chat"]}
    assert ids == {"alpha", "hidden", "legacy"}
