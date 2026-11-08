"""Scam rules. Each rule is a small pure function: RuleContext -> RuleHit | None.

A rule whose input is unavailable returns None; the matching DEPENDENCY rule reports it.
Lists are ordered by priority: earlier rules explain the decision first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from app.config import Thresholds
from app.services.mule_service import MuleResult
from app.schemas import Category, CopStatus, HistoryStatus, PaymentRequest, RemoteAccess, ScamCategory


@dataclass(frozen=True)
class RuleHit:
    code: str
    category: Category
    message: str
    scam_category: ScamCategory | None = None


@dataclass(frozen=True)
class RuleContext:
    request: PaymentRequest
    mule: MuleResult
    thresholds: Thresholds
    keywords: dict[ScamCategory, list[str]]

    @property
    def amount(self) -> Decimal:
        return self.request.amount

    @property
    def history_available(self) -> bool:
        return self.request.customer_history.status is HistoryStatus.AVAILABLE

    @property
    def new_payee(self) -> bool | None:
        """True/False when history is available, None when unknown."""
        if not self.history_available:
            return None
        return not self.request.customer_history.previously_paid_payee

    @property
    def cop(self) -> CopStatus:
        return self.request.confirmation_of_payee.status

    @property
    def remote_access(self) -> RemoteAccess:
        return self.request.session.remote_access


Rule = Callable[[RuleContext], RuleHit | None]


# ---------- DEPENDENCY: an input needed to assess safely is missing -> HOLD ----------

def mule_list_unavailable(c: RuleContext) -> RuleHit | None:
    if c.mule is MuleResult.UNAVAILABLE:
        return RuleHit("MULE_LIST_UNAVAILABLE", Category.DEPENDENCY, "Mule account list unavailable; cannot screen payee")
    return None


def cop_unavailable(c: RuleContext) -> RuleHit | None:
    if c.cop is CopStatus.UNAVAILABLE:
        return RuleHit("COP_UNAVAILABLE", Category.DEPENDENCY, "Confirmation of Payee result unavailable")
    return None


def remote_access_unavailable(c: RuleContext) -> RuleHit | None:
    if c.remote_access is RemoteAccess.UNAVAILABLE:
        return RuleHit("REMOTE_ACCESS_UNAVAILABLE", Category.DEPENDENCY, "Remote-access session signal unavailable")
    return None


def history_unavailable(c: RuleContext) -> RuleHit | None:
    if not c.history_available:
        return RuleHit("HISTORY_UNAVAILABLE", Category.DEPENDENCY, "Customer payment history unavailable")
    return None


# ---------- HARD: strong scam indicators -> HOLD ----------

def mule_account_match(c: RuleContext) -> RuleHit | None:
    if c.mule is MuleResult.MATCH:
        return RuleHit("MULE_ACCOUNT_MATCH", Category.HARD, "Payee account is on the mule account list", ScamCategory.MULE_PAYEE)
    return None


def remote_access_new_payee(c: RuleContext) -> RuleHit | None:
    if c.remote_access is RemoteAccess.DETECTED and c.new_payee is True:
        return RuleHit("REMOTE_ACCESS_NEW_PAYEE", Category.HARD,
                       "Remote access active during a payment to a new payee", ScamCategory.REMOTE_ACCESS_SCAM)
    return None


def payee_mismatch_high_value(c: RuleContext) -> RuleHit | None:
    limit = c.thresholds.payee_mismatch_hold_amount
    if c.cop is CopStatus.NO_MATCH and c.amount >= limit:
        return RuleHit("PAYEE_MISMATCH_HIGH_VALUE", Category.HARD,
                       f"Payee name does not match the account and amount is at or above {limit}", ScamCategory.INVOICE_SCAM)
    return None


def new_payee_very_high_value(c: RuleContext) -> RuleHit | None:
    limit = c.thresholds.new_payee_hold_amount
    if c.new_payee is True and c.amount >= limit:
        return RuleHit("NEW_PAYEE_VERY_HIGH_VALUE", Category.HARD,
                       f"First payment to this payee at or above {limit}", ScamCategory.GENERAL_SCAM)
    return None


# ---------- SOFT: worth a targeted warning -> WARN ----------

def remote_access_detected(c: RuleContext) -> RuleHit | None:
    if c.remote_access is RemoteAccess.DETECTED and c.new_payee is not True:
        return RuleHit("REMOTE_ACCESS_DETECTED", Category.SOFT,
                       "Remote access active during the payment session", ScamCategory.REMOTE_ACCESS_SCAM)
    return None


def payee_mismatch(c: RuleContext) -> RuleHit | None:
    if c.cop is CopStatus.NO_MATCH and c.amount < c.thresholds.payee_mismatch_hold_amount:
        return RuleHit("PAYEE_MISMATCH", Category.SOFT, "Payee name does not match the account", ScamCategory.INVOICE_SCAM)
    return None


def payee_close_match(c: RuleContext) -> RuleHit | None:
    if c.cop is CopStatus.CLOSE_MATCH:
        return RuleHit("PAYEE_CLOSE_MATCH", Category.SOFT, "Payee name is a close but not exact match", ScamCategory.INVOICE_SCAM)
    return None


def new_payee_high_value(c: RuleContext) -> RuleHit | None:
    t = c.thresholds
    if c.new_payee is True and t.new_payee_warn_amount <= c.amount < t.new_payee_hold_amount:
        return RuleHit("NEW_PAYEE_HIGH_VALUE", Category.SOFT,
                       f"First payment to this payee at or above {t.new_payee_warn_amount}", ScamCategory.GENERAL_SCAM)
    return None


def high_value_payment(c: RuleContext) -> RuleHit | None:
    limit = c.thresholds.high_value_amount
    if c.amount >= limit:
        return RuleHit("HIGH_VALUE_PAYMENT", Category.SOFT, f"Payment amount at or above {limit}", ScamCategory.GENERAL_SCAM)
    return None


def unusual_payment(c: RuleContext) -> RuleHit | None:
    if not c.history_available:
        return None
    t, h = c.thresholds, c.request.customer_history
    if c.amount < t.unusual_payment_min_amount:
        return None
    no_baseline = h.payments_last_90d == 0 or h.avg_amount_90d == 0
    if no_baseline or c.amount >= t.unusual_payment_multiplier * h.avg_amount_90d:
        return RuleHit("UNUSUAL_PAYMENT_PATTERN", Category.SOFT,
                       f"Amount is unusual for this customer (at least {t.unusual_payment_multiplier}x the 90-day average)",
                       ScamCategory.GENERAL_SCAM)
    return None


def rapid_payee_additions(c: RuleContext) -> RuleHit | None:
    if not c.history_available:
        return None
    limit = c.thresholds.rapid_payee_additions_24h
    if c.request.customer_history.payees_added_last_24h >= limit:
        return RuleHit("RAPID_PAYEE_ADDITIONS", Category.SOFT,
                       f"{limit} or more payees added in the last 24 hours", ScamCategory.GENERAL_SCAM)
    return None


def scam_keyword_in_reference(c: RuleContext) -> RuleHit | None:
    reference = c.request.reference
    if not reference:
        return None
    for scam_type, words in c.keywords.items():
        for word in words:
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", reference, re.IGNORECASE):
                return RuleHit("SCAM_KEYWORD_IN_REFERENCE", Category.SOFT,
                               f"Payment reference contains language typical of {scam_type.value.replace('_', ' ').lower()}",
                               scam_type)
    return None


DEPENDENCY_RULES: tuple[Rule, ...] = (mule_list_unavailable, cop_unavailable, remote_access_unavailable, history_unavailable)
HARD_RULES: tuple[Rule, ...] = (mule_account_match, remote_access_new_payee, payee_mismatch_high_value, new_payee_very_high_value)
SOFT_RULES: tuple[Rule, ...] = (remote_access_detected, payee_mismatch, payee_close_match, new_payee_high_value,
                                high_value_payment, unusual_payment, rapid_payee_additions, scam_keyword_in_reference)
