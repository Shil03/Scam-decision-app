"""Request and response models.

Validation here is the REJECT path: anything that fails these models is never scored.
Money is always a JSON string parsed to Decimal, never a float.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

SCHEMA_MAX_AMOUNT = Decimal("1000000.00")
_MONEY_RE = re.compile(r"^\d{1,9}(\.\d{1,2})?$")


def _money_string(value: object) -> object:
    if isinstance(value, str) and _MONEY_RE.fullmatch(value):
        return value
    raise ValueError('must be a decimal string with at most 2 decimal places, e.g. "2500.00"')


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")]
PositiveMoney = Annotated[Decimal, BeforeValidator(_money_string), Field(gt=0, le=SCHEMA_MAX_AMOUNT)]
NonNegativeMoney = Annotated[Decimal, BeforeValidator(_money_string), Field(ge=0, le=SCHEMA_MAX_AMOUNT)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=100_000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------- enums ----------

class Channel(str, Enum):
    MOBILE = "MOBILE"
    WEB = "WEB"


class CopStatus(str, Enum):
    MATCH = "MATCH"
    CLOSE_MATCH = "CLOSE_MATCH"
    NO_MATCH = "NO_MATCH"
    UNAVAILABLE = "UNAVAILABLE"


class RemoteAccess(str, Enum):
    DETECTED = "DETECTED"
    NOT_DETECTED = "NOT_DETECTED"
    UNAVAILABLE = "UNAVAILABLE"


class HistoryStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class Outcome(str, Enum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    HOLD = "HOLD"
    REJECT = "REJECT"


class Category(str, Enum):
    DEPENDENCY = "DEPENDENCY"
    HARD = "HARD"
    SOFT = "SOFT"
    INFO = "INFO"


class ScamCategory(str, Enum):
    REMOTE_ACCESS_SCAM = "REMOTE_ACCESS_SCAM"
    INVESTMENT_SCAM = "INVESTMENT_SCAM"
    IMPERSONATION_SCAM = "IMPERSONATION_SCAM"
    INVOICE_SCAM = "INVOICE_SCAM"
    MULE_PAYEE = "MULE_PAYEE"
    GENERAL_SCAM = "GENERAL_SCAM"


# ---------- request ----------

class Payee(_Strict):
    bsb: Annotated[str, StringConstraints(pattern=r"^\d{3}-?\d{3}$")]
    account_number: Annotated[str, StringConstraints(pattern=r"^\d{6,10}$")]
    account_name: Annotated[str, StringConstraints(min_length=1, max_length=140)]


class ConfirmationOfPayee(_Strict):
    status: CopStatus


class Session(_Strict):
    remote_access: RemoteAccess


class CustomerHistory(_Strict):
    status: HistoryStatus
    previously_paid_payee: StrictBool | None = None
    payments_last_90d: NonNegativeInt | None = None
    avg_amount_90d: NonNegativeMoney | None = None
    payees_added_last_24h: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _fields_match_status(self) -> "CustomerHistory":
        values = [self.previously_paid_payee, self.payments_last_90d,
                  self.avg_amount_90d, self.payees_added_last_24h]
        if self.status is HistoryStatus.AVAILABLE and any(v is None for v in values):
            raise ValueError("all history fields are required when status is AVAILABLE")
        if self.status is HistoryStatus.UNAVAILABLE and any(v is not None for v in values):
            raise ValueError("history fields must be omitted when status is UNAVAILABLE")
        return self


class PaymentRequest(_Strict):
    payment_id: Identifier
    customer_id: Identifier
    channel: Channel
    amount: PositiveMoney
    currency: Literal["AUD"]
    payee: Payee
    reference: Annotated[str, StringConstraints(max_length=140)] | None = None
    confirmation_of_payee: ConfirmationOfPayee
    session: Session
    customer_history: CustomerHistory


# ---------- responses ----------

class Reason(BaseModel):
    code: str
    category: Category
    message: str


class DecisionResponse(BaseModel):
    """Returned for ALLOW, WARN and HOLD. The first four fields are the minimum contract."""
    decision: Outcome
    reason_codes: list[str] = Field(min_length=1)
    ruleset_version: str
    correlation_id: str
    scam_category: ScamCategory | None
    requires_customer_confirmation: bool
    reasons: list[Reason] = Field(min_length=1)
    decision_id: str
    payment_id: str | None
    review_id: str | None
    degraded: bool
    evaluated_at: str


class FieldError(BaseModel):
    field: str
    issue: str


class RejectResponse(BaseModel):
    decision: Literal[Outcome.REJECT] = Outcome.REJECT
    correlation_id: str
    errors: list[FieldError]
