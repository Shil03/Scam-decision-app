"""Persisted records. Only what audit and review need: no account number, payee name or reference."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    payment_id: str
    correlation_id: str
    customer_id: str
    decision: str
    reason_codes: str
    scam_category: str | None
    ruleset_version: str
    config_hash: str
    amount: str
    currency: str
    payee_token: str
    payee_masked: str
    signals: str
    request_fingerprint: str
    response: str
    degraded: int
    latency_ms: float
    created_at: str

    def row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    decision_id: str
    payment_id: str
    customer_id: str
    amount: str
    currency: str
    payee_masked: str
    payee_token: str
    reason_codes: str
    scam_category: str | None
    ruleset_version: str
    priority: int
    status: str
    created_at: str

    def row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RejectionRecord:
    rejection_id: str
    correlation_id: str
    payment_id: str | None
    error_fields: str
    created_at: str

    def row(self) -> dict:
        return asdict(self)


def insert_sql(table: str, row: dict) -> str:
    cols = ", ".join(row)
    params = ", ".join(f":{c}" for c in row)
    return f"INSERT INTO {table} ({cols}) VALUES ({params})"
