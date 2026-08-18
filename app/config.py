from functools import lru_cache
import re

from pydantic_settings import BaseSettings, SettingsConfigDict

from .data.dialects import (  # noqa: F401 — re-export
    API_STYLES,
    dialect_choices,
    map_upstream_path,
    resolve_api_style,
)


# Functional kinds (path families). Source *names* are free-form slugs in the DB.
KINDS = ("chat", "embed", "stt", "tts")
MODEL_CHECK_KINDS = frozenset(KINDS)
MODEL_REQUIRED_KINDS = frozenset({"chat", "embed"})
MODEL_ROUTE_KINDS = frozenset(KINDS)

SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

KIND_PATH_HINTS = {
    "chat": "/v1/chat/completions · /api/*",
    "embed": "/v1/embeddings",
    "stt": "/v1/audio/transcriptions",
    "tts": "/v1/audio/speech",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    domain: str = "localhost"
    public_host: str = ""
    data_dir: str = "/data"
    session_secret: str = "change-me-session-secret"
    admin_bootstrap_user: str = "admin"
    admin_bootstrap_password: str = "changeme"
    session_max_age: int = 60 * 60 * 12

    llm_api_key: str | None = None
    ollama_api_key: str | None = None
    embed_api_key: str | None = None
    stt_api_key: str | None = None
    tts_api_key: str | None = None

    chat_source: str = ""
    chat2_source: str = ""
    embed_source: str = ""
    stt_source: str = ""
    tts_source: str = ""
    chat_backend: str = ""
    chat2_backend: str = ""
    llm_backend: str = ""
    ollama_backend: str = ""
    embed_backend: str = ""
    stt_backend: str = ""
    tts_backend: str = ""

    temp_max_c: str = "30"
    temp_guard_disabled: bool = False
    # If the sidecar is unreachable or returns an unexpected status:
    # - true  → allow traffic (fail-open)
    # - false → reject traffic (fail-closed)
    temp_guard_fail_open: bool = True
    # Deprecated legacy override; normal behavior derives /check from each source host.
    temp_guard_url: str = "http://source-sidecar:8080/check"
    # Optional source-sidecar power probe. Empty = off.
    gpu_power_url: str = ""
    display_timezone: str = "UTC"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def split_source_path(path: str) -> tuple[str | None, str]:
    """If path is /s/{name}/..., return (name, upstream_path). Else (None, path)."""
    p = (path or "").split("?")[0]
    parts = p.split("/")
    if len(parts) >= 3 and parts[1].lower() == "s":
        name = parts[2].lower()
        if SOURCE_NAME_RE.match(name):
            rest = "/" + "/".join(parts[3:]) if len(parts) > 3 else "/"
            return name, rest
    return None, p


def kind_from_upstream_path(upstream: str) -> str | None:
    """Map stripped upstream path → kind (for default sources)."""
    p = (upstream or "").split("?")[0].lower()
    if p.startswith("/api/"):
        return "chat"
    if p.startswith("/v1/embeddings"):
        return "embed"
    if p.startswith("/v1/audio/transcriptions") or p.startswith("/v1/audio/translations"):
        return "stt"
    if p.startswith("/v1/audio/speech"):
        return "tts"
    if (
        p.startswith("/v1/chat/completions")
        or p.startswith("/v1/completions")
        or p.startswith("/completion")
        or p.startswith("/v1/models")
        or p.startswith("/v1/")
        or p.startswith("/chat")
    ):
        return "chat"
    return None


def upstream_path_for_proxy(path: str) -> str:
    """Path sent to the upstream (strips /s/{name} when present)."""
    named, upstream = split_source_path(path)
    if named is not None:
        return upstream or "/"
    return (path or "").split("?")[0] or "/"


def public_route_for_source(
    name: str,
    kind: str,
    *,
    is_default: bool = False,
    settings: Settings | None = None,
) -> str:
    """Client path for this kind. All sources of a kind share /v1; model picks the box."""
    del name, is_default  # equal sources — catalog merge, not /s/{name} vs primary
    settings = settings or get_settings()
    base = (settings.public_host or "").strip() or "API-gateway"
    hint = KIND_PATH_HINTS.get(kind, "/")
    return f"{base}{hint}"


SERVICES = KINDS
MODEL_CHECK_SERVICES = MODEL_CHECK_KINDS


def public_api_base(*, gateway_port: str | None = None) -> str:
    """Client-facing OpenAI-compatible base (…/v1), no trailing slash after v1."""
    import os

    settings = get_settings()
    host = (settings.public_host or os.getenv("PUBLIC_HOST") or "").strip()
    if host and host != "_":
        host = host.split("/")[0].strip()
        if host.startswith("http://") or host.startswith("https://"):
            return f"{host.rstrip('/')}/v1"
        # Homelab public names are almost always TLS via reverse proxy
        return f"https://{host}/v1"
    port = gateway_port or os.getenv("GATEWAY_PORT", "9081")
    return f"http://localhost:{port}/v1"
