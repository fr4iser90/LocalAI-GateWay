"""Unit tests for optional VL auto-routing helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.data.models import Base, CatalogModel, make_engine, make_session_factory
from app.vision_route import (
    mint_forward_ticket,
    model_is_vision,
    parse_forward_ticket,
    request_needs_vision,
    resolve_vl_model,
    rewrite_json_model,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "vl.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_request_needs_vision_image_url():
    body = json.dumps(
        {
            "model": "qwen",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,xx"},
                        },
                    ],
                }
            ],
        }
    ).encode()
    assert request_needs_vision(body, "application/json") is True


def test_request_needs_vision_text_only():
    body = json.dumps(
        {
            "model": "qwen",
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()
    assert request_needs_vision(body, "application/json") is False


def test_resolve_vl_sibling(tmp_path: Path):
    db = _session(tmp_path)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="Qwen2.5-7B-Instruct",
            enabled=True,
            tags="",
        )
    )
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="Qwen2.5-VL-7B-Instruct",
            enabled=True,
            tags="vision",
        )
    )
    db.commit()
    assert model_is_vision(db, "chat", "Qwen2.5-VL-7B-Instruct") is True
    assert model_is_vision(db, "chat", "Qwen2.5-7B-Instruct") is False
    assert (
        resolve_vl_model(db, "chat", "Qwen2.5-7B-Instruct")
        == "Qwen2.5-VL-7B-Instruct"
    )
    assert resolve_vl_model(db, "chat", "Qwen2.5-VL-7B-Instruct") is None


def test_resolve_vl_suffix(tmp_path: Path):
    db = _session(tmp_path)
    db.add(
        CatalogModel(
            source_name="chat", kind="chat", model_id="Foo-Q4_K_M", enabled=True
        )
    )
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="Foo-VL-Q4_K_M",
            enabled=True,
            tags="vision",
        )
    )
    db.commit()
    assert resolve_vl_model(db, "chat", "Foo-Q4_K_M") == "Foo-VL-Q4_K_M"


def test_rewrite_json_model():
    raw = b'{"model":"a","messages":[]}'
    out = rewrite_json_model(raw, "a-VL")
    assert json.loads(out)["model"] == "a-VL"


def test_group_models_vl_pairs_suffix(tmp_path: Path):
    db = _session(tmp_path)
    a = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="Qwen3.6-35B-A3B-UD-Q4_K_M",
        enabled=True,
        tags="tools",
    )
    b = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="Qwen3.6-35B-A3B-UD-Q4_K_M-VL",
        enabled=True,
        tags="vision,tools",
    )
    c = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="GemCod-R-Sapphire-270M",
        enabled=True,
        tags="code",
    )
    db.add_all([a, b, c])
    db.commit()
    from app.vision_route import group_models_vl_pairs

    pairs = group_models_vl_pairs([a, b, c])
    assert len(pairs) == 2
    assert pairs[0][0].model_id == "Qwen3.6-35B-A3B-UD-Q4_K_M"
    assert pairs[0][1].model_id == "Qwen3.6-35B-A3B-UD-Q4_K_M-VL"
    assert pairs[1][0].model_id == "GemCod-R-Sapphire-270M"
    assert pairs[1][1] is None


def test_forward_ticket_roundtrip():
    t = mint_forward_ticket(
        secret="s",
        service="chat",
        backend="10.0.0.1:1",
        rewrite_uri="/v1/chat/completions",
        rewrite_model="x-VL",
    )
    p = parse_forward_ticket(t, "s")
    assert p is not None
    assert p["service"] == "chat"
    assert p["model"] == "x-VL"
    assert parse_forward_ticket(t, "wrong") is None
