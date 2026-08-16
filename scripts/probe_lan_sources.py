#!/usr/bin/env python3
"""One-shot probe of LAN sources from env (for crafting integration tests)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

OUT = Path("/tmp/lag_tts_probe.bin")


def src(name: str) -> str:
    return (os.getenv(f"{name}_SOURCE") or os.getenv(f"{name}_BACKEND") or "").strip()


def main() -> int:
    for name in ["CHAT", "EMBED", "STT", "TTS", "CHAT2"]:
        b = src(name)
        print(f"=== {name} {b or '(unset)'} ===")
        if not b:
            continue
        with httpx.Client(timeout=8.0, follow_redirects=True) as c:
            for p in ["/health", "/v1/models", "/"]:
                try:
                    r = c.get(f"http://{b}{p}")
                    body = r.text[:160].replace("\n", " ")
                    print(f"  GET {p} -> {r.status_code} {r.headers.get('content-type')} {body!r}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  GET {p} -> ERR {exc}")

    tts = src("TTS")
    audio = b""
    if tts:
        print("--- TTS /v1/audio/speech ---")
        payloads = [
            {"model": "tts-1", "input": "hello gateway test", "voice": "alloy", "response_format": "wav"},
            {"model": "tts", "input": "hello gateway test", "response_format": "wav"},
            {"input": "hello gateway test", "response_format": "wav"},
            {"model": "tts-1", "input": "hello gateway test"},
        ]
        with httpx.Client(timeout=90.0) as c:
            for payload in payloads:
                try:
                    r = c.post(f"http://{tts}/v1/audio/speech", json=payload)
                    print(
                        f"  {payload} -> {r.status_code} "
                        f"{r.headers.get('content-type')} len={len(r.content)}"
                    )
                    if r.status_code >= 400:
                        print(f"    err={r.text[:200]!r}")
                    elif len(r.content) > 100:
                        OUT.write_bytes(r.content)
                        audio = r.content
                        print(f"  saved {OUT}")
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  ERR {exc}")

    stt = src("STT")
    if stt and audio:
        print("--- STT /v1/audio/transcriptions ---")
        with httpx.Client(timeout=180.0) as c:
            for data in [
                {"model": "whisper-1"},
                {"model": "Systran/faster-whisper-large-v3"},
                {"model": "tiny"},
                {},
            ]:
                files = {"file": ("speech.wav", audio, "audio/wav")}
                try:
                    r = c.post(f"http://{stt}/v1/audio/transcriptions", data=data, files=files)
                    print(f"  data={data} -> {r.status_code} {r.text[:300]!r}")
                    if r.status_code < 300:
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  ERR {exc}")

    chat = src("CHAT")
    if chat:
        print("--- CHAT /v1/chat/completions ---")
        with httpx.Client(timeout=90.0) as c:
            # discover a model id if possible
            model = "gpt-4"
            try:
                m = c.get(f"http://{chat}/v1/models")
                if m.status_code == 200:
                    data = m.json()
                    ids = [x.get("id") for x in (data.get("data") or []) if isinstance(x, dict)]
                    if ids:
                        model = str(ids[0])
                        print(f"  using model {model}")
            except Exception:
                pass
            r = c.post(
                f"http://{chat}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
            )
            print(f"  -> {r.status_code} {r.text[:300]!r}")

    emb = src("EMBED")
    if emb:
        print("--- EMBED /v1/embeddings ---")
        with httpx.Client(timeout=90.0) as c:
            model = "text-embedding"
            try:
                m = c.get(f"http://{emb}/v1/models")
                if m.status_code == 200:
                    data = m.json()
                    ids = [x.get("id") for x in (data.get("data") or []) if isinstance(x, dict)]
                    if ids:
                        model = str(ids[0])
            except Exception:
                pass
            r = c.post(
                f"http://{emb}/v1/embeddings",
                json={"model": model, "input": "hello gateway"},
            )
            print(f"  model={model} -> {r.status_code} {r.text[:300]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
