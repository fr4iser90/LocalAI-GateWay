from __future__ import annotations

import os

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .admin.access import Forbidden, RedirectToLogin
from .admin.accounts import get_auth_settings, router as accounts_router
from .admin.routes import router as admin_router
from .admin.ops import router as ops_router
from .auth.check import authorize, extract_model
from .data.backends import (
    address_for_source,
    get_source_by_name,
    resolve_source_for_kind,
)
from .config import (
    MODEL_ROUTE_KINDS,
    get_settings,
    kind_from_upstream_path,
    map_upstream_path,
    split_source_path,
    upstream_path_for_proxy,
)
from .data.db import get_db, init_db
from .vision_route import (
    mint_forward_ticket,
    parse_forward_ticket,
    request_needs_vision,
    resolve_vl_model,
    rewrite_json_model,
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="LLM Gateway Auth", docs_url=None, redoc_url=None)
    app.state.settings = settings
    init_db(settings)

    @app.on_event("startup")
    def _log_public_urls() -> None:
        port = os.getenv("PORT", "8080")
        web_port = os.getenv("WEB_PORT") or port
        gateway_port = os.getenv("GATEWAY_PORT", "9081")
        base = f"http://127.0.0.1:{web_port}"
        print(f"Web UI:       {base}", flush=True)
        print(f"  Admin:      {base}/          (platform admin)", flush=True)
        print(f"  User:       {base}/me        (team members)", flush=True)
        print(f"  Login:      {base}/login", flush=True)
        print(f"  Account:    {base}/account   (password / email)", flush=True)
        print(f"  Forgot:     {base}/forgot", flush=True)
        print(f"  Register:   {base}/register (if enabled in Settings)", flush=True)
        print(f"API gateway:  http://127.0.0.1:{gateway_port}", flush=True)
        ph = (settings.public_host or "").strip() or f"127.0.0.1:{gateway_port}"
        print(f"  example:    http://{ph}/v1/chat/completions  (+ X-Api-Key)", flush=True)

    static_dir = Path(__file__).parent / "admin" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def force_password_change(request: Request, call_next):
        path = request.url.path
        allow_prefixes = ("/static", "/v1/")
        allow_exact = {
            "/login",
            "/logout",
            "/forgot",
            "/reset",
            "/register",
            "/account",
            "/account/password",
            "/account/email",
            "/privacy",
            "/privacy/wipe-usage",
            "/healthz",
        }
        allow = path.startswith(allow_prefixes) or path in allow_exact
        try:
            uid = request.session.get("user_id")
        except AssertionError:
            uid = None
        if uid and not allow:
            from .data.db import SessionLocal
            from .data.models import AdminUser

            if SessionLocal is not None:
                db = SessionLocal()
                try:
                    u = db.get(AdminUser, uid)
                    if u and u.must_change_password:
                        return RedirectResponse("/account", status_code=303)
                finally:
                    db.close()
        return await call_next(request)

    # Session must wrap the app (add last = outermost)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="gateway_admin",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=False,
    )

    @app.exception_handler(RedirectToLogin)
    async def _redirect_login(_request: Request, _exc: RedirectToLogin):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(_request: Request, _exc: Forbidden):
        return HTMLResponse("<h1>403 Forbidden</h1><p><a href='/me'>Back</a></p>", status_code=403)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/v1/gateway/models")
    def gateway_models(request: Request, db: Session = Depends(get_db)):
        """Filtered OpenAI model list for IDEs (nginx routes GET /v1/models here)."""
        from .data.catalog import (
            favorites_for_key,
            load_api_key,
            models_visible_for_key,
            openai_models_payload,
        )
        from .data.models import utcnow

        raw_key = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization") or ""
        if not raw_key and auth.lower().startswith("bearer "):
            raw_key = auth[7:].strip()
        api_key = load_api_key(db, raw_key)
        if api_key is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if api_key.expires_at and api_key.expires_at < utcnow():
            return JSONResponse({"error": "expired_api_key"}, status_code=401)
        rows = models_visible_for_key(db, api_key)
        return openai_models_payload(rows, favorites_for_key(api_key))

    @app.api_route("/v1/auth/check", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def auth_check(request: Request, db: Session = Depends(get_db)):
        settings = request.app.state.settings
        host = request.headers.get("x-original-host") or request.headers.get("host") or ""
        uri = request.headers.get("x-original-uri") or request.url.path
        method = request.headers.get("x-original-method") or request.method
        client_ip = request.headers.get("x-real-ip") or (
            request.client.host if request.client else ""
        )
        raw_key = request.headers.get("x-api-key")
        named, upstream_uri = split_source_path(uri)
        kind = None
        service = None
        body = None
        content_type = request.headers.get("content-type")
        method_u = method.upper()
        vl_rewrite: str | None = None

        if named is not None:
            src = get_source_by_name(db, named)
            if src is None:
                return JSONResponse(
                    {"error": "unknown_source", "source": named, "path": uri},
                    status_code=403,
                )
            service = src.name
            kind = src.kind
            upstream_uri = upstream_uri or "/"
            if kind in MODEL_ROUTE_KINDS and method_u in {"POST", "PUT", "PATCH"}:
                body = await request.body()
        else:
            upstream_uri = upstream_path_for_proxy(uri)
            kind = kind_from_upstream_path(upstream_uri)
            if kind is None:
                return JSONResponse(
                    {"error": "unknown_route", "host": host, "path": uri},
                    status_code=403,
                )
            # Read body early so model routing can pick the source
            if kind in MODEL_ROUTE_KINDS and method_u in {"POST", "PUT", "PATCH"}:
                body = await request.body()
            model = extract_model(body, content_type) if body else None
            src = resolve_source_for_kind(db, kind, model=model)
            if src is None:
                return JSONResponse(
                    {"error": "backend_not_configured", "kind": kind},
                    status_code=503,
                )
            service = src.name

        # Optional VL: detect image parts → authorize + forward as vision sibling
        if (
            kind == "chat"
            and body
            and get_auth_settings(db).auto_vl_routing
            and request_needs_vision(body, content_type)
        ):
            asked = extract_model(body, content_type)
            sibling = resolve_vl_model(db, service, asked)
            if sibling and sibling != asked:
                body = rewrite_json_model(body, sibling)
                vl_rewrite = sibling

        result = authorize(
            db,
            raw_key=raw_key,
            service=service,
            kind=kind,
            method=method,
            path=upstream_uri,
            host=host,
            client_ip=client_ip,
            body=body,
            content_type=content_type,
        )

        headers = {}
        if result.remaining_rpm is not None:
            headers["X-RateLimit-Remaining"] = str(result.remaining_rpm)
        headers["X-Priority"] = str(result.priority)
        headers["X-Auth-Reason"] = result.reason

        if result.status != 204:
            return JSONResponse(
                {"error": result.reason, "service": service},
                status_code=result.status,
                headers=headers,
            )

        backend = address_for_source(db, service)
        if not backend:
            return JSONResponse(
                {"error": "backend_not_configured", "service": service},
                status_code=503,
                headers=headers,
            )

        headers["X-Backend"] = backend
        # Always rewrite so nginx can map OpenAI client paths → backend dialect
        # (piper /audio/speech, whisper.cpp /inference, /s/{name}/ strip, …)
        src_row = get_source_by_name(db, service)
        api_style = getattr(src_row, "api_style", None) if src_row is not None else None
        rewrite_uri = map_upstream_path(
            upstream_uri, kind=kind, api_style=api_style
        )
        headers["X-Rewrite-Uri"] = rewrite_uri

        if vl_rewrite:
            headers["X-Gateway-Proxy"] = "1"
            headers["X-Gateway-Ticket"] = mint_forward_ticket(
                secret=settings.session_secret,
                service=service,
                backend=backend,
                rewrite_uri=rewrite_uri,
                rewrite_model=vl_rewrite,
            )
            headers["X-Auth-Reason"] = f"ok;vl={vl_rewrite}"

        return Response(status_code=204, headers=headers)

    @app.api_route("/v1/gateway/forward", methods=["POST", "PUT", "PATCH"])
    async def gateway_forward(request: Request):
        """Internal body rewrite (VL) after auth_request — ticket required."""
        settings = request.app.state.settings
        ticket = request.headers.get("x-gateway-ticket") or ""
        payload = parse_forward_ticket(ticket, settings.session_secret)
        if payload is None:
            return JSONResponse({"error": "invalid_gateway_ticket"}, status_code=403)

        backend = str(payload.get("backend") or "").strip()
        rewrite_uri = str(payload.get("uri") or "/").strip() or "/"
        rewrite_model = str(payload.get("model") or "").strip()
        if not backend or not rewrite_model:
            return JSONResponse({"error": "invalid_gateway_ticket"}, status_code=403)

        body = await request.body()
        body = rewrite_json_model(body, rewrite_model)
        url = f"http://{backend}{rewrite_uri}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        hop = {
            "content-type",
            "content-length",
            "host",
            "connection",
            "transfer-encoding",
            "x-gateway-ticket",
            "x-gateway-proxy",
        }
        up_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in hop
        }
        up_headers["content-type"] = "application/json"
        up_headers["content-length"] = str(len(body))

        client = httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=10.0))
        try:
            upstream = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=up_headers,
                    content=body,
                ),
                stream=True,
            )
        except Exception as exc:
            await client.aclose()
            return JSONResponse(
                {"error": "upstream_unreachable", "detail": str(exc)},
                status_code=502,
            )

        out_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower()
            not in {"transfer-encoding", "connection", "content-length", "content-encoding"}
        }

        async def _stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _stream(),
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=upstream.headers.get("content-type"),
        )

    app.include_router(admin_router)
    app.include_router(ops_router)
    app.include_router(accounts_router)
    return app


app = create_app()
