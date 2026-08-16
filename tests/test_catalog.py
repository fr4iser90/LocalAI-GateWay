"""Unit tests for model catalog filtering / disable."""

from __future__ import annotations

from pathlib import Path

from app.data.backends import upsert_source
from app.data.catalog import (
    is_model_globally_enabled,
    models_visible_for_key,
    openai_models_payload,
    set_model_enabled,
)
from app.data.db import hash_api_key
from app.data.models import (
    ApiKey,
    Base,
    CatalogModel,
    ModelAllowlist,
    ServiceGrant,
    make_engine,
    make_session_factory,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "c.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_disabled_model_hidden_and_blocked(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    a = CatalogModel(source_name="chat", kind="chat", model_id="alpha", enabled=True)
    b = CatalogModel(source_name="chat", kind="chat", model_id="beta", enabled=True)
    db.add_all([a, b])
    key = ApiKey(
        label="t",
        key_hash=hash_api_key("gw_test"),
        key_prefix="gw_test",
        is_active=True,
    )
    db.add(key)
    db.flush()
    db.add(ServiceGrant(api_key_id=key.id, service="chat"))
    db.commit()

    visible = models_visible_for_key(db, key)
    assert {m.model_id for m in visible} == {"alpha", "beta"}

    set_model_enabled(db, b.id, False)
    db.commit()
    visible = models_visible_for_key(db, key)
    assert {m.model_id for m in visible} == {"alpha"}
    assert is_model_globally_enabled(db, "chat", "beta") is False
    assert is_model_globally_enabled(db, "chat", "alpha") is True
    assert is_model_globally_enabled(db, "chat", "unknown") is True

    payload = openai_models_payload(visible)
    assert [x["id"] for x in payload["data"]] == ["alpha"]


def test_allowlist_intersects_catalog(tmp_path: Path):
    db = _session(tmp_path)
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="alpha", enabled=True))
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="beta", enabled=True))
    key = ApiKey(
        label="t",
        key_hash=hash_api_key("gw_test2"),
        key_prefix="gw_t",
        is_active=True,
    )
    db.add(key)
    db.flush()
    db.add(ServiceGrant(api_key_id=key.id, service="chat"))
    db.add(ModelAllowlist(api_key_id=key.id, service="chat", model_name="beta"))
    db.commit()

    visible = models_visible_for_key(db, key)
    assert [m.model_id for m in visible] == ["beta"]
