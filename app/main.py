"""FastAPI application. Run with: uvicorn app.main:app --reload"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, errors
from app.api import admin, auth, companies, dashboard, documents, employees, leaves, reports, system, tickets
from app.config import get_settings
from app.deps import client_ip

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    s = get_settings()
    s.check_production()
    app = FastAPI(
        title="HR Leave API",
        version=__version__,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Behind Cloudflare the visitor's address is in CF-Connecting-IP.
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
        token = client_ip.set(ip)
        try:
            response = await call_next(request)
        finally:
            client_ip.reset(token)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    errors.install(app)
    for module in (system, auth, companies, employees, documents, leaves, tickets, dashboard, reports, admin):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
