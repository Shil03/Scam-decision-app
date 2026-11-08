"""Acceptance checks, swept across the whole labelled scenario suite where that makes sense."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from app.api import payments as payments_api
from app.logging_setup import JsonFormatter
from app.services import decision_engine
from tests.support import ApiEnv, CallCounter, apply_changes, load_scenarios, scenario_body

BASE, SCENARIOS = load_scenarios()
REJECTS = [s for s in SCENARIOS if s["expected"]["decision"] == "REJECT"]
ASSESSED = [s for s in SCENARIOS if s["expected"]["decision"] != "REJECT"]
PLAIN_ASSESSED = [s for s in ASSESSED if not s.get("config_changes")]


@pytest.fixture
def env(tmp_path) -> ApiEnv:
    return ApiEnv(tmp_path)


def post(env: ApiEnv, scenario: dict, n: int, correlation_id: str | None = None):
    headers = {"Content-Type": "application/json"}
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    return env.client.post("/payments/assess", content=scenario_body(BASE, scenario, f"PMT-ACC-{n:04d}"),
                           headers=headers)


def test_reject_never_invokes_the_risk_assessment_engine(env, monkeypatch):
    scorer = CallCounter(decision_engine.evaluate)
    enricher = CallCounter(env.app.state.shield.mule_service.check)
    monkeypatch.setattr(decision_engine, "evaluate", scorer)
    monkeypatch.setattr(env.app.state.shield.mule_service, "check", enricher)
    for n, sc in enumerate(REJECTS):
        assert post(env, sc, n).json()["decision"] == "REJECT", sc["id"]
    assert (scorer.calls, enricher.calls) == (0, 0)
    assert env.rows("SELECT * FROM decisions") == []
    assert len(env.rows("SELECT * FROM rejections")) == len(REJECTS)


def test_every_non_reject_result_has_at_least_one_reason_code(env):
    for n, sc in enumerate(PLAIN_ASSESSED):
        body = post(env, sc, n).json()
        assert body["decision"] in {"ALLOW", "WARN", "HOLD"} and len(body["reason_codes"]) >= 1, sc["id"]
        assert len(body["reasons"]) == len(body["reason_codes"])


def test_every_decision_records_the_active_ruleset_version(env):
    sample = PLAIN_ASSESSED[:9]                                    # S01-S09: includes three HOLDs
    holds = sum(s["expected"]["decision"] == "HOLD" for s in sample)
    for n, sc in enumerate(sample):
        post(env, sc, n)
    env.edit_config(lambda d: d.update(ruleset_version="2026.11.9"))
    for n, sc in enumerate(sample, start=100):
        assert post(env, sc, n).json()["ruleset_version"] == "2026.11.9"
    versions = [r["ruleset_version"] for r in env.rows("SELECT ruleset_version FROM decisions ORDER BY rowid")]
    assert versions == ["2026.11.1"] * len(sample) + ["2026.11.9"] * len(sample)
    queued = [r["ruleset_version"] for r in env.rows("SELECT ruleset_version FROM review_queue ORDER BY rowid")]
    assert holds >= 1 and queued == ["2026.11.1"] * holds + ["2026.11.9"] * holds


@pytest.mark.parametrize("failure", ["switched_off", "file_missing", "file_corrupt", "lookup_raises"])
def test_mule_list_failure_can_never_return_allow(env, monkeypatch, failure):
    if failure == "switched_off":
        env.edit_config(lambda d: d["dependencies"].update(mule_list_enabled=False))
    elif failure == "file_missing":
        env.mules.unlink()
    elif failure == "file_corrupt":
        env.mules.write_text("{ not json", encoding="utf-8")
    else:
        def raises(*_a, **_k):
            raise TimeoutError("upstream mule service timed out")
        monkeypatch.setattr(env.app.state.shield.mule_service, "check", raises)
    for n, sc in enumerate(PLAIN_ASSESSED):
        body = post(env, sc, n).json()
        assert body["decision"] == "HOLD", (failure, sc["id"], body["decision"])
        assert body["reason_codes"][0] == "MULE_LIST_UNAVAILABLE" and body["degraded"] is True


def test_health_reports_degraded_mode_when_mule_list_is_disabled(env):
    healthy = env.client.get("/health").json()
    assert healthy["status"] == "ok" and healthy["dependencies"]["mule_list"] == "up"
    env.edit_config(lambda d: d["dependencies"].update(mule_list_enabled=False))
    degraded = env.client.get("/health").json()
    assert degraded["status"] == "degraded" and degraded["dependencies"]["mule_list"] == "disabled"


def test_configuration_changes_are_reflected_without_code_changes(env):
    """Edit only config/rules.json between two identical payments; nothing else changes."""
    request = apply_changes(BASE, {"amount": "2400.00"})          # 8x the 90-day average
    first = env.assess({**request, "payment_id": "PMT-CFG-0001"}).json()
    assert (first["decision"], first["reason_codes"]) == ("WARN", ["UNUSUAL_PAYMENT_PATTERN"])

    def tune(d):
        d["ruleset_version"] = "2026.11.3"
        d["thresholds"]["unusual_payment_multiplier"] = "10"
    env.edit_config(tune)
    second = env.assess({**request, "payment_id": "PMT-CFG-0002"}).json()
    assert (second["decision"], second["reason_codes"], second["ruleset_version"]) == \
           ("ALLOW", ["NO_RISK_SIGNAL"], "2026.11.3")


def test_logs_never_expose_raw_sensitive_account_details(env, caplog):
    caplog.set_level(logging.DEBUG, logger="app_shield")
    sensitive: set[str] = set()
    for n, sc in enumerate(SCENARIOS):
        if "raw_body" not in sc:
            request = apply_changes(BASE, sc.get("changes", {}))
            payee = request.get("payee") if isinstance(request.get("payee"), dict) else {}
            sensitive |= {payee.get("account_number"), payee.get("account_name"), payee.get("bsb"),
                          request.get("reference")}
        post(env, sc, n, f"acc-log-corr-{n:04d}")
    sensitive -= {None, ""}
    lines = [JsonFormatter().format(r) for r in caplog.records if r.name.startswith("app_shield")]
    assert len([l for l in lines if '"payment_decision"' in l or '"payment_rejected"' in l]) == len(SCENARIOS)
    text = "\n".join(lines)
    leaks = sorted(v for v in sensitive if v in text)
    assert leaks == [], f"sensitive values found in logs: {leaks}"
    for line in lines:
        json.loads(line)                                           # every line is valid JSON


def test_held_payments_appear_in_the_review_queue(env):
    held = [s for s in PLAIN_ASSESSED if s["expected"]["decision"] == "HOLD"]
    for n, sc in enumerate(held):
        post(env, sc, n)
    queue = env.client.get("/review-queue").json()
    assert queue["count"] == len(held)
    by_id = {item["payment_id"]: item for item in queue["items"]}
    for n, sc in enumerate(held):
        item = by_id[f"PMT-ACC-{n:04d}"]
        assert item["reason_codes"] == sc["expected"]["reason_codes"] and item["status"] == "PENDING"
        detail = env.client.get(f"/review-queue/PMT-ACC-{n:04d}").json()
        assert [r["code"] for r in detail["reasons"]] == sc["expected"]["reason_codes"]
    priorities = [item["priority"] for item in queue["items"]]
    assert priorities == sorted(priorities)                        # most urgent first
