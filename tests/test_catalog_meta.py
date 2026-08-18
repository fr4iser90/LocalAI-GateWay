"""Catalog metadata: tags, notes, docs links."""

from __future__ import annotations

from pathlib import Path

from app.data.catalog import (
    format_tags,
    openai_models_payload,
    parse_tags,
    suggest_docs_url,
    update_catalog_meta,
)
from app.data.models import Base, CatalogModel, make_engine, make_session_factory


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "meta.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_parse_and_format_tags():
    assert parse_tags("Tools, vision; CODE  ") == ["tools", "vision", "code"]
    assert format_tags("Fast, tools, tools") == "fast,tools"


def test_suggest_docs_url():
    assert suggest_docs_url("meta-llama/Llama-3.2-3B") == (
        "https://huggingface.co/meta-llama/Llama-3.2-3B"
    )
    assert suggest_docs_url("meta-llama/Llama-3.2-3B:Q4") == (
        "https://huggingface.co/meta-llama/Llama-3.2-3B"
    )
    assert suggest_docs_url("llama3.2:latest") == ""
    assert suggest_docs_url("https://example.com/x") == ""


def test_infer_tags():
    from app.data.catalog import infer_tags

    assert "vision" in infer_tags("Qwen3.6-35B-A3B-UD-Q4_K_M-VL", "chat")
    assert "embed" in infer_tags("bge-m3-Q4_K_M", "embed")
    assert "code" in infer_tags("GemCod-R-Sapphire-270M", "chat")
    assert "stt" in infer_tags("stt", "stt")


def test_update_catalog_meta_and_payload_description(tmp_path: Path):
    db = _session(tmp_path)
    row = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="alpha",
        enabled=True,
    )
    db.add(row)
    db.commit()

    update_catalog_meta(
        db,
        row.id,
        tags="tools, code",
        short_note="Solid default",
        docs_url="https://huggingface.co/org/alpha",
    )
    db.commit()
    db.refresh(row)
    assert row.tags == "tools,code"
    assert row.short_note == "Solid default"
    assert row.docs_url.startswith("https://")

    update_catalog_meta(db, row.id, docs_url="not-a-url")
    db.commit()
    db.refresh(row)
    assert row.docs_url == ""

    payload = openai_models_payload([row])
    assert payload["data"][0]["description"] == "Solid default"


def test_parse_openai_model_item_loaded_and_unloaded():
    from app.data.catalog import parse_openai_model_item

    loaded = parse_openai_model_item(
        {
            "id": "GemCod",
            "status": {
                "value": "loaded",
                "args": ["--ctx-size", "8192", "--model", "/x.gguf"],
            },
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
            "meta": {
                "n_ctx": 4096,
                "n_ctx_train": 32768,
                "n_embd": 640,
                "n_params": 268098816,
                "size": 536309504,
            },
        }
    )
    assert loaded is not None
    assert loaded.model_id == "GemCod"
    assert loaded.upstream_status == "loaded"
    assert loaded.ctx_size == 8192
    assert loaded.has_meta is True
    assert loaded.n_ctx == 4096
    assert loaded.n_ctx_train == 32768
    assert loaded.n_embd == 640
    assert loaded.n_params == 268098816
    assert loaded.model_size == 536309504
    assert loaded.modalities_in == "text"

    unloaded = parse_openai_model_item(
        {
            "id": "big",
            "status": {
                "value": "unloaded",
                "args": ["--ctx-size", "131072"],
            },
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        }
    )
    assert unloaded is not None
    assert unloaded.upstream_status == "unloaded"
    assert unloaded.ctx_size == 131072
    assert unloaded.has_meta is False
    assert unloaded.n_embd is None


def test_last_known_meta_retained_on_unload(tmp_path: Path):
    from app.data.catalog import apply_discovered_fields, parse_openai_model_item
    from app.data.models import utcnow

    db = _session(tmp_path)
    row = CatalogModel(source_name="chat", kind="chat", model_id="GemCod", enabled=True)
    db.add(row)
    db.commit()

    loaded = parse_openai_model_item(
        {
            "id": "GemCod",
            "status": {"value": "loaded", "args": ["--ctx-size", "8192"]},
            "meta": {"n_embd": 640, "n_ctx": 4096, "n_params": 100},
        }
    )
    assert loaded is not None
    apply_discovered_fields(row, loaded, now=utcnow())
    db.commit()
    db.refresh(row)
    assert row.n_embd == 640
    assert row.n_ctx == 4096
    assert row.ctx_size == 8192
    assert row.upstream_status == "loaded"

    unloaded = parse_openai_model_item(
        {
            "id": "GemCod",
            "status": {"value": "unloaded", "args": ["--ctx-size", "16384"]},
        }
    )
    assert unloaded is not None
    apply_discovered_fields(row, unloaded, now=utcnow())
    db.commit()
    db.refresh(row)
    assert row.upstream_status == "unloaded"
    assert row.ctx_size == 16384  # args always refresh
    assert row.n_embd == 640  # last known retained
    assert row.n_ctx == 4096

    payload = openai_models_payload([row])
    assert payload["data"][0]["n_embd"] == 640
    assert payload["data"][0]["ctx_size"] == 16384
    assert payload["data"][0]["context_length"] == 16384
    assert payload["data"][0]["status"] == "unloaded"


def test_context_length_from_ctx_size_only(tmp_path: Path):
    db = _session(tmp_path)
    row = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="big-128k",
        enabled=True,
        ctx_size=131072,
        n_ctx_train=32768,
    )
    db.add(row)
    db.commit()
    payload = openai_models_payload([row])
    assert payload["data"][0]["context_length"] == 131072


def test_context_length_falls_back_to_n_ctx_train(tmp_path: Path):
    db = _session(tmp_path)
    row = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="m",
        enabled=True,
        n_ctx_train=65536,
    )
    db.add(row)
    db.commit()
    payload = openai_models_payload([row])
    assert payload["data"][0]["context_length"] == 65536
    assert "ctx_size" not in payload["data"][0]


def test_fetch_piper_voices_returns_discovered():
    from app.data.catalog import _fetch_piper_voices
    from unittest.mock import MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "ok": True,
        "service": "piper-tts",
        "voices": ["de_DE-thorsten-high"],
    }
    with patch("app.data.catalog.httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        got = _fetch_piper_voices("http://tts:9001")
        assert [d.model_id for d in got] == ["de_DE-thorsten-high"]
