"""Decision engine: runs the rules and applies precedence. No I/O.

Precedence: any DEPENDENCY or HARD hit -> HOLD; else any SOFT hit -> WARN; else ALLOW.
Enough SOFT hits together escalate to a HARD MULTIPLE_RISK_SIGNALS hit.
Any unexpected error -> HOLD / ENGINE_ERROR (fail closed, never ALLOW).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import RuleConfig
from app.mule_list import MuleResult
from app.rules import DEPENDENCY_RULES, HARD_RULES, SOFT_RULES, Rule, RuleContext, RuleHit
from app.schemas import Category, Outcome, PaymentRequest, WarningType

log = logging.getLogger("app_shield.engine")

NO_RISK_SIGNALS = RuleHit("NO_RISK_SIGNALS", Category.INFO, "No risk signals found")


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: tuple[RuleHit, ...]
    warning_type: WarningType | None
    rule_set_version: str
    degraded: bool

    @property
    def reason_codes(self) -> list[str]:
        return [r.code for r in self.reasons]


def _run(rules: tuple[Rule, ...], ctx: RuleContext) -> list[RuleHit]:
    return [hit for rule in rules if (hit := rule(ctx)) is not None]


def evaluate(request: PaymentRequest, mule: MuleResult, config: RuleConfig) -> Decision:
    ctx = RuleContext(request, mule, config.thresholds, config.reference_keywords)
    dependency, hard, soft = _run(DEPENDENCY_RULES, ctx), _run(HARD_RULES, ctx), _run(SOFT_RULES, ctx)

    escalate_at = config.thresholds.soft_signal_escalation_count
    if len(soft) >= escalate_at:
        hard.append(RuleHit("MULTIPLE_RISK_SIGNALS", Category.HARD,
                            f"{len(soft)} risk signals together (escalates at {escalate_at})", soft[0].warning_type))

    if dependency or hard:
        outcome = Outcome.HOLD
    elif soft:
        outcome = Outcome.WARN
    else:
        outcome = Outcome.ALLOW

    reasons = tuple(dependency + hard + soft) or (NO_RISK_SIGNALS,)
    warning_type = next((r.warning_type for r in reasons if r.warning_type), None)
    return Decision(outcome, reasons, warning_type, config.rule_set_version, degraded=bool(dependency))


def fail_closed(rule_set_version: str, message: str = "Unexpected error while assessing; held for review") -> Decision:
    hit = RuleHit("ENGINE_ERROR", Category.DEPENDENCY, message)
    return Decision(Outcome.HOLD, (hit,), None, rule_set_version, degraded=True)


def safe_evaluate(request: PaymentRequest, mule: MuleResult, config: RuleConfig) -> Decision:
    """evaluate(), but an unexpected exception becomes HOLD instead of propagating."""
    try:
        return evaluate(request, mule, config)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        # Log the type only: exception text could carry payment data.
        log.error("rule evaluation failed: %s", exc.__class__.__name__)
        return fail_closed(config.rule_set_version)
