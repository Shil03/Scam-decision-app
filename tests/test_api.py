"""Integration tests: the full workflow over HTTP with a real SQLite file.

validate -> enrich -> score -> decide -> persist -> log -> queue if held.
"""

from __future__ import annotations

import json
import types
import logging
import sqlite3

import pytest

from app.api import payments as payments_api
from app.logging_setup import JsonFormatter
from tests.conftest import payment
from fastapi.testclient import TestClient
from tests.support import ApiEnv as Env

MULE_PAYEE = {"bsb": "999-001", "account_number": "11110001", "account_name": "Mule Holdings"}
NEW_PAYEE = {"customer_history": {"previously_paid_payee": False}}


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def pid(n: int) -> str:
    return f"PMT-{n:06d}"


# ---------- the four outcomes end to end ----------

def test_allow_is_returned_persisted_and_not_queued(env):
    r = env.assess(payment(payment_id=pid(1)), "corr-allow-0001")
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOW" and body["reason_codes"] == ["NO_RISK_SIGNAL"]
    assert body["ruleset_version"] == "2026.11.1" and body["correlation_id"] == "corr-allow-0001"
    assert r.headers["X-Correlation-ID"] == "corr-allow-0001"
    assert body["evaluated_at"].endswith("Z")
    [row] = env.rows("SELECT * FROM decisions")
    assert row["decision"] == "ALLOW" and json.loads(row["reason_codes"]) == ["NO_RISK_SIGNAL"]
    assert row["ruleset_version"] == "2026.11.1" and len(row["config_hash"]) == 64
    assert row["created_at"].endswith("Z")
    assert env.client.get("/review-queue").json()["count"] == 0


def test_warn_requires_customer_confirmation(env):
    body = env.assess(payment(payment_id=pid(2), reference="bitcoin investment")).json()
    assert body["decision"] == "WARN"
    assert body["requires_customer_confirmation"] is True
    assert body["scam_category"] == "INVESTMENT_SCAM"
    assert body["review_id"] is None


def test_hold_is_queued_with_only_necessary_data(env):
    body = env.assess(payment(payment_id=pid(3), payee=MULE_PAYEE)).json()
    assert body["decision"] == "HOLD" and body["reason_codes"] == ["MULE_ACCOUNT_MATCH"]
    assert body["review_id"]

    queue = env.client.get("/review-queue").json()
    assert queue["count"] == 1
    item = queue["items"][0]
    assert item["payment_id"] == pid(3) and item["review_id"] == body["review_id"]
    assert item["priority"] == 1 and item["status"] == "PENDING"
    assert item["payee_masked"] == "999-*** *****001"
    assert set(item) == {"review_id", "payment_id", "customer_id", "amount", "currency", "payee_masked",
                         "payee_token", "reason_codes", "scam_category", "ruleset_version", "priority",
                         "status", "created_at"}

    detail = env.client.get(f"/review-queue/{pid(3)}").json()
    assert detail["reasons"][0]["code"] == "MULE_ACCOUNT_MATCH"
    assert detail["signals"]["mule_list"] == "MATCH"
    assert "11110001" not in json.dumps(detail) and "Mule Holdings" not in json.dumps(detail)


def test_review_detail_unknown_or_not_held_is_404(env):
    env.assess(payment(payment_id=pid(4)))                     # ALLOW: never queued
    assert env.client.get(f"/review-queue/{pid(4)}").status_code == 404
    assert env.client.get("/review-queue/PMT-999999").status_code == 404


@pytest.mark.parametrize("raw, field", [
    (b"{not json", "body"),
    (b"[]", "body"),
    (json.dumps(payment(amount="-10.00")).encode(), "amount"),
    (json.dumps(payment(payee=...)).encode(), "payee"),
    (json.dumps(payment(extra="x")).encode(), "extra"),
])
def test_reject_is_422_never_scored_or_queued(env, raw, field):
    r = env.client.post("/payments/assess", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    body = r.json()
    assert body["decision"] == "REJECT" and "reason_codes" not in body and "ruleset_version" not in body
    assert any(e["field"].startswith(field) for e in body["errors"])
    assert env.rows("SELECT * FROM decisions") == []
    assert len(env.rows("SELECT * FROM rejections")) == 1
    assert env.client.get("/review-queue").json()["count"] == 0


def test_oversized_body_is_rejected(env):
    r = env.client.post("/payments/assess", content=b"{" + b" " * 20_000 + b"}")
    assert r.status_code == 413 and r.json()["decision"] == "REJECT"


# ---------- unique payment and correlation IDs ----------

def test_retry_with_same_payload_replays_the_stored_decision(env):
    first = env.assess(payment(payment_id=pid(5), payee=MULE_PAYEE), "corr-retry-0001")
    again = env.assess(payment(payment_id=pid(5), payee=MULE_PAYEE), "corr-retry-0001")
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    assert again.json() == first.json()
    assert len(env.rows("SELECT * FROM decisions")) == 1
    assert env.client.get("/review-queue").json()["count"] == 1


def test_same_payment_id_with_different_payload_is_rejected(env):
    env.assess(payment(payment_id=pid(6)))
    r = env.assess(payment(payment_id=pid(6), amount="9000.00"))
    assert r.status_code == 409 and r.json()["errors"][0]["field"] == "payment_id"


def test_correlation_id_cannot_be_reused_for_another_payment(env):
    env.assess(payment(payment_id=pid(7)), "corr-shared-0001")
    r = env.assess(payment(payment_id=pid(8)), "corr-shared-0001")
    assert r.status_code == 409 and r.json()["errors"][0]["field"] == "X-Correlation-ID"


def test_malformed_correlation_header_is_replaced(env):
    r = env.assess(payment(payment_id=pid(9)), "bad id with spaces")
    assert r.headers["X-Correlation-ID"] != "bad id with spaces"
    assert r.json()["correlation_id"] == r.headers["X-Correlation-ID"]


# ---------- mule-list dependency and degraded mode ----------

def test_mule_list_switched_off_holds_and_health_is_degraded(env):
    assert env.client.get("/health").json()["status"] == "ok"
    env.edit_config(lambda d: d["dependencies"].update(mule_list_enabled=False))

    body = env.assess(payment(payment_id=pid(10))).json()
    assert body["decision"] == "HOLD" and body["reason_codes"] == ["MULE_LIST_UNAVAILABLE"]
    assert body["degraded"] is True
    health = env.client.get("/health").json()
    assert health["status"] == "degraded" and health["dependencies"]["mule_list"] == "disabled"

    env.edit_config(lambda d: d["dependencies"].update(mule_list_enabled=True))
    assert env.assess(payment(payment_id=pid(11))).json()["decision"] == "ALLOW"
    assert env.client.get("/health").json()["status"] == "ok"


def test_mule_list_file_missing_holds_and_health_reports_down(env):
    env.mules.unlink()
    assert env.assess(payment(payment_id=pid(12))).json()["reason_codes"] == ["MULE_LIST_UNAVAILABLE"]
    assert env.client.get("/health").json()["dependencies"]["mule_list"] == "down"


# ---------- configuration without code change ----------

def test_threshold_change_applies_without_restart(env):
    new_payee_150 = payment(payment_id=pid(13), amount="150.00", **NEW_PAYEE)
    assert env.assess(new_payee_150).json()["decision"] == "ALLOW"

    def lower(d):
        d["thresholds"]["new_payee_warn_amount"] = "100.00"
        d["ruleset_version"] = "2026.11.2"
    env.edit_config(lower)

    body = env.assess(payment(payment_id=pid(14), amount="150.00", **NEW_PAYEE)).json()
    assert body["decision"] == "WARN" and body["reason_codes"] == ["NEW_PAYEE_HIGH_VALUE"]
    assert body["ruleset_version"] == "2026.11.2"
    rules = env.client.get("/config/rules").json()
    assert rules["ruleset_version"] == "2026.11.2" and rules["thresholds"]["new_payee_warn_amount"] == "100.00"
    versions = [r["ruleset_version"] for r in env.rows("SELECT ruleset_version FROM decisions ORDER BY created_at")]
    assert versions == ["2026.11.1", "2026.11.2"]


def test_invalid_config_edit_keeps_last_good_and_reports_degraded(env):
    env.edit_config(lambda d: d["thresholds"].update(new_payee_warn_amount="-5"))
    assert env.assess(payment(payment_id=pid(15))).json()["ruleset_version"] == "2026.11.1"
    health = env.client.get("/health").json()
    assert health["status"] == "degraded" and health["dependencies"]["config"] == "stale"
    assert env.client.get("/config/rules").json()["stale"] is True


def test_config_endpoint_exposes_no_secrets(env):
    text = env.client.get("/config/rules").text
    assert "test-key" not in text and "token" not in text.lower()


# ---------- fail closed ----------

def test_database_write_failure_holds_instead_of_allowing(env, monkeypatch):
    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(env.app.state.shield.audit, "record_decision", broken)
    r = env.assess(payment(payment_id=pid(16)))
    assert r.status_code == 200
    assert r.json()["decision"] == "HOLD" and r.json()["reason_codes"] == ["ENGINE_ERROR"]


def test_unexpected_error_holds_instead_of_500(env, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(payments_api, "assess", broken)
    r = env.assess(payment(payment_id=pid(17)))
    assert r.status_code == 200
    assert r.json()["decision"] == "HOLD" and r.json()["reason_codes"] == ["ENGINE_ERROR"]


# ---------- logging and privacy ----------

SENSITIVE = ("12345678", "Jordan Citizen", "Rent November", "11110001", "Mule Holdings")


def test_each_decision_emits_one_structured_log_line_without_sensitive_data(env, caplog):
    caplog.set_level(logging.INFO, logger="app_shield")
    env.assess(payment(payment_id=pid(18)), "corr-log-00001")
    env.assess(payment(payment_id=pid(19), payee=MULE_PAYEE), "corr-log-00002")
    env.assess(payment(payment_id=pid(20), amount="-1"), "corr-log-00003")

    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name.startswith("app_shield")]
    decisions = [l for l in lines if l["event"] == "payment_decision"]
    rejects = [l for l in lines if l["event"] == "payment_rejected"]
    assert [l["correlation_id"] for l in decisions] == ["corr-log-00001", "corr-log-00002"]
    assert rejects[0]["correlation_id"] == "corr-log-00003" and rejects[0]["error_fields"] == ["amount"]
    for line in decisions:
        assert {"decision", "reason_codes", "ruleset_version", "payee_token", "amount_band", "ts"} <= line.keys()
        assert line["ts"].endswith("Z")
    text = json.dumps(lines)
    for value in SENSITIVE:
        assert value not in text


def test_database_never_stores_raw_account_name_or_reference(env):
    env.assess(payment(payment_id=pid(21)))
    env.assess(payment(payment_id=pid(22), payee=MULE_PAYEE))
    env.assess(payment(payment_id=pid(23), payee={"account_number": "12AB"}))   # REJECT path too
    raw = b"".join(p.read_bytes() for p in env.db.parent.glob(env.db.name + "*"))
    for value in SENSITIVE:
        assert value.encode() not in raw, value


# ---------- independent review regressions ----------

def test_json_parser_recursion_error_is_reject_not_hold(env, monkeypatch):
    """Review finding F2: on Python <= 3.12 deep nesting raises RecursionError, not ValueError."""
    def deep(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")
    # Patch only the payments module's view of json; the test client still needs the real one.
    monkeypatch.setattr(payments_api, "json", types.SimpleNamespace(loads=deep, dumps=json.dumps))
    r = env.client.post("/payments/assess", content=b"[[[[]]]]", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["decision"] == "REJECT"
    assert env.rows("SELECT * FROM decisions") == []


def test_duplicate_keys_are_rejected(env):
    """Review finding F3: last-key-wins could assess a different amount from the one released."""
    raw = json.dumps(payment(payment_id=pid(30))).replace('"amount": "250.00"', '"amount": "1.00", "amount": "250.00"')
    r = env.client.post("/payments/assess", content=raw.encode(), headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["errors"] == [{"field": "amount", "issue": "duplicate key"}]


def test_safety_net_hold_emits_a_decision_log_line(env, monkeypatch, caplog):
    """Review finding F4: every decision, including the safety-net HOLD, emits payment_decision."""
    caplog.set_level(logging.INFO, logger="app_shield")
    def broken(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(payments_api, "assess", broken)
    env.assess(payment(payment_id=pid(31)), "corr-safety-0001")
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name.startswith("app_shield")]
    [decision] = [l for l in lines if l["event"] == "payment_decision"]
    assert decision["correlation_id"] == "corr-safety-0001"
    assert decision["decision"] == "HOLD" and decision["reason_codes"] == ["ENGINE_ERROR"]
    assert decision["persisted"] is False and decision["ruleset_version"] == "2026.11.1"


# ---------- assessment regressions: D1 concurrent duplicates, D2 empty mule list ----------

def _lose_the_race(env, monkeypatch):
    """Make the next pre-insert duplicate check miss an already-committed decision, exactly as a
    request does when a concurrent request commits between its check and its insert."""
    audit = env.app.state.shield.audit
    real, calls = audit.find_decision, {"n": 0}
    def find_decision(payment_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(payment_id)
    monkeypatch.setattr(audit, "find_decision", find_decision)


def test_race_loser_with_same_payload_gets_the_winners_decision(env, monkeypatch):
    first = env.assess(payment(payment_id=pid(40), payee=MULE_PAYEE), "corr-race-0001").json()
    _lose_the_race(env, monkeypatch)
    r = env.assess(payment(payment_id=pid(40), payee=MULE_PAYEE), "corr-race-0002")
    assert r.status_code == 200 and r.headers["Idempotent-Replay"] == "true"
    assert r.json() == first                                   # not an unrecorded HOLD ENGINE_ERROR
    assert len(env.rows("SELECT * FROM decisions")) == 1
    assert env.client.get("/review-queue").json()["count"] == 1


def test_race_loser_with_different_payload_gets_409(env, monkeypatch):
    env.assess(payment(payment_id=pid(41)), "corr-race-0003")
    _lose_the_race(env, monkeypatch)
    r = env.assess(payment(payment_id=pid(41), amount="999.00"), "corr-race-0004")
    assert r.status_code == 409 and r.json()["errors"][0]["field"] == "payment_id"


def test_concurrent_identical_first_attempts_all_get_one_consistent_decision(env):
    """Assessment D1: 12 simultaneous first attempts used to return 1 ALLOW + 11 HOLD ENGINE_ERROR."""
    from concurrent.futures import ThreadPoolExecutor
    body = json.dumps(payment(payment_id=pid(42))).encode()
    def fire(_):
        with TestClient(env.app) as client:
            r = client.post("/payments/assess", content=body, headers={"Content-Type": "application/json"})
            return r.status_code, r.json()["decision"], r.json()["decision_id"]
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(fire, range(12)))
    assert {(s, d) for s, d, _ in results} == {(200, "ALLOW")}, results
    assert len({decision_id for *_, decision_id in results}) == 1      # every caller saw the same decision
    assert len(env.rows("SELECT * FROM decisions")) == 1


def test_empty_mule_list_is_unavailable_not_healthy(env):
    """Assessment D2: an empty (truncated) list reported 'up' and ALLOWED a known mule account."""
    env.mules.write_text('{"accounts": []}', encoding="utf-8")
    health = env.client.get("/health").json()
    assert health["status"] == "degraded" and health["dependencies"]["mule_list"] == "down"
    body = env.assess(payment(payment_id=pid(43), payee=MULE_PAYEE)).json()
    assert body["decision"] == "HOLD" and body["reason_codes"] == ["MULE_LIST_UNAVAILABLE"]


def test_mule_list_minimum_size_is_configurable(env):
    env.edit_config(lambda d: d["dependencies"].update(mule_list_min_entries=4))   # stub has 3 entries
    assert env.client.get("/health").json()["dependencies"]["mule_list"] == "down"
    assert env.assess(payment(payment_id=pid(44))).json()["reason_codes"] == ["MULE_LIST_UNAVAILABLE"]
    env.edit_config(lambda d: d["dependencies"].update(mule_list_min_entries=3))
    assert env.client.get("/health").json()["status"] == "ok"
    assert env.assess(payment(payment_id=pid(45))).json()["decision"] == "ALLOW"
