"""Application entrypoint: wiring, lifespan, routers, static dashboard."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .a2a.server import router as a2a_router
from .cache import backend_name as cache_backend
from .config import settings
from .db import init_db, session_scope
from .mcp.server import router as mcp_router
from .observability import ObservabilityMiddleware, configure_logging
from .routers import agent, auth, discovery, jobs, marketplace, payments, system, algokit as algokit_router, vibekit as vibekit_router, zerion as zerion_router
from .seed import seed
from .services.scheduler import scheduler
from .storage import backend_name as storage_backend
from .workers.sandbox import runner

log = logging.getLogger("m2x.main")
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    with session_scope() as db:
        report = seed(db)
    log.info("startup: %s", report)
    log.info(
        "backends -> db=%s cache=%s storage=%s sandbox=%s",
        "sqlite" if settings.is_sqlite else "postgresql",
        cache_backend(),
        storage_backend(),
        runner.backend,
    )
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "Machine-to-machine compute and tool exchange: provider marketplace, agent planner, "
        "x402 payments, Bazaar discovery, ephemeral sandbox workers, metering, SHA-256 "
        "integrity, signed receipts, reputation, scheduling, retries, refunds and disputes. "
        "External providers (Zerion Onchain Intelligence) are sold through the same paid "
        "path on their own payment rail — see ZERION.md."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(ObservabilityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-PAYMENT-RESPONSE", "X-Job-Id", "X-Payment-Id", "X-Request-ID",
                    "X-Content-SHA256", "X-Response-Time-Ms"],
)

for router in (
    system.router,
    auth.router,
    marketplace.router,
    discovery.router,
    jobs.router,
    payments.router,
    agent.router,
    algokit_router.router,
    vibekit_router.router,
    zerion_router.router,
    mcp_router,
    a2a_router,
):
    app.include_router(router)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "type": exc.__class__.__name__,
                 "request_id": getattr(request.state, "request_id", "")},
    )


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    """Zero-dependency operator console — serves dashboard.html or falls back to standalone.html."""
    primary = STATIC_DIR / "dashboard.html"
    fallback = STATIC_DIR / "standalone.html"
    if primary.exists():
        return FileResponse(primary)
    if fallback.exists():
        return FileResponse(fallback)
    return FileResponse(fallback)  # will 404 cleanly if neither exists


@app.get("/standalone", include_in_schema=False)
def standalone() -> FileResponse:
    """Serves the standalone single-file UI (standalone_app.html)."""
    path = STATIC_DIR / "standalone.html"
    if not path.exists():
        path = STATIC_DIR / "dashboard.html"
    return FileResponse(path)
