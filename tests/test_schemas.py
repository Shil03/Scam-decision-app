"""Malformed requests must fail validation (-> REJECT) and so are never scored."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import PaymentRequest
from tests.conftest import payment

HISTORY_UNAVAILABLE = {"status": "UNAVAILABLE", "previously_paid_payee": ..., "payments_last_90d": ...,
                       "avg_amount_90d": ..., "payees_added_last_24h": ...}


def test_clean_payment_is_valid():
    req = PaymentRequest.model_validate(payment())
    assert req.amount == Decimal("250.00")


def test_history_unavailable_without_fields_is_valid():
    PaymentRequest.model_validate(payment(customer_history=HISTORY_UNAVAILABLE))


def test_bsb_without_hyphen_is_valid():
    PaymentRequest.model_validate(payment(payee={"bsb": "062000"}))


@pytest.mark.parametrize("changes, field", [
    ({"payee": ...}, "payee"),
    ({"confirmation_of_payee": ...}, "confirmation_of_payee"),
    ({"amount": "0.00"}, "amount"),
    ({"amount": "-5.00"}, "amount"),
    ({"amount": "10.005"}, "amount"),
    ({"amount": 250.5}, "amount"),             # JSON number, not a string
    ({"amount": "1e3"}, "amount"),
    ({"amount": "NaN"}, "amount"),
    ({"amount": "1000000.01"}, "amount"),      # above schema cap
    ({"currency": "USD"}, "currency"),
    ({"channel": "ATM"}, "channel"),
    ({"payee": {"bsb": "62-000"}}, "payee.bsb"),
    ({"payee": {"account_number": "12AB5678"}}, "payee.account_number"),
    ({"payee": {"account_name": ""}}, "payee.account_name"),
    ({"payment_id": "PMT 1; DROP TABLE"}, "payment_id"),
    ({"reference": "x" * 141}, "reference"),
    ({"unexpected": "field"}, "unexpected"),
    ({"confirmation_of_payee": {"status": "maybe"}}, "confirmation_of_payee.status"),
    ({"session": {"remote_access": True}}, "session.remote_access"),
    ({"customer_history": {"previously_paid_payee": "yes"}}, "customer_history.previously_paid_payee"),
    ({"customer_history": {"payments_last_90d": "12"}}, "customer_history.payments_last_90d"),
    ({"customer_history": {"payees_added_last_24h": -1}}, "customer_history.payees_added_last_24h"),
    ({"customer_history": {"avg_amount_90d": ...}}, "customer_history"),
    ({"customer_history": {"status": "UNAVAILABLE"}}, "customer_history"),
])
def test_malformed_requests_fail_validation(changes, field):
    with pytest.raises(ValidationError) as err:
        PaymentRequest.model_validate(payment(**changes))
    locations = {".".join(str(p) for p in e["loc"]) for e in err.value.errors()}
    assert any(loc.startswith(field) for loc in locations), locations


@pytest.mark.parametrize("amount, message", [
    ("-50.00", "greater than 0"),        # negative: bound error, not a format error
    ("0.00", "greater than 0"),
    ("10.005", "at most 2 decimal places"),
    ("abc", "decimal string"),
])
def test_amount_errors_are_clear(amount, message):
    with pytest.raises(ValidationError) as err:
        PaymentRequest.model_validate(payment(amount=amount))
    assert message in err.value.errors()[0]["msg"]


@pytest.mark.parametrize("changes, field", [
    ({"payee": {"account_number": "１１１１０００１"}}, "payee.account_number"),   # fullwidth
    ({"payee": {"account_number": "١١١١٠٠٠١"}}, "payee.account_number"),       # Arabic-Indic
    ({"payee": {"bsb": "９９９-００１"}}, "payee.bsb"),
    ({"amount": "٢٥٠.٠٠"}, "amount"),
    ({"customer_history": {"avg_amount_90d": "３００.００"}}, "customer_history.avg_amount_90d"),
])
def test_non_ascii_digits_are_rejected(changes, field):
    """Review finding F1: \d matched any Unicode digit, so a mule account in fullwidth digits
    passed validation, missed the mule list and was ALLOWED."""
    with pytest.raises(ValidationError) as err:
        PaymentRequest.model_validate(payment(**changes))
    assert any(".".join(map(str, e["loc"])) == field for e in err.value.errors())
