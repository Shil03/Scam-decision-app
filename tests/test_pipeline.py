"""assess(): validate -> check dependencies -> hard -> soft -> decide, end to end in-process."""

import pytest

from app.config import ConfigStore
from app.mule_list import MuleList, MuleResult
from app.schemas import Outcome
from app.services.decision_engine import assess
from app.services.validator import validate_payment
from tests.conftest import DEFAULT_CONFIG, DEFAULT_MULE_LIST, payment

CONFIG = ConfigStore(DEFAULT_CONFIG).get().rules
MULES = MuleList(DEFAULT_MULE_LIST)
CONTRACT_KEYS = {"decision", "reason_codes", "ruleset_version", "correlation_id"}


class SpyMuleList:
    """Records calls, so tests can prove REJECT never reaches enrichment."""
    def __init__(self, result=MuleResult.NO_MATCH, raises=False):
        self.calls, self.result, self.raises = 0, result, raises

    def check(self, *_args, **_kwargs):
        self.calls += 1
        if self.raises:
            raise ConnectionError("upstream down")
        return self.result


# ---------- REJECT: nothing is assessed ----------

@pytest.mark.parametrize("payload", [
    None, [], "not an object", 42,
    payment(amount="-1.00"),
    payment(payee=...),
    payment(currency="USD", unexpected="x"),
])
def test_invalid_input_is_rejected_without_enrichment_or_scoring(payload):
    spy = SpyMuleList()
    decision = assess(payload, "corr-1", CONFIG, spy)
    assert decision.outcome is Outcome.REJECT
    assert spy.calls == 0                       # no enrichment
    assert decision.reasons == ()               # no scoring
    assert decision.ruleset_version is None
    assert decision.errors
    assert decision.summary()["correlation_id"] == "corr-1"


def test_validation_errors_never_echo_submitted_values():
    secret_account, secret_name = "98765X321", "Z" * 200
    result = validate_payment(payment(payee={"account_number": secret_account, "account_name": secret_name},
                                      amount="12.345"))
    assert not result.ok
    text = repr([e.model_dump() for e in result.errors])
    assert secret_account not in text and secret_name not in text and "12.345" not in text
    assert {e.field for e in result.errors} == {"payee.account_number", "payee.account_name", "amount"}


# ---------- non-REJECT contract ----------

@pytest.mark.parametrize("mule, changes, outcome", [
    (MuleResult.NO_MATCH, {}, Outcome.ALLOW),
    (MuleResult.NO_MATCH, {"confirmation_of_payee": {"status": "CLOSE_MATCH"}}, Outcome.WARN),
    (MuleResult.MATCH, {}, Outcome.HOLD),
    (MuleResult.UNAVAILABLE, {}, Outcome.HOLD),
])
def test_every_non_reject_decision_meets_the_contract(mule, changes, outcome):
    decision = assess(payment(**changes), "corr-42", CONFIG, SpyMuleList(mule))
    summary = decision.summary()
    assert decision.outcome is outcome
    assert CONTRACT_KEYS <= summary.keys()
    assert summary["reason_codes"] and summary["ruleset_version"] == CONFIG.ruleset_version
    assert summary["correlation_id"] == "corr-42"


def test_flags_tell_the_channel_what_to_do():
    allow = assess(payment(), "c", CONFIG, SpyMuleList())
    warn = assess(payment(reference="crypto"), "c", CONFIG, SpyMuleList())
    hold = assess(payment(), "c", CONFIG, SpyMuleList(MuleResult.MATCH))
    assert (allow.requires_customer_confirmation, allow.queue_for_review) == (False, False)
    assert (warn.requires_customer_confirmation, warn.queue_for_review) == (True, False)
    assert (hold.requires_customer_confirmation, hold.queue_for_review) == (False, True)
    assert warn.summary()["scam_category"] == "INVESTMENT_SCAM"


# ---------- dependencies fail closed ----------

def test_mule_list_switched_off_in_config_holds():
    off = CONFIG.model_copy(update={"dependencies": CONFIG.dependencies.model_copy(update={"mule_list_enabled": False})})
    decision = assess(payment(), "c", off, MULES)
    assert decision.outcome is Outcome.HOLD and decision.reason_codes == ["MULE_LIST_UNAVAILABLE"]
    assert decision.degraded


def test_mule_list_raising_holds_instead_of_crashing():
    decision = assess(payment(), "c", CONFIG, SpyMuleList(raises=True))
    assert decision.outcome is Outcome.HOLD and decision.reason_codes == ["MULE_LIST_UNAVAILABLE"]


def test_real_stub_matches_synthetic_mule_account():
    decision = assess(payment(payee={"bsb": "999-001", "account_number": "11110001"}), "c", CONFIG, MULES)
    assert decision.outcome is Outcome.HOLD and decision.reason_codes == ["MULE_ACCOUNT_MATCH"]


# ---------- high payment amount (configurable) ----------

@pytest.mark.parametrize("amount, outcome, codes", [
    ("24999.99", Outcome.ALLOW, ["NO_RISK_SIGNAL"]),
    ("25000.00", Outcome.WARN, ["HIGH_VALUE_PAYMENT"]),
])
def test_high_value_payment_to_known_payee(amount, outcome, codes):
    history = {"customer_history": {"avg_amount_90d": "20000.00"}}   # not unusual for this customer
    decision = assess(payment(amount=amount, **history), "c", CONFIG, SpyMuleList())
    assert decision.outcome is outcome and decision.reason_codes == codes
