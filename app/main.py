"""APP Shield service entry point.

Run:  uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, Request

from app.api import operations, payments
from app.config import Settings
from app.logging_setup import configure_logging, correlation_id_var
from app.state import AppState

_CORRELATION_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="APP Shield",
        version="0.1.0",
        description="Authorised push payment scam decision service: ALLOW, WARN, HOLD or REJECT before release.",
    )
    app.state.shield = AppState.build(settings)

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        supplied = request.headers.get("X-Correlation-ID")
        cid = supplied if supplied and _CORRELATION_RE.fullmatch(supplied) else str(uuid.uuid4())
        request.state.correlation_id = cid
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = cid
        return response

    app.include_router(payments.router)
    app.include_router(operations.router)
    return app
