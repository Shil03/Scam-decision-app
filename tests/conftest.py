from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "rules.json"
DEFAULT_MULE_LIST = ROOT / "sample-data" / "mule_accounts.json"

# A clean payment: known payee, normal amount, CoP match, no remote access -> ALLOW.
BASE_PAYMENT: dict = {
    "payment_id": "PMT-000001",
    "customer_id": "CUST-0001",
    "channel": "MOBILE",
    "amount": "250.00",
    "currency": "AUD",
    "payee": {"bsb": "062-000", "account_number": "12345678", "account_name": "Jordan Citizen"},
    "reference": "Rent November",
    "confirmation_of_payee": {"status": "MATCH"},
    "session": {"remote_access": "NOT_DETECTED"},
    "customer_history": {
        "status": "AVAILABLE",
        "previously_paid_payee": True,
        "payments_last_90d": 12,
        "avg_amount_90d": "300.00",
        "payees_added_last_24h": 0,
    },
}


def deep_merge(base: dict, changes: dict) -> dict:
    """Return a copy of base with changes applied; a value of ... deletes the key."""
    out = copy.deepcopy(base)
    for key, value in changes.items():
        if value is ...:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def payment(**changes) -> dict:
    return deep_merge(BASE_PAYMENT, changes)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    shutil.copy(DEFAULT_CONFIG, path)
    return path


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
