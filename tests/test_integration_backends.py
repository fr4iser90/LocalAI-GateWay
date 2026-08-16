"""Live API checks against LAN sources / local gateway (INTEGRATION=1).

Artifacts land in ``output/integration/<timestamp>/`` (and ``latest`` symlink),
including per-call watt samples when ``GPU_POWER_URL`` is set.

Preferred path: hit the **gateway** (``INTEGRATION_GATEWAY`` / ``:9081``) with
``INTEGRATION_API_KEY`` so chat metering (real duration + Wh) is exercised.
Falls back to direct ``*_SOURCE`` hosts if no API key.

Run::

  set -a && source .env && set +a
  export INTEGRATION=1
  export INTEGRATION_API_KEY=gw_…
  pytest -m integration -q
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.integration_helpers import (
    PowerProbe,
    base_url,
    env_source,
    first_model_id,
    gateway_api_key,
    gateway_base,
    gpu_power_url,
    integration_output_dir,
    require_integration,
    require_source,
    sample_gpu_watts,
    save_bytes,
    save_json,
    save_text,
    synthesize_speech,
    transcribe_audio,
)

pytestmark = pytest.mark.integration


def _via_gateway() -> tuple[str, dict[str, str]] | None:
    key = gateway_api_key()
    if not key:
        return None
    return gateway_base(), {"X-Api-Key": key}


@pytest.fixture(scope="module", autouse=True)
def out_dir():
    require_integration()
    run_dir = integration_output_dir()
    gw = _via_gateway()
    snap = sample_gpu_watts()
    save_json(
        run_dir,
        "00_meta.json",
        {
            "CHAT_SOURCE": env_source("CHAT"),
            "EMBED_SOURCE": env_source("EMBED"),
            "STT_SOURCE": env_source("STT"),
            "TTS_SOURCE": env_source("TTS"),
            "CHAT2_SOURCE": env_source("CHAT2"),
            "GPU_POWER_URL": gpu_power_url() or None,
            "gpu_snapshot": snap,
            "gateway": gw[0] if gw else None,
            "via_gateway": bool(gw),
            "note": (
                "Calls use the gateway when INTEGRATION_API_KEY is set "
                "(chat gets real duration/Wh metering). "
                "Each *_power.json has sidecar samples for this test wall time."
            ),
        },
    )
    return run_dir


@pytest.fixture(scope="module")
def power_summary(out_dir):
    rows: list[dict] = []
    yield rows
    save_json(out_dir, "power_summary.json", {"calls": rows})


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


def test_chat_completions_returns_message(out_dir, power_summary):
    gw = _via_gateway()
    if gw:
        host, headers = gw
        mode = "gateway"
    else:
        host = require_source("CHAT")
        headers = {}
        mode = "direct"

    with PowerProbe("chat_completion") as probe:
        with httpx.Client(timeout=180.0) as client:
            model = first_model_id(client, host, headers=headers) or "default"
            resp = client.post(
                f"{base_url(host)}/v1/chat/completions",
                headers=headers,
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
    save_text(
        out_dir,
        "chat_completion.txt",
        (data["choices"][0]["message"].get("content") or ""),
    )
    report = probe.report(mode=mode, host=host, model=model, http_status=resp.status_code)
    save_json(out_dir, "chat_completion_power.json", report)
    power_summary.append(report)
    assert "choices" in data and data["choices"], data
    msg = data["choices"][0].get("message") or {}
    assert msg.get("role") == "assistant"
    assert "content" in msg


def test_embeddings_returns_vector(out_dir, power_summary):
    gw = _via_gateway()
    if gw:
        host, headers = gw
        mode = "gateway"
    else:
        host = require_source("EMBED")
        headers = {}
        mode = "direct"

    with PowerProbe("embeddings") as probe:
        with httpx.Client(timeout=120.0) as client:
            model = first_model_id(client, host, headers=headers) or "default"
            resp = client.post(
                f"{base_url(host)}/v1/embeddings",
                headers=headers,
                json={"model": model, "input": "hello gateway"},
            )
    assert resp.status_code == 200, resp.text[:400]
    data = resp.json()
    rows = data.get("data") or []
    assert rows, data
    emb = rows[0].get("embedding")
    assert isinstance(emb, list) and len(emb) > 8
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
    report = probe.report(mode=mode, host=host, model=model, http_status=resp.status_code)
    save_json(out_dir, "embeddings_power.json", report)
    power_summary.append(report)


def test_tts_returns_wav_audio(out_dir, power_summary):
    gw = _via_gateway()
    phrase = "Hallo Gateway Test eins zwei drei"
    if gw:
        host, headers = gw
        mode = "gateway"
    else:
        host = require_source("TTS")
        headers = {}
        mode = "direct"

    with PowerProbe("tts") as probe:
        with httpx.Client(timeout=120.0) as client:
            audio, path_used = synthesize_speech(client, host, phrase, headers=headers)
    assert len(audio) > 1000
    assert audio[:4] == b"RIFF"
    assert audio[8:12] == b"WAVE"
    save_bytes(out_dir, "tts.wav", audio)
    save_json(
        out_dir,
        "tts_meta.json",
        {"path": path_used, "bytes": len(audio), "phrase": phrase, "mode": mode},
    )
    report = probe.report(mode=mode, host=host, path=path_used, audio_bytes=len(audio))
    save_json(out_dir, "tts_power.json", report)
    power_summary.append(report)


def test_stt_transcribes_tts_audio_roundtrip(out_dir, power_summary):
    """TTS → WAV → STT; expect something like 'gateway' / 'test' in the text."""
    require_integration()
    gw = _via_gateway()
    phrase = "Hallo Gateway Test eins zwei drei"

    if gw:
        host, headers = gw
        mode = "gateway"
        tts_host = stt_host = host
    else:
        tts_host = env_source("TTS")
        stt_host = env_source("STT")
        if not tts_host:
            pytest.skip("TTS_SOURCE not set")
        if not stt_host:
            pytest.skip("STT_SOURCE not set")
        headers = {}
        mode = "direct"

    with PowerProbe("roundtrip_tts_stt") as probe:
        with httpx.Client(timeout=180.0) as client:
            audio, tts_path = synthesize_speech(client, tts_host, phrase, headers=headers)
            assert audio[:4] == b"RIFF"
            text, stt_path, raw = transcribe_audio(
                client, stt_host, audio, headers=headers
            )

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
            "mode": mode,
        },
    )
    report = probe.report(
        mode=mode,
        tts_path=tts_path,
        stt_path=stt_path,
        transcript=text,
        audio_bytes=len(audio),
    )
    save_json(out_dir, "roundtrip_power.json", report)
    power_summary.append(report)

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


def test_gateway_usage_events_include_watts(out_dir):
    """After gateway calls, dump recent UsageEvent rows (duration / W / Wh)."""
    require_integration()
    if not _via_gateway():
        pytest.skip("INTEGRATION_API_KEY not set — no gateway metering to dump")
    # Give finalize_usage_metering a moment to commit after streaming chat
    time.sleep(0.5)
    try:
        import sqlite3
        from pathlib import Path

        # Host may not see /data — try docker volume via published nothing;
        # fall back to querying auth-gateway container file if mounted.
        candidates = [
            Path("/data/gateway.db"),
            Path(__file__).resolve().parents[1] / "data" / "gateway.db",
        ]
        db_path = next((p for p in candidates if p.is_file()), None)
        if db_path is None:
            # Pull via docker exec into artifact
            import subprocess

            raw = subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "llm-auth-gateway",
                    "python",
                    "-c",
                    r"""
import json
from app.config import get_settings
from app.data import db as dmod
from app.data.models import UsageEvent
dmod.init_db(get_settings())
db = dmod.SessionLocal()
rows = (
    db.query(UsageEvent)
    .order_by(UsageEvent.id.desc())
    .limit(20)
    .all()
)
out = []
for e in rows:
    out.append({
        "id": e.id,
        "service": e.service,
        "model": e.model,
        "result": e.result,
        "status": e.status,
        "duration_ms": e.duration_ms,
        "tokens_in": e.tokens_in,
        "watts": e.watts,
        "watt_hours": e.watt_hours,
        "pool_cost": e.pool_cost,
        "path": e.path,
        "key_label": e.key_label,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    })
db.close()
print(json.dumps(out))
""",
                ],
                text=True,
            )
            import json

            events = json.loads(raw.strip().splitlines()[-1])
        else:
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            cur = con.execute(
                """
                SELECT id, service, model, result, status, duration_ms, tokens_in,
                       watts, watt_hours, pool_cost, path, key_label, created_at
                FROM usage_events
                ORDER BY id DESC LIMIT 20
                """
            )
            events = [dict(r) for r in cur.fetchall()]
            con.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read usage events: {exc}")

    save_json(out_dir, "gateway_usage_events.json", {"events": events})
    chat_ok = [
        e
        for e in events
        if e.get("result") == "ok"
        and (e.get("service") == "chat" or (e.get("path") or "").endswith("chat/completions"))
    ]
    # Soft assert: if we just ran chat via gateway, expect duration and preferably watts
    if chat_ok:
        latest = chat_ok[0]
        assert latest.get("duration_ms") is not None, latest


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
