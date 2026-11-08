"""Shared test harness: a real app instance on temporary config, mule list and SQLite files."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import DEFAULT_CONFIG, DEFAULT_MULE_LIST, write_json


class ApiEnv:
    def __init__(self, tmp: Path):
        self.config = tmp / "rules.json"
        self.mules = tmp / "mule_accounts.json"
        self.db = tmp / "app_shield.db"
        shutil.copy(DEFAULT_CONFIG, self.config)
        shutil.copy(DEFAULT_MULE_LIST, self.mules)
        self.app = create_app(Settings(config_path=self.config, mule_list_path=self.mules,
                                       db_path=self.db, token_key="test-key"))
        self.client = TestClient(self.app)

    def assess(self, body: dict, correlation_id: str | None = None):
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else {}
        return self.client.post("/payments/assess", json=body, headers=headers)

    def edit_config(self, change) -> None:
        data = json.loads(self.config.read_text())
        change(data)
        write_json(self.config, data)
        st = self.config.stat()
        os.utime(self.config, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params)]

    def active_ruleset_version(self) -> str:
        return json.loads(self.config.read_text())["ruleset_version"]


# ---------- labelled scenario suite ----------

SCENARIO_FILE = Path(__file__).resolve().parent / "scenarios" / "scenarios.json"
DELETE = "__delete__"


def apply_changes(base: dict, changes: dict) -> dict:
    out = json.loads(json.dumps(base))
    for key, value in changes.items():
        if value == DELETE:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = apply_changes(out[key], value)
        else:
            out[key] = value
    return out


def load_scenarios() -> tuple[dict, list[dict]]:
    data = json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))
    return data["base_request"], data["scenarios"]


def scenario_body(base: dict, scenario: dict, payment_id: str) -> bytes:
    if "raw_body" in scenario:
        return scenario["raw_body"].encode()
    request = apply_changes(base, scenario.get("changes", {}))
    if "payment_id" in request:
        request["payment_id"] = payment_id
    return json.dumps(request).encode()


class CallCounter:
    """Wraps a callable and counts calls, to prove whether a request was scored or enriched."""
    def __init__(self, target):
        self.target, self.calls = target, 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.target(*args, **kwargs)
