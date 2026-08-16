"""Unit tests for dynamic BackendSource seeding / defaults."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.data.backends import (
    default_source_for_kind,
    list_sources,
    seed_backends_from_env,
    upsert_source,
)
from app.data.models import Base, BackendConfig, make_engine, make_session_factory


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
