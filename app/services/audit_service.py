"""Audit trail and review queue.

Every decision is stored with its reason codes, ruleset version and config hash.
A HOLD also gets a review_queue row, in the same transaction.
The payee account is stored only as an HMAC token plus a masked form.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from app.database import Database
from app.models.entities import DecisionRecord, RejectionRecord, ReviewRecord, insert_sql
from app.schemas import FieldError, Outcome, PaymentRequest
from app.services.decision_engine import Decision


class AuditService:
    def __init__(self, db: Database, token_key: str) -> None:
        self._db = db
        self._key = token_key.encode()

    # ---------- privacy helpers ----------

    def _hmac(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()

    def payee_token(self, request: PaymentRequest) -> str:
        return self._hmac(f"payee:{request.payee.bsb.replace('-', '')}:{request.payee.account_number}")[:20]

    def request_fingerprint(self, request: PaymentRequest) -> str:
        return self._hmac("request:" + json.dumps(request.model_dump(mode="json"), sort_keys=True))

    @staticmethod
    def mask_payee(request: PaymentRequest) -> str:
        bsb, acct = request.payee.bsb.replace("-", ""), request.payee.account_number
        return f"{bsb[:3]}-*** {'*' * (len(acct) - 3)}{acct[-3:]}"

    @staticmethod
    def signals(decision: Decision) -> dict:
        """Derived inputs the rules used; enough for audit, nothing identifying."""
        req = decision.request
        history = req.customer_history
        return {
            "confirmation_of_payee": req.confirmation_of_payee.status.value,
            "remote_access": req.session.remote_access.value,
            "history": history.status.value,
            "previously_paid_payee": history.previously_paid_payee,
            "payments_last_90d": history.payments_last_90d,
            "avg_amount_90d": str(history.avg_amount_90d) if history.avg_amount_90d is not None else None,
            "payees_added_last_24h": history.payees_added_last_24h,
            "mule_list": decision.mule_result.value if decision.mule_result else None,
            "channel": req.channel.value,
        }

    @staticmethod
    def priority(decision: Decision) -> int:
        codes = set(decision.reason_codes)
        if "MULE_ACCOUNT_MATCH" in codes or any(r.category.value == "DEPENDENCY" for r in decision.reasons):
            return 1
        if codes & {"REMOTE_ACCESS_NEW_PAYEE", "PAYEE_MISMATCH_HIGH_VALUE", "NEW_PAYEE_VERY_HIGH_VALUE"}:
            return 2
        return 3

    # ---------- lookups ----------

    def find_decision(self, payment_id: str) -> dict | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT decision_id, request_fingerprint, response FROM decisions WHERE payment_id = ?",
                               (payment_id,)).fetchone()
        return dict(row) if row else None

    def correlation_in_use(self, correlation_id: str) -> bool:
        with self._db.connect() as conn:
            return conn.execute("SELECT 1 FROM decisions WHERE correlation_id = ?", (correlation_id,)).fetchone() is not None

    # ---------- writes ----------

    def record_decision(self, decision: Decision, *, decision_id: str, review_id: str | None, config_hash: str,
                        fingerprint: str, response: dict, latency_ms: float, created_at: str) -> None:
        req = decision.request
        token, masked = self.payee_token(req), self.mask_payee(req)
        reason_codes = json.dumps(decision.reason_codes)
        record = DecisionRecord(
            decision_id=decision_id, payment_id=req.payment_id, correlation_id=decision.correlation_id,
            customer_id=req.customer_id, decision=decision.outcome.value, reason_codes=reason_codes,
            scam_category=decision.scam_category.value if decision.scam_category else None,
            ruleset_version=decision.ruleset_version, config_hash=config_hash, amount=str(req.amount),
            currency=req.currency, payee_token=token, payee_masked=masked,
            signals=json.dumps(self.signals(decision)), request_fingerprint=fingerprint,
            response=json.dumps(response), degraded=int(decision.degraded), latency_ms=round(latency_ms, 3),
            created_at=created_at,
        )
        with self._db.connect() as conn:
            conn.execute(insert_sql("decisions", record.row()), record.row())
            if decision.outcome is Outcome.HOLD:
                review = ReviewRecord(
                    review_id=review_id, decision_id=decision_id, payment_id=req.payment_id,
                    customer_id=req.customer_id, amount=str(req.amount), currency=req.currency,
                    payee_masked=masked, payee_token=token, reason_codes=reason_codes,
                    scam_category=record.scam_category, ruleset_version=decision.ruleset_version,
                    priority=self.priority(decision), status="PENDING", created_at=created_at,
                )
                conn.execute(insert_sql("review_queue", review.row()), review.row())

    def record_rejection(self, correlation_id: str, errors: tuple[FieldError, ...] | list[FieldError],
                         payment_id: str | None, created_at: str) -> None:
        record = RejectionRecord(
            rejection_id=str(uuid.uuid4()), correlation_id=correlation_id, payment_id=payment_id,
            error_fields=json.dumps([e.model_dump() for e in errors]), created_at=created_at,
        )
        with self._db.connect() as conn:
            conn.execute(insert_sql("rejections", record.row()), record.row())

    # ---------- review queue reads ----------

    _QUEUE_COLUMNS = ("review_id", "payment_id", "customer_id", "amount", "currency", "payee_masked", "payee_token",
                      "reason_codes", "scam_category", "ruleset_version", "priority", "status", "created_at")

    @staticmethod
    def _review_dict(row) -> dict:
        item = dict(row)
        item["reason_codes"] = json.loads(item["reason_codes"])
        return item

    def review_queue(self, status: str = "PENDING", limit: int = 50) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(self._QUEUE_COLUMNS)} FROM review_queue WHERE status = ? "
                "ORDER BY priority, created_at LIMIT ?", (status, limit)).fetchall()
        return [self._review_dict(r) for r in rows]

    def review_detail(self, payment_id: str) -> dict | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join('q.' + c for c in self._QUEUE_COLUMNS)}, "
                "d.decision_id, d.correlation_id, d.config_hash, d.signals, d.response, d.degraded "
                "FROM review_queue q JOIN decisions d ON d.decision_id = q.decision_id WHERE q.payment_id = ?",
                (payment_id,)).fetchone()
        if row is None:
            return None
        item = self._review_dict(row)
        item["signals"] = json.loads(item["signals"])
        item["reasons"] = json.loads(item.pop("response"))["reasons"]
        item["degraded"] = bool(item["degraded"])
        return item
