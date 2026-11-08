"""Operational endpoints: health and the active (non-secret) rule configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.state import AppState

router = APIRouter(tags=["operations"])


def _state(request: Request) -> AppState:
    return request.app.state.shield


@router.get("/health", summary="Service health and dependency status")
def health(request: Request) -> dict:
    state = _state(request)
    loaded = state.config_store.get()
    mule = state.mule_service.status(enabled=loaded.rules.dependencies.mule_list_enabled)
    database = "up" if state.db.ping() else "down"
    config = "stale" if state.config_store.stale else "current"
    degraded = mule != "up" or database != "up" or config != "current"
    return {
        "status": "degraded" if degraded else "ok",
        "ruleset_version": loaded.rules.ruleset_version,
        "dependencies": {"mule_list": mule, "database": database, "config": config},
        "token_key": "dev-default" if state.settings.token_key_is_default else "configured",
    }


@router.get("/config/rules", summary="Active rule configuration (contains no secrets)")
def config_rules(request: Request) -> dict:
    state = _state(request)
    loaded = state.config_store.get()
    return {
        **loaded.rules.model_dump(mode="json"),
        "config_hash": loaded.config_hash,
        "stale": state.config_store.stale,
    }
