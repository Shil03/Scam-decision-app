"""Request validation: the REJECT path.

Turns a raw JSON payload into a PaymentRequest, or into field-level errors.
Errors never echo the submitted values, so they are safe to return and to log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas import FieldError, PaymentRequest


@dataclass(frozen=True)
class ValidationResult:
    request: PaymentRequest | None
    errors: tuple[FieldError, ...] = ()

    @property
    def ok(self) -> bool:
        return self.request is not None


def _field_path(loc: tuple) -> str:
    return ".".join(str(part) for part in loc) or "body"


def validate_payment(payload: Any) -> ValidationResult:
    if not isinstance(payload, dict):
        return ValidationResult(None, (FieldError(field="body", issue="must be a JSON object"),))
    try:
        return ValidationResult(PaymentRequest.model_validate(payload))
    except ValidationError as exc:
        errors = tuple(
            FieldError(field=_field_path(err["loc"]), issue=err["msg"])
            for err in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        return ValidationResult(None, errors)
