"""Tests for model → source routing on default /v1 paths."""

from __future__ import annotations

from pathlib import Path

from app.data.backends import (
    model_match_score,
    resolve_source_for_kind,
    upsert_source,
)
from app.data.models import Base, CatalogModel, make_engine, make_session_factory


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "m.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def _cat(db, source: str, model_id: str, *, enabled: bool = True):
    db.add(
        CatalogModel(
            source_name=source,
            kind="chat",
            model_id=model_id,
            enabled=enabled,
        )
    )


def test_model_match_score():
    assert model_match_score("llama3.2", "llama3.2") == 1000 + len("llama3.2")
    assert model_match_score("llama3.2", "llama3.2:latest") is not None
    assert model_match_score("qwen*", "qwen2.5-7b") is not None
    assert model_match_score("qwen*", "llama") is None
    assert model_match_score("a", "b") is None


def test_resolve_by_catalog_model(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="a:1", is_default=True)
    upsert_source(db, name="ollama", kind="chat", address="a:2", is_default=False)
    upsert_source(db, name="gpu2", kind="chat", address="a:3", is_default=False)
    _cat(db, "ollama", "llama3.2")
    _cat(db, "ollama", "qwen2.5")
    _cat(db, "gpu2", "mistral")
    db.commit()

    assert resolve_source_for_kind(db, "chat", model="llama3.2:latest").name == "ollama"
    assert resolve_source_for_kind(db, "chat", model="qwen2.5").name == "ollama"
    assert resolve_source_for_kind(db, "chat", model="mistral").name == "gpu2"
    assert resolve_source_for_kind(db, "chat", model="unknown-model").name == "chat"
    assert resolve_source_for_kind(db, "chat", model=None).name == "chat"


def test_disabled_catalog_not_routed(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="a:1", is_default=True)
    upsert_source(db, name="other", kind="chat", address="a:2", is_default=False)
    _cat(db, "other", "llama3.2", enabled=False)
    db.commit()
    assert resolve_source_for_kind(db, "chat", model="llama3.2").name == "chat"


def test_isolated_skipped_in_merge(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="a:1", is_default=True)
    upsert_source(
        db,
        name="secret",
        kind="chat",
        address="a:2",
        is_default=False,
        isolated=True,
    )
    _cat(db, "secret", "llama3.2")
    db.commit()
    assert resolve_source_for_kind(db, "chat", model="llama3.2").name == "chat"


def test_tts_catalog_merge(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="tts", kind="tts", address="a:1", is_default=True)
    upsert_source(db, name="piper2", kind="tts", address="a:2", is_default=False)
    db.add(
        CatalogModel(
            source_name="piper2", kind="tts", model_id="tts-1-hd", enabled=True
        )
    )
    db.commit()
    assert resolve_source_for_kind(db, "tts", model="tts-1-hd").name == "piper2"
    assert resolve_source_for_kind(db, "tts", model="tts-1").name == "tts"
