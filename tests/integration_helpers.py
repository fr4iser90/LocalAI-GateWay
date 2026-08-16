"""Helpers for optional LAN integration tests (INTEGRATION=1)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "output" / "integration"


def load_dotenv_file(path: Path | None = None) -> None:
    """Load KEY=VAL from .env into os.environ (do not override existing)."""
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if "#" in val and not (val.startswith('"') or val.startswith("'")):
            val = val.split("#", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val


def require_integration() -> None:
    load_dotenv_file()
    if os.getenv("INTEGRATION", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("Set INTEGRATION=1 (loads .env) to hit real backends")


def env_source(name: str) -> str:
    load_dotenv_file()
    return (os.getenv(f"{name}_SOURCE") or os.getenv(f"{name}_BACKEND") or "").strip()


def require_source(name: str) -> str:
    require_integration()
    backend = env_source(name)
    if not backend:
        pytest.skip(f"{name}_SOURCE not set")
    return backend


def base_url(hostport: str) -> str:
    return f"http://{hostport}"


def integration_output_dir() -> Path:
    """Create output/integration/<timestamp>/ and return it."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUTPUT_DIR / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    latest = OUTPUT_DIR / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    try:
        latest.symlink_to(run_dir.name, target_is_directory=True)
    except OSError:
        # Windows / restricted FS — write a pointer file instead
        (OUTPUT_DIR / "latest.txt").write_text(str(run_dir), encoding="utf-8")
    return run_dir


def save_bytes(run_dir: Path, name: str, data: bytes) -> Path:
    path = run_dir / name
    path.write_bytes(data)
    return path


def save_text(run_dir: Path, name: str, text: str) -> Path:
    path = run_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def save_json(run_dir: Path, name: str, payload: object) -> Path:
    path = run_dir / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def first_model_id(client: httpx.Client, hostport: str) -> str | None:
    try:
        resp = client.get(f"{base_url(hostport)}/v1/models", timeout=10.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
    except Exception:
        return None
    return None


def tts_voice(client: httpx.Client, hostport: str) -> str:
    """Discover Piper-style voice from GET /."""
    try:
        resp = client.get(f"{base_url(hostport)}/", timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            voices = data.get("voices") or []
            if voices:
                return str(voices[0])
    except Exception:
        pass
    return os.getenv("TTS_VOICE", "de_DE-thorsten-high").strip()


def synthesize_speech(client: httpx.Client, hostport: str, text: str) -> tuple[bytes, str]:
    """POST TTS — try OpenAI path then piper /audio/speech. Returns (audio, path_used)."""
    voice = tts_voice(client, hostport)
    payloads = [
        {"input": text, "voice": voice, "response_format": "wav"},
        {"input": text, "voice": voice},
        {"model": "tts-1", "input": text, "voice": voice, "response_format": "wav"},
    ]
    paths = ["/v1/audio/speech", "/audio/speech"]
    last = None
    for path in paths:
        for payload in payloads:
            resp = client.post(f"{base_url(hostport)}{path}", json=payload, timeout=120.0)
            last = resp
            if resp.status_code < 300 and len(resp.content) > 64:
                return resp.content, path
    detail = last.text[:200] if last is not None else "no response"
    raise AssertionError(f"TTS failed on {hostport}: {detail}")


def transcribe_audio(
    client: httpx.Client, hostport: str, audio: bytes, filename: str = "speech.wav"
) -> tuple[str, str, object]:
    """POST STT. Returns (text, path_used, raw_payload_or_text)."""
    files = {"file": (filename, audio, "audio/wav")}
    attempts = [
        ("/v1/audio/transcriptions", {"model": "whisper-1", "response_format": "json"}),
        ("/v1/audio/transcriptions", {"response_format": "json"}),
        ("/inference", {"response_format": "json"}),
        ("/inference", {"response_format": "text"}),
    ]
    last = None
    for path, data in attempts:
        resp = client.post(
            f"{base_url(hostport)}{path}",
            data=data,
            files=files,
            timeout=180.0,
        )
        last = resp
        if resp.status_code >= 300:
            continue
        ctype = (resp.headers.get("content-type") or "").lower()
        if "json" in ctype:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and "text" in payload:
                    return str(payload["text"]), path, payload
            except Exception:
                pass
        text = resp.text.strip()
        if text:
            return text, path, text
    detail = last.text[:200] if last is not None else "no response"
    raise AssertionError(f"STT failed on {hostport}: {detail}")
