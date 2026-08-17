"""Helpers for optional LAN integration tests (INTEGRATION=1)."""

from __future__ import annotations

import json
import os
import threading
import time
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
    if hostport.startswith("http://") or hostport.startswith("https://"):
        return hostport.rstrip("/")
    return f"http://{hostport}"


def gateway_base() -> str:
    """Gateway URL (INTEGRATION_GATEWAY or http://127.0.0.1:GATEWAY_PORT)."""
    load_dotenv_file()
    explicit = (os.getenv("INTEGRATION_GATEWAY") or "").strip()
    if explicit:
        return base_url(explicit)
    port = (os.getenv("GATEWAY_PORT") or "9081").strip()
    return f"http://127.0.0.1:{port}"


def gateway_api_key() -> str | None:
    load_dotenv_file()
    return (os.getenv("INTEGRATION_API_KEY") or "").strip() or None


def _address_hostport(address: str) -> str:
    """Normalize ``host:port`` from a source address or URL."""
    addr = (address or "").strip()
    if not addr:
        return ""
    if addr.startswith("http://") or addr.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(addr)
        if not parsed.hostname:
            return ""
        if parsed.port:
            return f"{parsed.hostname}:{parsed.port}"
        return parsed.hostname
    return addr


def _gateway_listen_port() -> str:
    load_dotenv_file()
    return (os.getenv("GATEWAY_PORT") or "9081").strip()


def resolve_gpu_power_url(host: str | None = None) -> str:
    """Probe URL: ``GPU_POWER_URL`` if set, else ``http://<source-host>:9105/power``.

    Same co-location rule as the gateway (``suggest_gpu_power_url``). The local
    gateway listen address (``:9081``) is skipped so we do not hit this machine's
    ``:9105`` when tests go through ``INTEGRATION_GATEWAY``.
    """
    load_dotenv_file()
    explicit = (os.getenv("GPU_POWER_URL") or "").strip()
    if explicit:
        return explicit

    from app.usage_pool import suggest_gpu_power_url

    candidates: list[str] = []
    if host:
        candidates.append(host)
    for name in ("CHAT", "EMBED", "STT", "TTS"):
        src = env_source(name)
        if src:
            candidates.append(src)

    gw_port = _gateway_listen_port()
    seen: set[str] = set()
    for cand in candidates:
        addr = _address_hostport(cand)
        if not addr or addr in seen:
            continue
        seen.add(addr)
        port = addr.rsplit(":", 1)[-1] if ":" in addr else ""
        if port == gw_port:
            continue
        url = suggest_gpu_power_url(addr)
        if url:
            return url
    return ""


def gpu_power_url() -> str:
    return resolve_gpu_power_url()


def sample_gpu_watts(url: str | None = None) -> dict | None:
    """One live reading from gpu-power sidecar."""
    u = (url if url is not None else gpu_power_url()).strip()
    if not u:
        return None
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(u)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        return data
    except Exception:
        return None


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


class PowerProbe:
    """Sample GPU watts during a call; estimate Wh from wall time × mean W.

    Idle-at-start + busy-at-end alone skews the average (e.g. 11 W + 82 W → ~47 W).
    A background thread samples every ``interval_s`` while the request runs.
    """

    def __init__(self, label: str, *, interval_s: float = 2.0, host: str | None = None):
        self.label = label
        self.samples: list[float] = []
        self.t0 = 0.0
        self.t1 = 0.0
        self.probe_url = resolve_gpu_power_url(host)
        self.interval_s = max(0.5, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _snap(self) -> None:
        data = sample_gpu_watts(self.probe_url)
        if data and data.get("watts") is not None:
            with self._lock:
                self.samples.append(float(data["watts"]))

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._snap()

    def __enter__(self) -> "PowerProbe":
        self.t0 = time.perf_counter()
        self._snap()
        if self.probe_url:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"power-{self.label}", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 1.0)
            self._thread = None
        self._snap()
        self.t1 = time.perf_counter()

    def report(self, **extra) -> dict:
        duration_s = max(0.0, self.t1 - self.t0)
        with self._lock:
            samples = list(self.samples)
        avg = (sum(samples) / len(samples)) if samples else None
        wh = None
        if avg is not None and duration_s > 0:
            wh = round((avg * duration_s) / 3600.0, 6)
        # Keep JSON readable for hour-long runs: store rounded series + summary.
        rounded = [round(w, 2) for w in samples]
        return {
            "label": self.label,
            "probe": self.probe_url or None,
            "duration_ms": round(duration_s * 1000.0, 1),
            "watts_samples": rounded,
            "watts_sample_count": len(rounded),
            "watts_min": round(min(samples), 2) if samples else None,
            "watts_max": round(max(samples), 2) if samples else None,
            "watts_avg": round(avg, 2) if avg is not None else None,
            "watt_hours_est": wh,
            "note": (
                "Wh ≈ mean(periodic samples every "
                f"{self.interval_s:g}s) × wall_seconds / 3600. "
                "Chat via gateway also stores metered Wh on UsageEvent."
                if self.probe_url
                else (
                    "no gpu-power sidecar — set GPU_POWER_URL or CHAT_SOURCE "
                    "(probe is http://<source-host>:9105/power)"
                )
            ),
            **extra,
        }


def first_model_id(client: httpx.Client, hostport: str, headers: dict | None = None) -> str | None:
    try:
        resp = client.get(
            f"{base_url(hostport)}/v1/models",
            timeout=10.0,
            headers=headers or {},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
    except Exception:
        return None
    return None


def env_chat_model(key: str = "INTEGRATION_CHAT_MODEL") -> str | None:
    """Explicit model id from env, or None."""
    load_dotenv_file()
    mid = (os.getenv(key) or "").strip()
    return mid or None


def chat_model_id(
    client: httpx.Client,
    hostport: str,
    headers: dict | None = None,
    *,
    env_key: str = "INTEGRATION_CHAT_MODEL",
) -> str | None:
    """INTEGRATION_CHAT_MODEL when set, else first id from /v1/models."""
    explicit = env_chat_model(env_key)
    if explicit:
        return explicit
    return first_model_id(client, hostport, headers=headers)


def safe_model_filename(model_id: str) -> str:
    """Filesystem-safe slug for artifact names."""
    import re

    s = re.sub(r"[^\w.\-]+", "_", (model_id or "model").strip())
    return s[:120] or "model"


# Tiny PNG only as last-resort fallback when no screenshot fixture exists.
TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

DEFAULT_VL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "gic_landing.jpg"

# Text model has no image — gets a written brief of the reference page.
GIC_LANDING_TEXT_BRIEF = """
Reference landing page to recreate (text brief only — you cannot see the image):

Brand / title: "The General Intelligence Company Of New York"
Nav: About · Writing · Careers · button "Get Cofounder"
Hero: full-bleed painterly NYC Central Park illustration feel (skyline, park, pond, plane banner "Go Knicks!")
Hero headline: "AI that runs businesses autonomously"
Support: "The General Intelligence Company is an applied AI lab working towards automating businesses full-stack with AI."
CTA link: "Get to know us"
Tagline: "Agentic companies are on the horizon, and we're building them."
Next section (white bg): "Existing specialized agents have shown success in multiple fields but these are all isolated systems. They need a coordinator."
Fields mentioned: Coding, Customer Support, Marketing, Sales (and similar).
Subsection "Isolated systems" with short explanation that specialized agents need a human coordinator.
Aesthetic: editorial NY — large serif headlines, clean sans body, spacious, dark-on-white after the colorful hero. Not purple AI-slop gradients.
""".strip()

TEXT_LANDING_REPRO_PROMPT = f"""{GIC_LANDING_TEXT_BRIEF}

Task: Recreate this landing page as ONE self-contained HTML document (inline CSS, no external assets except optional Google Fonts).
Include a complete document: open and close all tags (html/head/body/style). Hero can be a CSS gradient + shapes if you cannot embed a real park photo.
Output ONLY the HTML document starting with <!DOCTYPE html> or <html>. No markdown fences, no commentary.
"""

VL_LANDING_REPRO_PROMPT = """
This image is a screenshot of a marketing landing page.

Task: Recreate the page as closely as you can as ONE self-contained HTML document (inline CSS).
Match structure, typography hierarchy, nav, hero text, and following sections from what you see.
Complete document only: open and close all tags. For the hero art use a CSS gradient/placeholder (no fake image URLs).
Output ONLY the HTML document starting with <!DOCTYPE html> or <html>. No markdown fences, no commentary before/after.
""".strip()
# Kept for the generic chat smoke (non-landing).
TEXT_SMOKE_PROMPT = (
    "In one short sentence: what does a local AI API gateway do? "
    "No bullet list."
)


def format_duration_ms(ms: float | int | None) -> str:
    """Human wall time: ``58.9 min (3534724 ms)`` / ``12.4 s`` / ``1.2 h``."""
    if ms is None:
        return "—"
    try:
        total_ms = float(ms)
    except (TypeError, ValueError):
        return str(ms)
    if total_ms < 0:
        total_ms = 0.0
    sec = total_ms / 1000.0
    if sec < 60:
        human = f"{sec:.1f} s"
    elif sec < 3600:
        human = f"{sec / 60.0:.1f} min"
    else:
        human = f"{sec / 3600.0:.2f} h"
    if total_ms >= 1000:
        return f"{human} ({total_ms:,.0f} ms)".replace(",", " ")
    return f"{human} ({total_ms:.1f} ms)"


def extract_html_document(text: str) -> str:
    """Pull a usable HTML document out of a model reply (strip fences / chatter)."""
    import re

    t = (text or "").strip()
    if not t:
        return ""
    fence = re.search(r"```(?:html)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if fence:
        t = fence.group(1).strip()
    lower = t.lower()
    for marker in ("<!doctype html", "<html"):
        i = lower.find(marker)
        if i >= 0:
            return t[i:].strip()
    # Fragment → wrap so the browser still opens something
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>generated</title></head><body>\n"
        f"{t}\n</body></html>"
    )


def write_model_landing_page(run_dir: Path, name: str, html: str) -> Path:
    """Save the model-produced landing HTML (the artifact to compare visually)."""
    return save_text(run_dir, name, html if html.endswith("\n") else html + "\n")


def write_compare_landings_index(
    run_dir: Path,
    *,
    entries: list[dict],
) -> Path:
    """Side-by-side links: generated landings + watts/tokens for the bake-off."""
    cards = []
    for e in entries:
        cards.append(
            f"""
<article class="card">
  <h2>{_html_escape(e.get("label"))}</h2>
  <p class="mono">{_html_escape(e.get("model"))}</p>
  <p class="meta">
    {_html_escape(format_duration_ms(e.get("duration_ms")))} ·
    {_html_escape(e.get("watts_avg"))} W avg ·
    {_html_escape(e.get("watt_hours_est"))} Wh ·
    tokens {_html_escape(e.get("total_tokens"))}
  </p>
  <p><a class="btn" href="{_html_escape(e.get("landing_href"))}">Open generated landing</a></p>
  <p class="small"><a href="{_html_escape(e.get("power_href"))}">power.json</a></p>
</article>
"""
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landing bake-off</title>
<style>
body{{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee;padding:2rem}}
h1{{font-size:1.5rem}}
.grid{{display:grid;gap:1.25rem;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr))}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:1.25rem}}
.mono{{font-family:ui-monospace,monospace;font-size:.85rem;word-break:break-all}}
.meta{{color:#aaa;font-size:.9rem}}
.btn{{display:inline-block;margin-top:.5rem;padding:.55rem 1rem;background:#3d9a7a;color:#04140f;text-decoration:none;border-radius:999px;font-weight:600}}
.small a{{color:#8af}}
.note{{color:#888;max-width:40rem}}
</style></head><body>
<h1>Landing page bake-off</h1>
<p class="note">Each model was asked to <strong>reproduce</strong> the reference landing as HTML.
Open the links to compare visuals. Metrics are for that generation call only.</p>
<p class="note"><a href="reference_gic_landing.jpg" style="color:#8af">Reference screenshot</a> (input to VL).</p>
<div class="grid">{''.join(cards)}</div>
</body></html>
"""
    return save_text(run_dir, "compare_landings.html", html)


def resolve_vl_image_path() -> Path | None:
    """INTEGRATION_VL_IMAGE, else tests/fixtures/gic_landing.jpg if present."""
    load_dotenv_file()
    explicit = (os.getenv("INTEGRATION_VL_IMAGE") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    if DEFAULT_VL_FIXTURE.is_file():
        return DEFAULT_VL_FIXTURE
    return None


def image_file_to_data_url(path: Path, *, max_bytes: int = 2_500_000) -> str:
    """Encode a local screenshot as data URL for OpenAI-style image_url parts."""
    import base64
    import mimetypes

    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise AssertionError(
            f"VL image too large ({len(raw)} bytes > {max_bytes}). "
            f"Resize or set INTEGRATION_VL_IMAGE to a smaller file: {path}"
        )
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        suffix = path.suffix.lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/jpeg")
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def vl_user_content(*, prompt: str | None = None) -> list[dict]:
    """Multimodal user content: text + screenshot (fixture or INTEGRATION_VL_IMAGE)."""
    text = (prompt or VL_LANDING_REPRO_PROMPT).strip()
    path = resolve_vl_image_path()
    if path is None:
        raise AssertionError(
            "No VL screenshot. Capture one (see tests/fixtures/README.md) or set "
            "INTEGRATION_VL_IMAGE=/path/to.png"
        )
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_file_to_data_url(path)}},
    ]


def _html_escape(s: object) -> str:
    t = str(s if s is not None else "")
    return (
        t.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_chat_landing_html(
    run_dir: Path,
    name: str,
    *,
    model: str,
    kind: str,
    content: str,
    usage: dict | None,
    power: dict,
    mode: str,
    host: str,
) -> Path:
    """GIC-inspired one-pager in output/ — completion + watts + usage (not in the app)."""
    usage = usage or {}
    prompt_t = usage.get("prompt_tokens")
    completion_t = usage.get("completion_tokens")
    total_t = usage.get("total_tokens")
    watts = power.get("watts_avg")
    wmin = power.get("watts_min")
    wmax = power.get("watts_max")
    n_samp = power.get("watts_sample_count")
    wh = power.get("watt_hours_est")
    dur = power.get("duration_ms")
    samples = power.get("watts_samples") or []
    if isinstance(samples, list) and len(samples) > 12:
        sample_preview = f"{samples[:3]} … {samples[-3:]} (n={len(samples)})"
    else:
        sample_preview = samples if samples else "—"

    def fmt(v, suffix=""):
        if v is None:
            return "—"
        return f"{v}{suffix}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html_escape(model)} · integration report</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:#0a0c0f; --ink:#f2f4f7; --muted:#9aa3b0; --faint:#6b7380;
      --line:rgba(242,244,247,.12); --accent:#3d9a7a; --elev:#12161c;
      --display:"Syne",sans-serif; --body:"Manrope",sans-serif;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; min-height:100vh; background:var(--bg); color:var(--ink);
      font-family:var(--body); line-height:1.55;
      background-image:
        radial-gradient(ellipse 70% 50% at 75% 15%, rgba(61,154,122,.2), transparent 55%),
        linear-gradient(165deg,#0a0c0f,#10151c 50%,#0a0c0f);
    }}
    main {{
      max-width:42rem; margin:0 auto;
      padding:clamp(3rem,10vh,6rem) clamp(1.25rem,4vw,2.5rem) 4rem;
    }}
    .brand {{
      font-family:var(--display); font-weight:800; font-size:clamp(2rem,6vw,3.4rem);
      letter-spacing:-.04em; line-height:.95; margin:0 0 1rem; max-width:16ch;
    }}
    h1 {{
      font-family:var(--display); font-weight:700; font-size:clamp(1.2rem,2.8vw,1.65rem);
      letter-spacing:-.03em; margin:0 0 .75rem; max-width:28ch;
    }}
    .lede {{ color:var(--muted); margin:0 0 2rem; max-width:36rem; }}
    .grid {{
      display:grid; gap:0; border-top:1px solid var(--line); margin:0 0 2rem;
    }}
    .row {{
      display:flex; flex-wrap:wrap; justify-content:space-between; gap:.5rem 1.5rem;
      padding:1rem 0; border-bottom:1px solid var(--line);
    }}
    .k {{ color:var(--faint); font-size:.8rem; text-transform:uppercase; letter-spacing:.08em; }}
    .v {{ font-variant-numeric:tabular-nums; font-weight:600; }}
    .mono {{ font-family:ui-monospace,monospace; font-size:.9rem; word-break:break-all; }}
    .out {{
      background:var(--elev); border:1px solid var(--line); border-radius:12px;
      padding:1.25rem 1.35rem; white-space:pre-wrap; font-size:1.05rem;
    }}
    .fig {{ margin:1.5rem 0 0; font-size:.8rem; color:var(--faint); }}
    footer {{ margin-top:3rem; color:var(--faint); font-size:.85rem; }}
  </style>
</head>
<body>
<main>
  <p class="brand">LocalAI Gateway</p>
  <h1>Integration run · {_html_escape(kind)} model</h1>
  <p class="lede">Smoke result for this call: reply, token usage, wall time, and GPU watt samples (sidecar). Not served by the gateway app — file only under <span class="mono">output/integration/</span>.</p>

  <div class="grid">
    <div class="row"><span class="k">Model</span><span class="v mono">{_html_escape(model)}</span></div>
    <div class="row"><span class="k">Kind</span><span class="v">{_html_escape(kind)}</span></div>
    <div class="row"><span class="k">Mode</span><span class="v">{_html_escape(mode)} · {_html_escape(host)}</span></div>
    <div class="row"><span class="k">HTTP</span><span class="v">{_html_escape(power.get("http_status"))}</span></div>
    <div class="row"><span class="k">Duration</span><span class="v">{_html_escape(format_duration_ms(dur))}</span></div>
    <div class="row"><span class="k">Watts avg</span><span class="v">{fmt(watts, " W")}</span></div>
    <div class="row"><span class="k">Watts min / max</span><span class="v">{fmt(wmin, " W")} / {fmt(wmax, " W")}</span></div>
    <div class="row"><span class="k">Wh (est.)</span><span class="v">{fmt(wh)}</span></div>
    <div class="row"><span class="k">Watt samples</span><span class="v mono">{_html_escape(sample_preview)} · n={_html_escape(n_samp if n_samp is not None else len(samples) if samples else 0)}</span></div>
    <div class="row"><span class="k">Prompt tokens</span><span class="v">{fmt(prompt_t)}</span></div>
    <div class="row"><span class="k">Completion tokens</span><span class="v">{fmt(completion_t)}</span></div>
    <div class="row"><span class="k">Total tokens</span><span class="v">{fmt(total_t)}</span></div>
  </div>

  <p class="k">Completion</p>
  <div class="out">{_html_escape(content) or "—"}</div>
  <p class="fig">Fig. 1 — PowerProbe: Wh ≈ mean(watts) × wall_seconds / 3600 · usage from API JSON when present</p>
  <footer>© LocalAI Gateway · test artifact</footer>
</main>
</body>
</html>
"""
    return save_text(run_dir, name, html)


def write_power_index_html(run_dir: Path, calls: list[dict]) -> Path | None:
    """Aggregate landing for all PowerProbe rows in this run."""
    if not calls:
        return None
    rows = []
    for c in calls:
        rows.append(
            "<div class=\"row\">"
            f"<span class=\"mono\">{_html_escape(c.get('model') or c.get('label'))}</span>"
            f"<span>{_html_escape(format_duration_ms(c.get('duration_ms')))} · "
            f"{_html_escape(c.get('watts_avg'))} W · "
            f"Wh {_html_escape(c.get('watt_hours_est'))}</span>"
            "</div>"
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Integration power summary</title>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600&family=Syne:wght@800&display=swap" rel="stylesheet">
<style>
body{{margin:0;background:#0a0c0f;color:#f2f4f7;font-family:Manrope,sans-serif;padding:3rem 1.5rem}}
.brand{{font-family:Syne,sans-serif;font-size:2.5rem;font-weight:800;letter-spacing:-.04em}}
.row{{display:flex;justify-content:space-between;gap:1rem;padding:.85rem 0;border-bottom:1px solid rgba(242,244,247,.12);flex-wrap:wrap}}
.mono{{font-family:ui-monospace,monospace;font-size:.9rem;word-break:break-all}}
.muted{{color:#9aa3b0}}
</style></head><body>
<p class="brand">LocalAI Gateway</p>
<p class="muted">Power summary · this integration run</p>
{''.join(rows)}
</body></html>
"""
    return save_text(run_dir, "index.html", html)


def tts_voice(client: httpx.Client, hostport: str, headers: dict | None = None) -> str:
    """Discover Piper-style voice from GET /."""
    try:
        resp = client.get(f"{base_url(hostport)}/", timeout=10.0, headers=headers or {})
        if resp.status_code == 200:
            data = resp.json()
            voices = data.get("voices") or []
            if voices:
                return str(voices[0])
    except Exception:
        pass
    return os.getenv("TTS_VOICE", "de_DE-thorsten-high").strip()


def synthesize_speech(
    client: httpx.Client,
    hostport: str,
    text: str,
    headers: dict | None = None,
) -> tuple[bytes, str]:
    """POST TTS — try OpenAI path then piper /audio/speech. Returns (audio, path_used)."""
    hdrs = headers or {}
    voice = tts_voice(client, hostport, headers=hdrs)
    payloads = [
        {"input": text, "voice": voice, "response_format": "wav"},
        {"input": text, "voice": voice},
        {"model": "tts-1", "input": text, "voice": voice, "response_format": "wav"},
    ]
    paths = ["/v1/audio/speech", "/audio/speech"]
    last = None
    for path in paths:
        for payload in payloads:
            resp = client.post(
                f"{base_url(hostport)}{path}",
                json=payload,
                timeout=120.0,
                headers=hdrs,
            )
            last = resp
            if resp.status_code < 300 and len(resp.content) > 64:
                return resp.content, path
    detail = last.text[:200] if last is not None else "no response"
    raise AssertionError(f"TTS failed on {hostport}: {detail}")


def transcribe_audio(
    client: httpx.Client,
    hostport: str,
    audio: bytes,
    filename: str = "speech.wav",
    headers: dict | None = None,
) -> tuple[str, str, object]:
    """POST STT. Returns (text, path_used, raw_payload_or_text)."""
    hdrs = dict(headers or {})
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
            headers=hdrs,
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
