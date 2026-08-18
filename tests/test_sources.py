"""Unit tests for dynamic BackendSource seeding / defaults."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.data.backends import (
    default_source_for_kind,
    list_sources,
    rename_source,
    seed_backends_from_env,
    upsert_source,
)
from app.data.models import (
    AdminUser,
    Base,
    BackendConfig,
    CatalogModel,
    ServiceGrant,
    make_engine,
    make_session_factory,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "t.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_seed_chat_and_chat2(tmp_path: Path):
    db = _session(tmp_path)
    settings = Settings(
        chat_source="10.0.0.1:1",
        chat2_source="10.0.0.1:2",
        embed_source="10.0.0.1:3",
    )
    seed_backends_from_env(db, settings)
    db.commit()
    names = {s.name: s for s in list_sources(db)}
    assert names["chat"].address == "10.0.0.1:1"
    assert names["chat"].is_default
    assert names["chat"].kind == "chat"
    assert names["chat2"].address == "10.0.0.1:2"
    assert names["chat2"].kind == "chat"
    assert not names["chat2"].is_default
    assert default_source_for_kind(db, "chat").name == "chat"
    assert default_source_for_kind(db, "embed").name == "embed"


def test_seed_skips_when_sources_already_exist(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="gpu-main", kind="chat", address="10.0.0.9:1", is_default=True)
    db.commit()
    seed_backends_from_env(
        db,
        Settings(chat_source="10.0.0.1:1", embed_source="10.0.0.1:3"),
    )
    db.commit()
    names = {s.name for s in list_sources(db)}
    assert names == {"gpu-main"}


def test_migrate_legacy_backend_config(tmp_path: Path):
    db = _session(tmp_path)
    db.add(BackendConfig(chat="1.1.1.1:1", chat2="1.1.1.1:2", embed="1.1.1.1:3"))
    db.commit()
    seed_backends_from_env(db, Settings())
    db.commit()
    names = {s.name: s for s in list_sources(db)}
    assert names["chat"].address == "1.1.1.1:1"
    assert names["chat2"].address == "1.1.1.1:2"
    assert names["embed"].address == "1.1.1.1:3"


def test_upsert_third_chat(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="a:1", is_default=True)
    upsert_source(db, name="ollama", kind="chat", address="a:2", is_default=False)
    upsert_source(db, name="gpu2", kind="chat", address="a:3", is_default=False)
    db.commit()
    assert len([s for s in list_sources(db) if s.kind == "chat"]) == 3
    assert default_source_for_kind(db, "chat").name == "chat"


def test_rename_source_rewrites_catalog_and_grants(tmp_path: Path):
    db = _session(tmp_path)
    src = upsert_source(db, name="chat", kind="chat", address="a:1", is_default=True)
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="m", enabled=True))
    user = AdminUser(
        username="bob",
        password_hash="x",
        is_platform_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(ServiceGrant(user_id=user.id, service="chat"))
    db.commit()

    err = rename_source(db, src, "gpu-main")
    db.commit()
    assert err is None
    db.refresh(src)
    assert src.name == "gpu-main"
    assert src.kind == "chat"
    assert db.query(CatalogModel).filter(CatalogModel.source_name == "gpu-main").count() == 1
    assert db.query(ServiceGrant).filter(ServiceGrant.service == "gpu-main").count() == 1
    assert db.query(CatalogModel).filter(CatalogModel.source_name == "chat").count() == 0


def test_apply_source_row_edits_swap_names(tmp_path: Path):
    db = _session(tmp_path)
    a = upsert_source(db, name="chat", kind="chat", address="a:1", is_default=True)
    b = upsert_source(db, name="chat2", kind="chat", address="a:2", is_default=False)
    db.commit()
    from app.data.backends import apply_source_row_edits

    err = apply_source_row_edits(
        db, [(a, "chat2", "a:1"), (b, "chat", "a:2")]
    )
    db.commit()
    assert err is None
    db.refresh(a)
    db.refresh(b)
    assert a.name == "chat2"
    assert b.name == "chat"


def test_hardware_label_optional_and_trimmed(tmp_path: Path):
    from app.data.backends import hardware_labels, normalize_hardware_label

    assert normalize_hardware_label("  Strix   Halo  ") == "Strix Halo"
    assert len(normalize_hardware_label("x" * 80)) == 64

    db = _session(tmp_path)
    upsert_source(db, name="test", kind="chat", address="a:1", hardware="  Jetson Orin Super ")
    db.commit()
    labels = hardware_labels(db)
    assert labels == {"test": "Jetson Orin Super"}
    upsert_source(db, name="test", kind="chat", address="a:1")
    db.commit()
    assert hardware_labels(db) == {"test": "Jetson Orin Super"}
    upsert_source(db, name="test", kind="chat", address="a:1", hardware="")
    db.commit()
    assert hardware_labels(db) == {}
