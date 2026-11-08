"""Long-lived service objects, built once per app instance and shared by the routes."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import ConfigStore, Settings
from app.database import Database
from app.services.audit_service import AuditService
from app.services.mule_service import MuleService


@dataclass(frozen=True)
class AppState:
    settings: Settings
    config_store: ConfigStore
    mule_service: MuleService
    db: Database
    audit: AuditService

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        db = Database(settings.db_path)
        return cls(
            settings=settings,
            config_store=ConfigStore(settings.config_path),
            mule_service=MuleService(settings.mule_list_path),
            db=db,
            audit=AuditService(db, settings.token_key),
        )
