"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from ..core import Gateway
from ..crypto import scrub
from ..errors import GateError
from . import admin_api, openai_api, user_api
from .security import BodyLimit, SecurityHeaders

log = logging.getLogger("apigate")
STATIC = Path(__file__).resolve().parent.parent / "static"


def create_app(gw: Gateway) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = not gw.started
        if owned:
            await gw.start()
        try:
            yield
        finally:
            if owned:
                await gw.stop()

    app = FastAPI(title="apigate", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gw = gw

    @app.exception_handler(GateError)
    async def gate_error(request: Request, exc: GateError):
        headers = {"Retry-After": str(max(1, int(exc.retry_after + 0.999)))} if exc.retry_after else None
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        e = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        return JSONResponse(GateError(400, "invalid_request", f"{loc}: {e.get('msg', 'invalid value')}".strip(": ")).body(), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(GateError(exc.status_code, code, str(exc.detail)).body(), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.error("unhandled error %s on %s %s: %s", rid, request.method, request.url.path, scrub(repr(exc)), exc_info=True)
        return JSONResponse(GateError(500, "internal_error", f"Internal error (request {rid})").body(), status_code=500)

    app.include_router(openai_api.router)
    app.include_router(user_api.router)
    app.include_router(admin_api.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    s = gw.settings
    if s.cors_list:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_list, allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type"], allow_credentials=False, max_age=600)
    app.add_middleware(BodyLimit, max_bytes=s.max_body_bytes)
    app.add_middleware(SecurityHeaders, hsts=s.hsts)
    return app
