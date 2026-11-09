"""Operational endpoints: health and the active (non-secret) rule configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.state import AppState

router = APIRouter(tags=["operations"])


def _state(request: Request) -> AppState:
    return request.app.state.shield


@router.get("/health/live", summary="Liveness: the process is up and answering (for restart probes)")
def live() -> dict:
    # Deliberately ignores dependencies: during a mule-list outage the service still answers every
    # payment safely (HOLD) and records it for review, so it must not be restarted for that.
    return {"status": "alive"}


@router.get("/health", summary="Service health and dependency status: HTTP 200 ok, 503 degraded")
def health(request: Request) -> JSONResponse:
    state = _state(request)
    loaded = state.config_store.get()
    deps = loaded.rules.dependencies
    mule = state.mule_service.status(enabled=deps.mule_list_enabled, min_entries=deps.mule_list_min_entries)
    database = "up" if state.db.ping() else "down"
    config = "stale" if state.config_store.stale else "current"
    degraded = mule != "up" or database != "up" or config != "current"
    body = {
        "status": "degraded" if degraded else "ok",
        "ruleset_version": loaded.rules.ruleset_version,
        "dependencies": {"mule_list": mule, "database": database, "config": config},
        "token_key": "dev-default" if state.settings.token_key_is_default else "configured",
    }
    # 503 while degraded so monitors that only read the status code see it (assessment finding D3).
    return JSONResponse(body, status_code=503 if degraded else 200)


@router.get("/config/rules", summary="Active rule configuration (contains no secrets)")
def config_rules(request: Request) -> dict:
    state = _state(request)
    loaded = state.config_store.get()
    return {
        **loaded.rules.model_dump(mode="json"),
        "config_hash": loaded.config_hash,
        "stale": state.config_store.stale,
    }
