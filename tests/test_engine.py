"""Engine unit tests: each rule, its boundaries, and decision precedence.

Expected outcomes come from evidence/design.md section 5, written before the code.
"""

import pytest

from app import engine as engine_module
from app.config import ConfigStore
from app.engine import evaluate, safe_evaluate
from app.mule_list import MuleResult
from app.schemas import Category, Outcome, PaymentRequest, WarningType
from tests.conftest import DEFAULT_CONFIG, payment

CONFIG = ConfigStore(DEFAULT_CONFIG).get().rules
NEW_PAYEE = {"customer_history": {"previously_paid_payee": False}}
HISTORY_UNAVAILABLE = {"customer_history": {"status": "UNAVAILABLE", "previously_paid_payee": ..., "payments_last_90d": ...,
                                            "avg_amount_90d": ..., "payees_added_last_24h": ...}}


def decide(mule: MuleResult = MuleResult.NO_MATCH, config=CONFIG, **changes):
    return evaluate(PaymentRequest.model_validate(payment(**changes)), mule, config)


def merge(*parts: dict) -> dict:
    out: dict = {}
    for part in parts:
        for k, v in part.items():
            out[k] = {**out.get(k, {}), **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@pytest.mark.parametrize("changes, mule, outcome, codes", [
    # ALLOW
    ({}, MuleResult.NO_MATCH, Outcome.ALLOW, ["NO_RISK_SIGNALS"]),
    (merge(NEW_PAYEE, {"amount": "999.99"}), MuleResult.NO_MATCH, Outcome.ALLOW, ["NO_RISK_SIGNALS"]),
    # WARN
    (merge(NEW_PAYEE, {"amount": "1000.00"}), MuleResult.NO_MATCH, Outcome.WARN, ["NEW_PAYEE_HIGH_VALUE"]),
    ({"confirmation_of_payee": {"status": "CLOSE_MATCH"}}, MuleResult.NO_MATCH, Outcome.WARN, ["COP_CLOSE_MATCH"]),
    ({"confirmation_of_payee": {"status": "NO_MATCH"}, "amount": "4999.99"}, MuleResult.NO_MATCH, Outcome.WARN, ["COP_NO_MATCH", "AMOUNT_ANOMALY"]),
    ({"confirmation_of_payee": {"status": "NO_MATCH"}}, MuleResult.NO_MATCH, Outcome.WARN, ["COP_NO_MATCH"]),
    ({"session": {"remote_access": "DETECTED"}}, MuleResult.NO_MATCH, Outcome.WARN, ["REMOTE_ACCESS_DETECTED"]),
    ({"amount": "1500.00"}, MuleResult.NO_MATCH, Outcome.WARN, ["AMOUNT_ANOMALY"]),          # exactly 5x avg 300
    ({"amount": "1499.99"}, MuleResult.NO_MATCH, Outcome.ALLOW, ["NO_RISK_SIGNALS"]),
    ({"customer_history": {"payees_added_last_24h": 3}}, MuleResult.NO_MATCH, Outcome.WARN, ["RAPID_PAYEE_ADDITIONS"]),
    ({"customer_history": {"payees_added_last_24h": 2}}, MuleResult.NO_MATCH, Outcome.ALLOW, ["NO_RISK_SIGNALS"]),
    ({"reference": "Bitcoin investment top-up"}, MuleResult.NO_MATCH, Outcome.WARN, ["SCAM_KEYWORD_IN_REFERENCE"]),
    ({"reference": "Police warrant fee"}, MuleResult.NO_MATCH, Outcome.WARN, ["SCAM_KEYWORD_IN_REFERENCE"]),
    ({"reference": "Cryptography textbook"}, MuleResult.NO_MATCH, Outcome.ALLOW, ["NO_RISK_SIGNALS"]),  # whole words only
    # HOLD: hard signals
    ({}, MuleResult.MATCH, Outcome.HOLD, ["MULE_ACCOUNT_MATCH"]),
    (merge(NEW_PAYEE, {"session": {"remote_access": "DETECTED"}}), MuleResult.NO_MATCH, Outcome.HOLD, ["REMOTE_ACCESS_NEW_PAYEE"]),
    ({"confirmation_of_payee": {"status": "NO_MATCH"}, "amount": "5000.00", "customer_history": {"avg_amount_90d": "4000.00"}},
     MuleResult.NO_MATCH, Outcome.HOLD, ["COP_NO_MATCH_HIGH_VALUE"]),
    (merge(NEW_PAYEE, {"amount": "10000.00", "customer_history": {"avg_amount_90d": "9000.00"}}),
     MuleResult.NO_MATCH, Outcome.HOLD, ["NEW_PAYEE_VERY_HIGH_VALUE"]),
    # HOLD: escalation of three soft signals
    ({"confirmation_of_payee": {"status": "CLOSE_MATCH"}, "reference": "crypto", "customer_history": {"payees_added_last_24h": 4}},
     MuleResult.NO_MATCH, Outcome.HOLD,
     ["MULTIPLE_RISK_SIGNALS", "COP_CLOSE_MATCH", "RAPID_PAYEE_ADDITIONS", "SCAM_KEYWORD_IN_REFERENCE"]),
    # HOLD: fail closed on dependencies
    ({}, MuleResult.UNAVAILABLE, Outcome.HOLD, ["MULE_LIST_UNAVAILABLE"]),
    ({"confirmation_of_payee": {"status": "UNAVAILABLE"}}, MuleResult.NO_MATCH, Outcome.HOLD, ["COP_UNAVAILABLE"]),
    ({"session": {"remote_access": "UNAVAILABLE"}}, MuleResult.NO_MATCH, Outcome.HOLD, ["REMOTE_ACCESS_UNAVAILABLE"]),
    (HISTORY_UNAVAILABLE, MuleResult.NO_MATCH, Outcome.HOLD, ["HISTORY_UNAVAILABLE"]),
    # dependency codes are listed before hard ones; other rules still run for the analyst
    ({"confirmation_of_payee": {"status": "UNAVAILABLE"}}, MuleResult.MATCH, Outcome.HOLD, ["COP_UNAVAILABLE", "MULE_ACCOUNT_MATCH"]),
    (merge(HISTORY_UNAVAILABLE, {"session": {"remote_access": "DETECTED"}}), MuleResult.NO_MATCH, Outcome.HOLD,
     ["HISTORY_UNAVAILABLE", "REMOTE_ACCESS_DETECTED"]),
])
def test_decision_table(changes, mule, outcome, codes):
    decision = decide(mule, **changes)
    assert decision.outcome is outcome
    assert decision.reason_codes == codes


def test_every_decision_has_reason_and_version():
    for mule in MuleResult:
        decision = decide(mule)
        assert decision.reasons and decision.rule_set_version == CONFIG.rule_set_version


def test_warning_type_follows_top_reason():
    assert decide(**{"reference": "ATO tax debt"}).warning_type is WarningType.IMPERSONATION_SCAM
    assert decide(MuleResult.MATCH).warning_type is WarningType.MULE_PAYEE
    assert decide().warning_type is None


def test_degraded_only_when_a_dependency_is_missing():
    assert decide(MuleResult.UNAVAILABLE).degraded
    assert not decide(MuleResult.MATCH).degraded


def test_thresholds_come_from_config():
    lowered = CONFIG.model_copy(update={"thresholds": CONFIG.thresholds.model_copy(update={"new_payee_warn_amount": 100})})
    request = merge(NEW_PAYEE, {"amount": "150.00"})
    assert decide(**request).outcome is Outcome.ALLOW
    assert decide(config=lowered, **request).outcome is Outcome.WARN


def test_unexpected_error_fails_closed(monkeypatch):
    def boom(*_):
        raise RuntimeError("account 12345678 exploded")
    monkeypatch.setattr(engine_module, "evaluate", boom)
    decision = safe_evaluate(PaymentRequest.model_validate(payment()), MuleResult.NO_MATCH, CONFIG)
    assert decision.outcome is Outcome.HOLD
    assert decision.reason_codes == ["ENGINE_ERROR"]
    assert decision.reasons[0].category is Category.DEPENDENCY
