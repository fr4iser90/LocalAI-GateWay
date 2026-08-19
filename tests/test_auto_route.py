"""Gateway auto / auto-quality / auto-long aliases."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.auto_route import (
    auto_alias_list_entries,
    auto_alias_slot,
    resolve_auto_model,
    rewrite_auto_model,
    target_for_slot,
)


def test_alias_slots():
    assert auto_alias_slot("auto") == "default"
    assert auto_alias_slot("AUTO-QUALITY") == "quality"
    assert auto_alias_slot("auto-long") == "long"
    assert auto_alias_slot("Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL") is None
    assert auto_alias_slot(None) is None


def test_empty_settings_disable_auto_rewrite():
    auth = SimpleNamespace(auto_model_default="", auto_model_quality="", auto_model_long="")
    assert resolve_auto_model(auth, "auto") is None
    assert resolve_auto_model(auth, "auto-quality") is None
    assert resolve_auto_model(auth, "auto-long") is None
    assert target_for_slot(None, "default") is None


def test_settings_override_targets():
    auth = SimpleNamespace(
        auto_model_default="daily-id",
        auto_model_quality="qual-id",
        auto_model_long="long-id",
    )
    assert resolve_auto_model(auth, "auto") == "daily-id"
    assert resolve_auto_model(auth, "auto-quality") == "qual-id"
    assert resolve_auto_model(auth, "auto-long") == "long-id"


def test_rewrite_auto_json_body():
    body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}).encode()
    auth = SimpleNamespace(auto_model_default="moe-mtp", auto_model_quality="", auto_model_long="")
    new_body, target = rewrite_auto_model(body, asked="auto", auth=auth)
    assert target == "moe-mtp"
    assert json.loads(new_body)["model"] == "moe-mtp"
    assert json.loads(new_body)["messages"][0]["content"] == "hi"


def test_rewrite_skips_real_model_id():
    body = json.dumps({"model": "already-real"}).encode()
    auth = SimpleNamespace(auto_model_default="moe-mtp", auto_model_quality="", auto_model_long="")
    new_body, target = rewrite_auto_model(body, asked="already-real", auth=auth)
    assert target is None
    assert new_body == body


def test_v1_models_lists_aliases_first():
    auth = SimpleNamespace(auto_model_default="", auto_model_quality="", auto_model_long="")
    ids = [e["id"] for e in auto_alias_list_entries(auth)]
    assert ids == []
