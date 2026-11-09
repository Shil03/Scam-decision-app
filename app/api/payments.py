"""Payment assessment and the analyst review queue."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from decimal import Decimal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.openapi_docs import assess_request_body
from app.logging_setup import log_event, utc_now_iso
from app.schemas import DecisionResponse, FieldError, Outcome, Reason, RejectResponse
from app.services.decision_engine import Decision, assess, fail_closed
from app.state import AppState

router = APIRouter()
log = logging.getLogger("app_shield.api")

_ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
_ID_RE = re.compile(_ID_PATTERN)
_AMOUNT_BANDS = ((Decimal("100"), "<100"), (Decimal("1000"), "100-1k"), (Decimal("10000"), "1k-10k"),
                 (Decimal("50000"), "10k-50k"))


def amount_band(amount: Decimal) -> str:
    return next((label for limit, label in _AMOUNT_BANDS if amount < limit), "50k+")


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """A repeated key is ambiguous (which amount would the channel release?), so it is malformed."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key if _ID_RE.fullmatch(key) else "body")
        out[key] = value
    return out


def _state(request: Request) -> AppState:
    return request.app.state.shield


# ---------- POST /payments/assess ----------

@router.post("/payments/assess", tags=["payments"],
             summary="Assess an outbound payment before release: ALLOW, WARN, HOLD or REJECT",
             openapi_extra=assess_request_body())
async def assess_payment(request: Request) -> JSONResponse:
    body = await request.body()
    return await run_in_threadpool(_process, _state(request), body, request.state.correlation_id)


def _process(state: AppState, body: bytes, correlation_id: str) -> JSONResponse:
    started = time.perf_counter()
    try:
        return _assess_and_record(state, body, correlation_id, started)
    except Exception as exc:  # noqa: BLE001 - last line of defence: never a 500, never ALLOW
        log_event(log, "assess_failed", logging.ERROR, correlation_id=correlation_id, error=exc.__class__.__name__)
        try:
            version = state.config_store.get().rules.ruleset_version
        except Exception:  # noqa: BLE001
            version = "unknown"
        decision, decision_id = fail_closed(correlation_id, version), str(uuid.uuid4())
        log_event(log, "payment_decision", correlation_id=correlation_id, decision_id=decision_id, payment_id=None,
                  decision=decision.outcome.value, reason_codes=decision.reason_codes, ruleset_version=version,
                  degraded=True, review_id=None, persisted=False,
                  latency_ms=round((time.perf_counter() - started) * 1000, 2))
        return JSONResponse(_response(decision, decision_id, None, utc_now_iso()))


def _assess_and_record(state: AppState, body: bytes, correlation_id: str, started: float) -> JSONResponse:
    limit = state.settings.max_body_bytes
    if len(body) > limit:
        return _reject(state, correlation_id, [FieldError(field="body", issue=f"must be at most {limit} bytes")], 413)
    try:
        payload = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKey as exc:
        return _reject(state, correlation_id, [FieldError(field=str(exc), issue="duplicate key")], 422)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _reject(state, correlation_id, [FieldError(field="body", issue="must be valid JSON")], 422)

    loaded = state.config_store.get()
    decision = assess(payload, correlation_id, loaded.rules, state.mule_service)
    if decision.outcome is Outcome.REJECT:
        return _reject(state, correlation_id, decision.errors, 422, _safe_payment_id(payload))

    req = decision.request
    decision_id, now = str(uuid.uuid4()), utc_now_iso()
    review_id = str(uuid.uuid4()) if decision.queue_for_review else None
    response = _response(decision, decision_id, review_id, now)
    persisted = True
    try:
        fingerprint = state.audit.request_fingerprint(req)
        prior = _prior_outcome(state, req.payment_id, fingerprint, correlation_id)
        if prior is not None:
            return prior
        try:
            state.audit.record_decision(
                decision, decision_id=decision_id, review_id=review_id, config_hash=loaded.config_hash,
                fingerprint=fingerprint, response=response, latency_ms=(time.perf_counter() - started) * 1000,
                created_at=now)
        except sqlite3.IntegrityError:
            # Lost a race: a concurrent request with the same payment_id or correlation ID committed
            # between the check above and this insert. Answer exactly as that request did
            # (assessment finding D1), instead of an unrecorded HOLD.
            prior = _prior_outcome(state, req.payment_id, fingerprint, correlation_id)
            if prior is not None:
                return prior
            raise
    except Exception as exc:  # noqa: BLE001 - cannot record => cannot release: HOLD
        log_event(log, "audit_write_failed", logging.ERROR, correlation_id=correlation_id, error=exc.__class__.__name__)
        decision = fail_closed(correlation_id, decision.ruleset_version, req)
        review_id, persisted = None, False
        response = _response(decision, decision_id, None, now)

    log_event(
        log, "payment_decision",
        correlation_id=correlation_id, decision_id=decision_id, payment_id=req.payment_id,
        decision=decision.outcome.value, reason_codes=decision.reason_codes,
        scam_category=response["scam_category"], ruleset_version=decision.ruleset_version,
        config_hash=loaded.config_hash[:12], payee_token=state.audit.payee_token(req),
        amount_band=amount_band(req.amount), degraded=decision.degraded, review_id=review_id,
        persisted=persisted, latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return JSONResponse(response)


def _prior_outcome(state: AppState, payment_id: str, fingerprint: str, correlation_id: str) -> JSONResponse | None:
    """The answer for a payment_id or correlation ID that already has a stored decision, else None.

    Same payment_id and payload: replay the stored response. Same payment_id with a different
    payload, or a correlation ID already used for another payment: 409 REJECT.
    """
    existing = state.audit.find_decision(payment_id)
    if existing is not None:
        if existing["request_fingerprint"] == fingerprint:
            log_event(log, "payment_decision_replayed", correlation_id=correlation_id,
                      payment_id=payment_id, decision_id=existing["decision_id"])
            return JSONResponse(json.loads(existing["response"]), headers={"Idempotent-Replay": "true"})
        return _reject(state, correlation_id, [FieldError(
            field="payment_id", issue="already assessed with a different payload")], 409, payment_id)
    if state.audit.correlation_in_use(correlation_id):
        return _reject(state, correlation_id, [FieldError(
            field="X-Correlation-ID", issue="already used for another payment")], 409, payment_id)
    return None


def _response(decision: Decision, decision_id: str, review_id: str | None, evaluated_at: str) -> dict:
    return DecisionResponse(
        decision=decision.outcome, reason_codes=decision.reason_codes, ruleset_version=decision.ruleset_version,
        correlation_id=decision.correlation_id, scam_category=decision.scam_category,
        requires_customer_confirmation=decision.requires_customer_confirmation,
        reasons=[Reason(code=r.code, category=r.category, message=r.message) for r in decision.reasons],
        decision_id=decision_id, payment_id=decision.request.payment_id if decision.request else None,
        review_id=review_id, degraded=decision.degraded, evaluated_at=evaluated_at,
    ).model_dump(mode="json")


def _safe_payment_id(payload: object) -> str | None:
    pid = payload.get("payment_id") if isinstance(payload, dict) else None
    return pid if isinstance(pid, str) and _ID_RE.fullmatch(pid) else None


def _reject(state: AppState, correlation_id: str, errors, status: int, payment_id: str | None = None) -> JSONResponse:
    errors = list(errors)
    try:
        state.audit.record_rejection(correlation_id, errors, payment_id, utc_now_iso())
    except Exception as exc:  # noqa: BLE001 - a REJECT is still a REJECT if audit is down
        log_event(log, "audit_write_failed", logging.ERROR, correlation_id=correlation_id, error=exc.__class__.__name__)
    log_event(log, "payment_rejected", correlation_id=correlation_id, payment_id=payment_id,
              error_fields=[e.field for e in errors], http_status=status)
    body = RejectResponse(correlation_id=correlation_id, errors=errors).model_dump(mode="json")
    return JSONResponse(body, status_code=status)


# ---------- review queue ----------

def _db_unavailable() -> JSONResponse:
    return JSONResponse({"detail": "review store unavailable"}, status_code=503)


@router.get("/review-queue", tags=["review"], summary="Held payments awaiting analyst review")
def review_queue(request: Request, limit: int = Query(50, ge=1, le=200)):
    try:
        items = _state(request).audit.review_queue(limit=limit)
    except sqlite3.Error:
        return _db_unavailable()
    return {"count": len(items), "items": items}


@router.get("/review-queue/{payment_id}", tags=["review"], summary="Review details for one held payment")
def review_detail(request: Request, payment_id: str = Path(pattern=_ID_PATTERN)):
    try:
        item = _state(request).audit.review_detail(payment_id)
    except sqlite3.Error:
        return _db_unavailable()
    if item is None:
        return JSONResponse({"detail": "no held payment with this payment_id"}, status_code=404)
    return item
