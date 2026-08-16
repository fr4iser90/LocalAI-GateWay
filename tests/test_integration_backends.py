"""Live API checks against LAN sources (INTEGRATION=1).

Artifacts land in ``output/integration/<timestamp>/`` (and ``latest`` symlink).

Run::

  set -a && source .env && set +a
  INTEGRATION=1 pytest -m integration -q
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration_helpers import (
    base_url,
    env_source,
    first_model_id,
    integration_output_dir,
    require_integration,
    require_source,
    save_bytes,
    save_json,
    save_text,
    synthesize_speech,
    transcribe_audio,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def out_dir():
    require_integration()
    run_dir = integration_output_dir()
    save_json(
        run_dir,
        "00_meta.json",
        {
            "CHAT_SOURCE": env_source("CHAT"),
            "EMBED_SOURCE": env_source("EMBED"),
            "STT_SOURCE": env_source("STT"),
            "TTS_SOURCE": env_source("TTS"),
            "CHAT2_SOURCE": env_source("CHAT2"),
        },
    )
    return run_dir


@pytest.mark.parametrize(
    "env_name,paths",
    [
        ("CHAT", ["/health", "/v1/models"]),
        ("CHAT2", ["/health", "/v1/models", "/api/tags"]),
        ("EMBED", ["/health", "/v1/models"]),
        ("STT", ["/health", "/"]),
        ("TTS", ["/health", "/"]),
    ],
)
def test_source_reachable(env_name: str, paths: list[str]):
    backend = require_source(env_name)
    last_err = None
    with httpx.Client(timeout=5.0, follow_redirects=True) as client:
        for path in paths:
            try:
                resp = client.get(f"{base_url(backend)}{path}")
                if resp.status_code < 500:
                    return
                last_err = f"{path} -> {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
    if env_name == "CHAT2":
        pytest.skip(f"CHAT2 unreachable: {last_err}")
    pytest.fail(f"{env_name} source {backend} unreachable: {last_err}")


def test_chat_completions_returns_message(out_dir):
    backend = require_source("CHAT")
    with httpx.Client(timeout=120.0) as client:
        model = first_model_id(client, backend) or "default"
        resp = client.post(
            f"{base_url(backend)}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                "max_tokens": 32,
                "temperature": 0,
            },
        )
    assert resp.status_code == 200, resp.text[:400]
    data = resp.json()
    save_json(out_dir, "chat_completion.json", data)
    save_text(out_dir, "chat_completion.txt", (data["choices"][0]["message"].get("content") or ""))
    assert "choices" in data and data["choices"], data
    msg = data["choices"][0].get("message") or {}
    assert msg.get("role") == "assistant"
    assert "content" in msg


def test_embeddings_returns_vector(out_dir):
    backend = require_source("EMBED")
    with httpx.Client(timeout=120.0) as client:
        model = first_model_id(client, backend) or "default"
        resp = client.post(
            f"{base_url(backend)}/v1/embeddings",
            json={"model": model, "input": "hello gateway"},
        )
    assert resp.status_code == 200, resp.text[:400]
    data = resp.json()
    rows = data.get("data") or []
    assert rows, data
    emb = rows[0].get("embedding")
    assert isinstance(emb, list) and len(emb) > 8
    # Full vector can be huge — save summary + first dims
    save_json(
        out_dir,
        "embeddings.json",
        {
            "model": data.get("model"),
            "dims": len(emb),
            "embedding_head": emb[:16],
            "usage": data.get("usage"),
        },
    )


def test_tts_returns_wav_audio(out_dir):
    backend = require_source("TTS")
    phrase = "Hallo Gateway Test eins zwei drei"
    with httpx.Client(timeout=120.0) as client:
        audio, path_used = synthesize_speech(client, backend, phrase)
    assert len(audio) > 1000
    assert audio[:4] == b"RIFF"
    assert audio[8:12] == b"WAVE"
    save_bytes(out_dir, "tts.wav", audio)
    save_json(out_dir, "tts_meta.json", {"path": path_used, "bytes": len(audio), "phrase": phrase})


def test_stt_transcribes_tts_audio_roundtrip(out_dir):
    """TTS → WAV → STT; expect something like 'gateway' / 'test' in the text."""
    require_integration()
    tts = env_source("TTS")
    stt = env_source("STT")
    if not tts:
        pytest.skip("TTS_SOURCE not set")
    if not stt:
        pytest.skip("STT_SOURCE not set")

    phrase = "Hallo Gateway Test eins zwei drei"
    with httpx.Client(timeout=180.0) as client:
        audio, tts_path = synthesize_speech(client, tts, phrase)
        assert audio[:4] == b"RIFF"
        text, stt_path, raw = transcribe_audio(client, stt, audio)

    save_bytes(out_dir, "roundtrip_tts.wav", audio)
    if isinstance(raw, dict):
        save_json(out_dir, "roundtrip_stt.json", raw)
    else:
        save_text(out_dir, "roundtrip_stt.txt", str(raw))
    save_text(out_dir, "roundtrip_transcript.txt", text)
    save_json(
        out_dir,
        "roundtrip_meta.json",
        {
            "phrase": phrase,
            "tts_path": tts_path,
            "stt_path": stt_path,
            "transcript": text,
            "audio_bytes": len(audio),
        },
    )

    lowered = text.lower()
    assert any(
        token in lowered for token in ("gateway", "test", "hallo", "hello", "eins", "zwei")
    ), f"unexpected transcript: {text!r}"


def test_openai_client_paths_map_concept(out_dir):
    """Document gateway mapping: clients use /v1/… ; backends may use native paths."""
    from app.config import map_upstream_path

    mapping = {
        "tts_openai_to_piper": map_upstream_path(
            "/v1/audio/speech", kind="tts", api_style="piper"
        ),
        "stt_openai_to_whisper": map_upstream_path(
            "/v1/audio/transcriptions", kind="stt", api_style="whisper_cpp"
        ),
        "chat_unchanged": map_upstream_path(
            "/v1/chat/completions", kind="chat", api_style="openai"
        ),
    }
    save_json(out_dir, "path_mapping.json", mapping)
    assert mapping["tts_openai_to_piper"] == "/audio/speech"
    assert mapping["stt_openai_to_whisper"] == "/inference"
    assert mapping["chat_unchanged"] == "/v1/chat/completions"


def test_chat2_optional_when_up():
    backend = env_source("CHAT2")
    require_integration()
    if not backend:
        pytest.skip("CHAT2_SOURCE not set")
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{base_url(backend)}/v1/models")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"CHAT2 unreachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"CHAT2 unhealthy: {resp.status_code}")
    assert resp.status_code < 500
