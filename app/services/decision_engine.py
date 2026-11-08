"""Decision engine: validate -> check dependencies -> hard rules -> soft rules -> decide.

Precedence (most severe wins):
  1. invalid input                       -> REJECT  (nothing else runs: no enrichment, no scoring)
  2. a required dependency unavailable   -> HOLD    (fail closed)
  3. any hard risk signal                -> HOLD
  4. any soft risk signal                -> WARN    (soft signals >= escalation count -> HOLD)
  5. nothing                             -> ALLOW   (NO_RISK_SIGNAL)
Any unexpected error after validation    -> HOLD / ENGINE_ERROR, never ALLOW.

All rules after validation are evaluated, even when the outcome is already HOLD, so the
analyst sees every signal. That changes the reasons listed, never the outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import RuleConfig
from app.services.enrichment import MuleLookup, enrich
from app.services.mule_service import MuleResult
from app.schemas import Category, FieldError, Outcome, PaymentRequest, ScamCategory
from app.services.rules import DEPENDENCY_RULES, HARD_RULES, SOFT_RULES, Rule, RuleContext, RuleHit
from app.services.validator import validate_payment

log = logging.getLogger("app_shield.engine")

NO_RISK_SIGNAL = RuleHit("NO_RISK_SIGNAL", Category.INFO, "No risk signals found")


@dataclass(frozen=True)
class Decision:
    correlation_id: str
    outcome: Outcome
    reasons: tuple[RuleHit, ...] = ()
    scam_category: ScamCategory | None = None
    ruleset_version: str | None = None
    degraded: bool = False
    errors: tuple[FieldError, ...] = ()
    request: PaymentRequest | None = field(default=None, repr=False)
    mule_result: MuleResult | None = None

    @property
    def reason_codes(self) -> list[str]:
        return [r.code for r in self.reasons]

    @property
    def requires_customer_confirmation(self) -> bool:
        """WARN: the channel shows a scam warning and proceeds only if the customer confirms."""
        return self.outcome is Outcome.WARN

    @property
    def queue_for_review(self) -> bool:
        return self.outcome is Outcome.HOLD

    def summary(self) -> dict:
        """The minimum response contract."""
        if self.outcome is Outcome.REJECT:
            return {"decision": "REJECT", "correlation_id": self.correlation_id,
                    "errors": [e.model_dump() for e in self.errors]}
        return {"decision": self.outcome.value, "reason_codes": self.reason_codes,
                "ruleset_version": self.ruleset_version, "correlation_id": self.correlation_id,
                "scam_category": self.scam_category.value if self.scam_category else None,
                "requires_customer_confirmation": self.requires_customer_confirmation}


def _run(rules: tuple[Rule, ...], ctx: RuleContext) -> list[RuleHit]:
    return [hit for rule in rules if (hit := rule(ctx)) is not None]


def evaluate(request: PaymentRequest, mule: MuleResult, config: RuleConfig, correlation_id: str) -> Decision:
    """Score an already-validated request. Pure: no I/O."""
    ctx = RuleContext(request, mule, config.thresholds, config.reference_keywords)
    dependency = _run(DEPENDENCY_RULES, ctx)
    hard = _run(HARD_RULES, ctx)
    soft = _run(SOFT_RULES, ctx)

    escalate_at = config.thresholds.soft_signal_escalation_count
    if len(soft) >= escalate_at:
        hard.append(RuleHit("MULTIPLE_RISK_SIGNALS", Category.HARD,
                            f"{len(soft)} risk signals together (escalates at {escalate_at})", soft[0].scam_category))

    if dependency or hard:
        outcome = Outcome.HOLD
    elif soft:
        outcome = Outcome.WARN
    else:
        outcome = Outcome.ALLOW

    reasons = tuple(dependency + hard + soft) or (NO_RISK_SIGNAL,)
    scam_category = next((r.scam_category for r in reasons if r.scam_category), None)
    return Decision(correlation_id, outcome, reasons, scam_category, config.ruleset_version,
                    degraded=bool(dependency), request=request, mule_result=mule)


def fail_closed(correlation_id: str, ruleset_version: str, request: PaymentRequest | None = None) -> Decision:
    hit = RuleHit("ENGINE_ERROR", Category.DEPENDENCY, "Unexpected error while assessing; held for review")
    return Decision(correlation_id, Outcome.HOLD, (hit,), None, ruleset_version, degraded=True, request=request)


def safe_evaluate(request: PaymentRequest, mule: MuleResult, config: RuleConfig, correlation_id: str) -> Decision:
    """evaluate(), but an unexpected exception becomes HOLD instead of propagating."""
    try:
        return evaluate(request, mule, config, correlation_id)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        # Log the type only: exception text could carry payment data.
        log.error("rule evaluation failed: %s", exc.__class__.__name__)
        return fail_closed(correlation_id, config.ruleset_version, request)


def assess(payload: object, correlation_id: str, config: RuleConfig, mule_service: MuleLookup) -> Decision:
    """Full pipeline for one payment: validate, enrich (mule list), score, decide."""
    validation = validate_payment(payload)
    if not validation.ok:
        return Decision(correlation_id, Outcome.REJECT, errors=validation.errors)
    signals = enrich(validation.request, config, mule_service)
    return safe_evaluate(validation.request, signals.mule, config, correlation_id)
