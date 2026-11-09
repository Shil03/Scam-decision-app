"""Enrichment: signals the service fetches itself, as opposed to signals the channel supplies.

Confirmation of Payee, remote-access flags and customer history arrive in the request.
The only fetched signal in this slice is the mule-list lookup. A failing lookup is
reported as UNAVAILABLE (the engine then fails closed), never raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.config import RuleConfig
from app.schemas import PaymentRequest
from app.services.mule_service import MuleResult

log = logging.getLogger("app_shield.enrichment")


class MuleLookup(Protocol):
    def check(self, bsb: str, account_number: str, enabled: bool, min_entries: int = 1) -> MuleResult: ...


@dataclass(frozen=True)
class Signals:
    mule: MuleResult


def enrich(request: PaymentRequest, config: RuleConfig, mule_service: MuleLookup) -> Signals:
    try:
        mule = mule_service.check(request.payee.bsb, request.payee.account_number,
                                  enabled=config.dependencies.mule_list_enabled,
                                  min_entries=config.dependencies.mule_list_min_entries)
    except Exception as exc:  # noqa: BLE001 - an unhealthy dependency is unavailable, not fatal
        log.error("mule list check failed: %s", exc.__class__.__name__)
        mule = MuleResult.UNAVAILABLE
    return Signals(mule=mule)
