# LocalAI-GateWay

Auth + admin UI in front of local AI backends (llama.cpp / Ollama / Whisper / Piper / …).

**One API base URL + API key.** The path selects the service. No per-service subdomains.  
A reverse proxy is optional.

## Architecture

```
Client → (optional) your proxy → llm-gateway (nginx, path routes)
                              → auth check → upstream host:port
Admin  → auth-gateway Web UI
```

## Quick start (no reverse proxy)

```bash
cp .env.example .env   # SESSION_SECRET, ADMIN_BOOTSTRAP_PASSWORD, DOMAIN, PUBLIC_HOST
docker compose up -d --build
```

- Admin: `http://localhost:9080`
- API: `http://localhost:9081/v1/chat/completions` with header `X-Api-Key: …`

## API paths

| Path | Resolves to |
|------|-------------|
| `/v1/chat/completions` (etc.) | Source chosen by request **`model`** among **enabled** catalog rows. Unknown / disabled / missing model → error (no dump onto another box) |
| `/v1/embeddings` | Same for embed sources + `model` |
| `/api/…` | Named or chat paths as configured — still only enabled models on `/v1` |
| `/s/{name}/v1/…` | Force source `{name}`; model must still be enabled on that source |
| `/v1/audio/transcriptions` | STT source for the requested enabled model |
| `/v1/audio/speech` | TTS source for the requested enabled model |

**Model merge:** `/v1` picks a source by request `model` from the **enabled** catalog. No match → 404 `unknown_model` (or 400 `missing_model`). Who may call a source is the **user/key grant**. `/s/{name}/` still forces a source, but disabled/unknown models are rejected.  
**API dialects:** `api_style` on each source — see `app/data/dialects.py`.  
**Model catalog:** Admin → Models — sync, disable, tags/notes/docs links.  
Keys/Teams pick models via checkboxes (empty = all). Favorites pin order in `/v1/models` (key overrides team).  
`GET /v1/models` returns only enabled models ∩ key/team allowlist (notes → `description` when set).  
Aliases **`auto`**, **`auto-quality`**, **`auto-long`** are rewritten in Settings → Routing (daily Q4 MoE+MTP / Q5 MoE+MTP / 128k). Enable **Auto-VL** so screenshots follow the text model.

## Optional reverse proxy

Point one hostname (`PUBLIC_HOST`) at the API gateway container (port 80).  
Admin can stay on `gateway.${DOMAIN}` (see optional `compose.traefik.yaml`) or your own proxy rules.

```bash
# Only if you already use Traefik on network "proxy":
docker compose -f compose.traefik.yaml up -d --build
```

## Web UI vs `.env`

| `.env` | Web UI |
|--------|--------|
| `DOMAIN`, `PUBLIC_HOST`, `SESSION_SECRET`, bootstrap admin | API keys, users, SMTP, teams |
| Ports, temp-guard (prod example) | Named sources (kind + address); grant keys per source |

## Layout

```
app/                  # FastAPI auth + admin
services/temp-guard/  # optional thermal sidecar
compose.yaml          # default
compose.traefik.yaml  # optional Traefik example
tests/                # pytest (unit + optional integration)
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q -m "not integration"          # CI / default — no LAN required

# Optional: probe your real backends from .env (do not commit .env)
INTEGRATION=1 pytest -m integration -q
# needs CHAT_SOURCE / EMBED_SOURCE / STT_SOURCE / TTS_SOURCE (CHAT2 optional)
# includes chat/embed JSON checks + TTS→STT audio roundtrip
# responses written to output/integration/latest/
```
