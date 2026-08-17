"""Unit tests for integration HTML report helpers (no live backends)."""

from __future__ import annotations

from pathlib import Path

from tests.integration_helpers import (
    extract_html_document,
    image_file_to_data_url,
    resolve_vl_image_path,
    write_chat_landing_html,
    write_compare_landings_index,
    write_model_landing_page,
    write_power_index_html,
)


def test_extract_html_from_fenced_reply():
    raw = "Sure!\n```html\n<!DOCTYPE html><html><body><h1>Hi</h1></body></html>\n```\n"
    out = extract_html_document(raw)
    assert out.lstrip().lower().startswith("<!doctype html")
    assert "<h1>Hi</h1>" in out


def test_write_model_landing_page(tmp_path: Path):
    p = write_model_landing_page(
        tmp_path, "chat_demo_landing.html", "<!DOCTYPE html><html><body>x</body></html>"
    )
    assert "x" in p.read_text(encoding="utf-8")


def test_write_compare_landings_index(tmp_path: Path):
    p = write_compare_landings_index(
        tmp_path,
        entries=[
            {
                "label": "text",
                "model": "m1",
                "duration_ms": 1,
                "watts_avg": 2,
                "watt_hours_est": 0.01,
                "total_tokens": 10,
                "landing_href": "a.html",
                "power_href": "a_power.json",
            }
        ],
    )
    assert "Open generated landing" in p.read_text(encoding="utf-8")


def test_write_chat_landing_html(tmp_path: Path):
    path = write_chat_landing_html(
        tmp_path,
        "chat_demo_report.html",
        model="Qwen3.8-27B-UD-Q8_K_XL",
        kind="text",
        content="note",
        usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        power={
            "http_status": 200,
            "duration_ms": 1234.5,
            "watts_avg": 180.0,
            "watt_hours_est": 0.0617,
            "watts_samples": [170.0, 190.0],
        },
        mode="gateway",
        host="127.0.0.1:9081",
    )
    text = path.read_text(encoding="utf-8")
    assert "Qwen3.8-27B-UD-Q8_K_XL" in text
    assert "LocalAI Gateway" in text
    assert "1.2 s" in text


def test_format_duration_ms():
    from tests.integration_helpers import format_duration_ms

    assert format_duration_ms(None) == "—"
    assert "12.4 s" in format_duration_ms(12400)
    assert "58.8 min" in format_duration_ms(3529724.3)
    assert "1.02 h" in format_duration_ms(3_672_000)


def test_write_power_index_html(tmp_path: Path):
    path = write_power_index_html(
        tmp_path,
        [{"model": "m1", "duration_ms": 100, "watts_avg": 50, "watt_hours_est": 0.001}],
    )
    assert path is not None
    assert "m1" in path.read_text(encoding="utf-8")


def test_vl_fixture_encodes_to_data_url():
    path = resolve_vl_image_path()
    assert path is not None, "tests/fixtures/gic_landing.jpg missing"
    url = image_file_to_data_url(path)
    assert url.startswith("data:image/")
    assert len(url) > 1000


def test_resolve_gpu_power_url_from_chat_source(monkeypatch):
    monkeypatch.setenv("GPU_POWER_URL", "")
    monkeypatch.setenv("CHAT_SOURCE", "192.168.178.25:11535")
    monkeypatch.setenv("EMBED_SOURCE", "")
    monkeypatch.setenv("STT_SOURCE", "")
    monkeypatch.setenv("TTS_SOURCE", "")
    from tests.integration_helpers import resolve_gpu_power_url

    assert resolve_gpu_power_url() == "http://192.168.178.25:9105/power"


def test_resolve_gpu_power_url_from_host_skips_gateway(monkeypatch):
    monkeypatch.setenv("GPU_POWER_URL", "")
    monkeypatch.setenv("CHAT_SOURCE", "192.168.178.25:11535")
    monkeypatch.setenv("GATEWAY_PORT", "9081")
    from tests.integration_helpers import resolve_gpu_power_url

    assert resolve_gpu_power_url("127.0.0.1:9081") == "http://192.168.178.25:9105/power"
    assert resolve_gpu_power_url("192.168.178.25:11537") == "http://192.168.178.25:9105/power"


def test_resolve_gpu_power_url_explicit_override(monkeypatch):
    monkeypatch.setenv("GPU_POWER_URL", "http://elsewhere:9105/power")
    from tests.integration_helpers import resolve_gpu_power_url

    assert resolve_gpu_power_url("192.168.178.25:11535") == "http://elsewhere:9105/power"
