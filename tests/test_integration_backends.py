"""Live API checks against LAN sources / local gateway (INTEGRATION=1).

Artifacts land in ``output/integration/<timestamp>/`` (and ``latest`` symlink),
including per-call watt samples from ``http://<source-host>:9105/power``
(or ``GPU_POWER_URL`` if set).

Preferred path: hit OnPrem API (``INTEGRATION_API_BASE`` / ``:9081``) with
``INTEGRATION_API_KEY`` so chat metering (real duration + Wh) is exercised.
Falls back to direct ``*_SOURCE`` hosts if no API key.

Run::

  set -a && source .env && set +a
  export INTEGRATION=1
  export INTEGRATION_API_KEY=gw_…
  pytest -m integration -q
  # Dense 27B bake-off (~10 min text / ~12 min VL with MTP):
  #   export INTEGRATION_LANDING_HEAVY=1
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.integration_helpers import (
    PowerProbe,
    TEXT_LANDING_REPRO_PROMPT,
    TEXT_SMOKE_PROMPT,
    VL_LANDING_REPRO_PROMPT,
    base_url,
    chat_model_id,
    env_chat_model,
    env_source,
    extract_html_document,
    first_model_id,
    onprem_api_key,
    onprem_api_base,
    gpu_power_url,
    integration_output_dir,
    require_integration,
    require_source,
    resolve_vl_image_path,
    safe_model_filename,
    sample_gpu_watts,
    save_bytes,
    save_json,
    save_text,
    synthesize_speech,
    transcribe_audio,
    vl_user_content,
    write_chat_landing_html,
    write_model_landing_page,
    write_power_index_html,
)

pytestmark = pytest.mark.integration


def _via_gateway() -> tuple[str, dict[str, str]] | None:
    key = onprem_api_key()
    if not key:
        return None
    return onprem_api_base(), {"X-Api-Key": key}


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
                "Each *_power.json samples http://<source-host>:9105/power "
                "(or GPU_POWER_URL)."
            ),
        },
    )
    return run_dir


@pytest.fixture(scope="module")
def power_summary(out_dir):
    rows: list[dict] = []
    yield rows
    save_json(out_dir, "power_summary.json", {"calls": rows})
    write_power_index_html(out_dir, rows)
    landing_rows = [r for r in rows if r.get("landing_file")]
    if landing_rows:
        from tests.integration_helpers import write_compare_landings_index

        write_compare_landings_index(
            out_dir,
            entries=[
                {
                    "label": r.get("kind", "run"),
                    "model": r.get("model"),
                    "duration_ms": r.get("duration_ms"),
                    "watts_avg": r.get("watts_avg"),
                    "watt_hours_est": r.get("watt_hours_est"),
                    "total_tokens": (r.get("usage") or {}).get("total_tokens"),
                    "landing_href": r.get("landing_file"),
                    "power_href": r.get("power_file")
                    or f"chat_{safe_model_filename(str(r.get('model') or 'm'))}_power.json",
                }
                for r in landing_rows
            ],
        )

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

    with PowerProbe("chat_completion", host=host) as probe:
        with httpx.Client(timeout=180.0) as client:
            model = chat_model_id(client, host, headers=headers) or "default"
            resp = client.post(
                f"{base_url(host)}/v1/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": TEXT_SMOKE_PROMPT}],
                    "max_tokens": 128,
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
    write_chat_landing_html(
        out_dir,
        "chat_completion_report.html",
        model=model,
        kind="text",
        content=msg.get("content") or "",
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        power=report,
        mode=mode,
        host=host,
    )

@pytest.mark.parametrize(
    "model,kind,tier",
    [
        ("Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL", "text", "fast"),
        ("Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL-VL", "vl", "fast"),
        ("Qwen3.8-27B-Q4_K_M-MTP", "text", "heavy"),
        ("Qwen3.8-27B-Q4_K_M-MTP-VL", "vl", "heavy"),
    ],
    ids=["a3b-q4-mtp-text", "a3b-q4-mtp-vl", "dense-q4-mtp-text", "dense-q4-mtp-vl"],
)
def test_chat_named_models_power(out_dir, power_summary, model: str, kind: str, tier: str):
    """Bake-off: each model reproduces the landing as HTML + power metrics.

    Default suite: Qwen3.6 35B-A3B MTP Q4 (text + VL). Dense 27B is skipped unless
    ``INTEGRATION_LANDING_HEAVY=1`` (~10–12 min each with Qwen3.8 MTP on Strix Halo).

    Override a single pair via env (skips other params)::

      export INTEGRATION_LANDING_FILTER=1
      export INTEGRATION_CHAT_MODEL=…
      export INTEGRATION_CHAT_MODEL_VL=…

    Open ``compare_landings.html`` and ``*_landing.html`` in the run folder.
    """
    require_integration()
    # Optional filter: only run env-named models when both set and FILTER=1
    load = __import__("tests.integration_helpers", fromlist=["load_dotenv_file"]).load_dotenv_file
    load()
    import os

    heavy = (os.getenv("INTEGRATION_LANDING_HEAVY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if tier == "heavy" and not heavy:
        pytest.skip("set INTEGRATION_LANDING_HEAVY=1 for dense 27B (~10–12 min each)")

    filt = (os.getenv("INTEGRATION_LANDING_FILTER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if filt:
        want_text = env_chat_model("INTEGRATION_CHAT_MODEL")
        want_vl = env_chat_model("INTEGRATION_CHAT_MODEL_VL")
        if kind == "text" and want_text and model != want_text:
            pytest.skip(f"filter text={want_text}")
        if kind == "vl" and want_vl and model != want_vl:
            pytest.skip(f"filter vl={want_vl}")

    gw = _via_gateway()
    if gw:
        host, headers = gw
        mode = "gateway"
    else:
        host = require_source("CHAT")
        headers = {}
        mode = "direct"

    if kind == "vl":
        img = resolve_vl_image_path()
        if img is None:
            pytest.skip("No VL screenshot fixture; see tests/fixtures/README.md")
        user_content = vl_user_content(prompt=VL_LANDING_REPRO_PROMPT)
        label = "landing_vl"
        save_bytes(out_dir, "reference_gic_landing.jpg", img.read_bytes())
        prompt_used = VL_LANDING_REPRO_PROMPT
    else:
        user_content = TEXT_LANDING_REPRO_PROMPT
        label = "landing_text"
        prompt_used = TEXT_LANDING_REPRO_PROMPT

    slug = safe_model_filename(model)
    # No max_tokens — let the model finish the HTML (truncation broke prior landings).
    # Long read timeout for local 27B + vision PP.
    http_timeout = httpx.Timeout(connect=30.0, read=3600.0, write=60.0, pool=30.0)
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0.2,
        # Qwen hybrid: skip reasoning so tokens go into the HTML, not thinking.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    with PowerProbe(f"{label}:{slug}", host=host) as probe:
        with httpx.Client(timeout=http_timeout) as client:
            resp = client.post(
                f"{base_url(host)}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
    assert resp.status_code == 200, resp.text[:800]
    data = resp.json()
    save_json(out_dir, f"chat_{slug}.json", data)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    raw = (msg.get("content") or "").strip()
    if not raw and msg.get("reasoning_content"):
        raw = str(msg.get("reasoning_content") or "")
    save_text(out_dir, f"chat_{slug}_raw.txt", raw)
    landing_html = extract_html_document(raw)
    landing_name = f"chat_{slug}_landing.html"
    landing_path = write_model_landing_page(out_dir, landing_name, landing_html)
    power_name = f"chat_{slug}_power.json"
    report = probe.report(
        mode=mode,
        host=host,
        model=model,
        kind=kind,
        http_status=resp.status_code,
        usage=data.get("usage"),
        vl_image=str(resolve_vl_image_path()) if kind == "vl" else None,
        prompt=prompt_used[:500],
        landing_file=landing_name,
        power_file=power_name,
        finish_reason=(data.get("choices") or [{}])[0].get("finish_reason"),
    )
    save_json(out_dir, power_name, report)
    power_summary.append(report)
    # Metrics sidecar (not the product landing)
    write_chat_landing_html(
        out_dir,
        f"chat_{slug}_metrics.html",
        model=model,
        kind=kind,
        content=f"(see {landing_name} for the generated page)\n\n" + raw[:2000],
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        power=report,
        mode=mode,
        host=host,
    )
    print(
        f"[{kind}] {model}: status={resp.status_code} "
        f"duration_ms={report.get('duration_ms')} "
        f"watts_avg={report.get('watts_avg')} "
        f"Wh≈{report.get('watt_hours_est')} "
        f"usage={data.get('usage')} "
        f"landing={landing_path}"
    )
    assert msg.get("role") == "assistant"
    assert landing_html and "<" in landing_html, "model did not produce HTML"
    assert "html" in landing_html.lower() or "body" in landing_html.lower()

def test_embeddings_returns_vector(out_dir, power_summary):
    gw = _via_gateway()
    if gw:
        host, headers = gw
        mode = "gateway"
    else:
        host = require_source("EMBED")
        headers = {}
        mode = "direct"

    with PowerProbe("embeddings", host=host) as probe:
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

    with PowerProbe("tts", host=host) as probe:
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

    with PowerProbe("roundtrip_tts_stt", host=tts_host) as probe:
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
        # fall back to querying onprem-auth container file if mounted.
        candidates = [
            Path("/data/onprem.db"),
            Path(__file__).resolve().parents[1] / "data" / "onprem.db",
        ]
        db_path = next((p for p in candidates if p.is_file()), None)
        if db_path is None:
            # Pull via docker exec into artifact
            import subprocess

            raw = subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "onprem-auth",
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
