"""OpenAPI (/docs) description of the POST /payments/assess request body. Documentation only.

The endpoint reads the raw body itself, so that malformed JSON becomes a clean REJECT rather than
FastAPI's generic error. As a result FastAPI cannot infer the body, and /docs showed no input box
("Try it out" sent an empty request). This module declares it explicitly: the PaymentRequest schema
plus the four sample payments from sample-data/requests/. Validation behaviour is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import PaymentRequest

SAMPLES = Path(__file__).resolve().parents[2] / "sample-data" / "requests"
EXAMPLES = (
    ("allow-known-payee", "ALLOW: clean payment to a known payee"),
    ("warn-payee-mismatch", "WARN: payee name does not match the account"),
    ("hold-remote-access-new-payee", "HOLD: remote access active, new payee"),
    ("reject-malformed", "REJECT: negative amount and missing payee"),
)


def _inline_refs(node: object, defs: dict) -> object:
    """Swagger UI cannot resolve Pydantic's nested $defs inside an operation, so inline them."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def assess_request_body() -> dict:
    schema = PaymentRequest.model_json_schema()
    examples = {
        name: {"summary": summary, "value": json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))}
        for name, summary in EXAMPLES
        if (SAMPLES / f"{name}.json").exists()
    }
    return {
        "requestBody": {
            "required": True,
            "description": ("One outbound payment. Pick a sample from **Examples**. To assess the same sample "
                            "again as a new payment, change `payment_id`: a repeated `payment_id` returns the "
                            "stored decision (`Idempotent-Replay: true`)."),
            "content": {"application/json": {"schema": _inline_refs(schema, schema.get("$defs", {})),
                                              "examples": examples}},
        }
    }
