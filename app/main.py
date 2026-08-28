"""Robot Resource Booking System — FastAPI application entry point.

One small server-side app: HTML UI, parsers, availability engine and booking
management, backed by a single SQLite file.
"""
from __future__ import annotations

import logging
import logging.config
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, DATABASE_URL
from app.db import init_db
from app.routes import calendar, lessons, requests, resources, search
from app.routes.common import templates
from app.security import SameOriginMiddleware

LOG_LEVEL = os.getenv("BOOKING_LOG_LEVEL", "INFO").upper()

logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s"}
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        },
        "root": {"handlers": ["console"], "level": LOG_LEVEL},
        "loggers": {"uvicorn.access": {"level": "WARNING"}},
    }
)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Robot Resource Booking System started (db=%s)", DATABASE_URL)
    yield
    log.info("Robot Resource Booking System shutting down")


app = FastAPI(
    title="Robot Resource Booking System",
    description="Internal robot booking, lesson scheduling and conflict checking.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(SameOriginMiddleware)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

app.include_router(calendar.router, tags=["calendar"])
app.include_router(requests.router, tags=["requests"])
app.include_router(lessons.router, tags=["lessons"])
app.include_router(resources.router, tags=["resources"])
app.include_router(search.router, tags=["search"])


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(404)
async def not_found(request: Request, exc) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"code": 404, "message": "Page not found."}, status_code=404
    )


@app.exception_handler(500)
async def server_error(request: Request, exc) -> HTMLResponse:
    log.exception("Unhandled error on %s", request.url.path)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"code": 500, "message": "Something went wrong. The error has been logged."},
        status_code=500,
    )
