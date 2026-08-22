"""Public model aliases."""

from __future__ import annotations

import json
from pathlib import Path

from app.data.backends import resolve_source_for_kind, upsert_source
from app.data.models import Base, CatalogModel, make_engine, make_session_factory
from app.model_aliases import (
    alias_list_entries,
    apply_client_model_rewrites,
    resolve_alias,
    upsert_alias,
    validate_alias_id,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "alias.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_validate_alias_id():
    assert validate_alias_id("qwen3.6") == "qwen3.6"
    assert validate_alias_id("AUTO") is None
    assert validate_alias_id("Bad Name") is None


def test_resolve_and_rewrite(tmp_path: Path):
    db = _session(tmp_path)
    upsert_alias(
        db,
        alias_id="qwen3.6",
        target_model_id="Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL-VL",
        preferred_source="chat",
        description="Daily VL",
    )
    db.commit()
    got = resolve_alias(db, "qwen3.6")
    assert got is not None
    assert got.target_model_id.endswith("-VL")
    assert got.preferred_source == "chat"

    body = json.dumps({"model": "qwen3.6", "messages": []}).encode()
    new_body, rewritten, pref = apply_client_model_rewrites(
        db, body, asked="qwen3.6", auth=None
    )
    assert rewritten.endswith("-VL")
    assert pref == "chat"
    assert json.loads(new_body)["model"].endswith("-VL")


def test_alias_list_entries_include_architecture(tmp_path: Path):
    db = _session(tmp_path)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="Qwen-VL",
            enabled=True,
            modalities_in="text,image",
            modalities_out="text",
            tags="vision",
            ctx_size=131072,
        )
    )
    upsert_alias(db, alias_id="qwen3.6", target_model_id="Qwen-VL", show_backend=True)
    db.commit()
    entries = alias_list_entries(db)
    assert entries[0]["id"] == "qwen3.6"
    assert "Qwen-VL" in entries[0]["description"]
    assert entries[0]["architecture"]["input_modalities"] == ["text", "image"]
    assert "vision" in entries[0]["tags"]


def test_alias_preferred_source_routes_to_named_upstream(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:11535")
    upsert_source(db, name="chat2", kind="chat", address="127.0.0.1:11537")
    mid = "shared-model"
    db.add(CatalogModel(source_name="chat", kind="chat", model_id=mid, enabled=True))
    db.add(CatalogModel(source_name="chat2", kind="chat", model_id=mid, enabled=True))
    upsert_alias(
        db,
        alias_id="qwen3.6",
        target_model_id=mid,
        preferred_source="chat2",
    )
    db.commit()
    resolved = resolve_alias(db, "qwen3.6")
    assert resolved is not None
    assert resolved.preferred_source == "chat2"
    pick = resolve_source_for_kind(
        db,
        "chat",
        model=mid,
        routing_strategy="name",
        preferred_source=resolved.preferred_source,
    )
    assert pick is not None
    assert pick.name == "chat2"
