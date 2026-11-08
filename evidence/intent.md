# Intent

## Problem

Authorised push payment (APP) scams happen when customers are manipulated into sending money themselves. Fraud controls built for unauthorised activity often miss them, because the customer is genuine, the device is familiar and the login is valid. The best time to intervene is the moment before the payment is released. After that, recovery rates drop sharply.

This slice builds a decision service that sits in the outbound payment path. It assesses each payment for scam risk and returns a decision the payment channel can act on, with reasons it can explain.

## Intended users

| User | Need |
|---|---|
| Payment channel (mobile or web banking backend) | A fast, deterministic decision: allow, warn, hold or reject |
| Customer (indirectly) | Targeted friction only when there is real reason for it, with warnings relevant to the likely scam type |
| Fraud and scams analyst | A review queue of held payments with clear reason codes |
| Risk and product owners | Thresholds they can tune in config without a code release |
| Audit and compliance | A record of every decision, the inputs used and the rule version applied |

## Decision outcomes

- **ALLOW**: no meaningful risk signals.
- **WARN**: soft signals present. Show a scam warning matched to the likely scam type, then allow the payment if the customer confirms.
- **HOLD**: hard signals present, or the service cannot assess risk safely. Send to the analyst review queue.
- **REJECT**: invalid or malformed request. Nothing is assessed.

## Assumptions

- The payment channel calls this service synchronously before releasing the payment and honours the decision.
- Confirmation of Payee results, remote-access session flags and customer payment history are supplied as inputs. This slice does not compute them.
- A mule account list exists upstream. Here it is a local stub that can be switched off to simulate an outage.
- A rules-based approach with reason codes is acceptable for a first release. Model-based scoring is a later step.

## Constraints

- 3-hour build timebox.
- Runs locally with no paid API keys, private credentials or external services.
- Synthetic data only. No customer, staff or confidential bank data.
- Fails closed: if a required dependency is unavailable, the outcome is HOLD, never ALLOW.

## Out of scope

- Model training or ML-based scoring.
- Building the mule account detection that feeds the mule list.
- Customer-facing UI and warning content design.
- Analyst case management beyond a basic review queue endpoint.
- Real payment rails integration, authentication and authorisation, and cloud deployment.
- Inbound payment monitoring and recovery or reimbursement workflows.

## Success criteria

1. A payment request runs end to end locally: validate, enrich, score, decide, log, and queue the payment if held.
2. Every non-REJECT decision returns at least one reason code and the rule-set version.
3. The labelled synthetic scenario suite passes in full via a single verify command.
4. Malformed requests are rejected with a clear validation error and are never scored.
5. With the mule list dependency disabled, affected payments return HOLD and the health endpoint reports degraded mode.
6. Thresholds can be changed in config, and the change is reflected in decisions without a code change.
7. Each decision emits a structured log line with a correlation ID and contains no raw sensitive fields beyond what the review needs.
