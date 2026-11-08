# Design

[intent.md](intent.md) is the source of truth. This document describes the slice **as implemented**. It was first written in Step 1 and corrected in Step 5 to match the code. Anything not built is marked as such.

## 1. Architecture and data flow

One FastAPI process with a deterministic rules engine at its centre. Code that does I/O (config file, mule list, SQLite, logs) sits at the edges, so the rules can be tested as plain functions.

```
 Payment channel (mobile or web banking backend; out of scope)
      │  POST /payments/assess        X-Correlation-ID (optional)
      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ main.py           correlation-ID middleware (accept a valid header or make a UUID)  │
│ api/payments.py   body ≤ 16 KB → JSON parse ──── fail ───────────────▶ REJECT 413/422 │
│   1. VALIDATE  services/validator.py   strict schema ── fail ───────▶ REJECT 422     │
│   2. ENRICH    services/enrichment.py → services/mule_service.py (switchable stub)   │
│   3. SCORE     services/rules.py       16 pure rules → RuleHit[]                     │
│   4. DECIDE    services/decision_engine.py   precedence → ALLOW | WARN | HOLD        │
│   5. PERSIST   services/audit_service.py → database.py   decision (+ queue row if HOLD)│
│   6. LOG       logging_setup.py   one JSON line, fields passed explicitly            │
│ any exception in 2–5 ───────────────────────────────────────────▶ HOLD ENGINE_ERROR │
└────────────────────────────────────────────────────────────────────────────┘
      ▲                           ▲                              ▲
 config/rules.json        sample-data/mule_accounts.json     data/app_shield.db
 (reloaded on change)     (synthetic stub)                   (SQLite, WAL)
```

### Component boundaries

| Module | Responsibility | I/O? |
|---|---|---|
| `app/main.py` | App factory (`create_app`), correlation-ID middleware, router wiring | no |
| `app/state.py` | Builds the long-lived objects once per app: config store, mule service, database, audit service | no |
| `app/api/payments.py` | `POST /payments/assess`, review-queue endpoints, the fail-closed safety net, the log line | via services |
| `app/api/operations.py` | `GET /health`, `GET /config/rules` | via services |
| `app/schemas.py` | Request and response models (Pydantic v2, `extra="forbid"`, money as decimal strings) | no |
| `app/config.py` | `RuleConfig` model, `ConfigStore` (reload on change, last-known-good), `Settings` from environment | reads file |
| `app/services/validator.py` | Raw payload → `PaymentRequest`, or field errors that never echo submitted values | no |
| `app/services/enrichment.py` | Fetches the service's own signals (mule list). Failures become `UNAVAILABLE`, never exceptions | via mule service |
| `app/services/mule_service.py` | Local stub of the upstream mule list; reloads on change; reports `up` / `disabled` / `down` | reads file |
| `app/services/rules.py` | One small pure function per rule | no |
| `app/services/decision_engine.py` | `assess()` pipeline and `evaluate()` precedence; `safe_evaluate()` fails closed | no |
| `app/services/audit_service.py` | Payee tokenising and masking, duplicate checks, transactional writes, review-queue reads | SQLite |
| `app/database.py`, `app/models/entities.py` | Schema, connections, record types | SQLite |
| `app/logging_setup.py` | JSON formatter, correlation-ID context, UTC timestamps | stdout |

The service **does not compute** Confirmation of Payee, remote-access flags or customer history. The channel supplies them, as the intent assumes. The only signal the service fetches itself is the mule-list lookup.

## 2. API contracts

| Method | Path | Purpose | Responses |
|---|---|---|---|
| POST | `/payments/assess` | Assess one payment before release | 200 ALLOW / WARN / HOLD · 422 REJECT (invalid) · 409 REJECT (duplicate ID) · 413 REJECT (body > 16 KB) |
| GET | `/review-queue` | Held payments awaiting review, most urgent first (`?limit=1..200`, default 50) | 200 · 503 if the store is down |
| GET | `/review-queue/{payment_id}` | One held payment: reasons, signals used, ruleset version, config hash | 200 · 404 if not held |
| GET | `/health` | Dependency state: mule list, database, config | 200 with `status` `ok` or `degraded` |
| GET | `/config/rules` | Active thresholds, keywords, ruleset version, config hash (no secrets) | 200 |

OpenAPI documentation is served at `/docs` while the app runs.

- **ALLOW, WARN and HOLD return 200.** They are decisions, not errors; the channel acts on `decision`.
- **REJECT uses the same envelope** (`decision: "REJECT"`), so the channel parses one shape. Nothing is enriched or scored; tests count the calls to prove it.
- **Correlation ID.** A valid `X-Correlation-ID` (8–64 chars, `[A-Za-z0-9-]`) is used; otherwise a UUID4 is generated. It is echoed in the response header and body, and stored in the log line and the database row.
- **Unique IDs and retries.** `payment_id` and `correlation_id` are both `UNIQUE` in `decisions`.
  - **Same `payment_id`, same payload:** the stored response is replayed with `Idempotent-Replay: true`, so a channel retrying after a timeout gets the same answer.
  - **Same `payment_id`, different payload:** 409 REJECT.
  - **Correlation ID reused for another payment:** 409 REJECT.
- **Channel contract (documented, not enforced):** a timeout, connection error or 5xx must be treated as HOLD by the channel.

## 3. Request and response schemas

### Request

```json
{
  "payment_id": "PMT-DEMO-0003",
  "customer_id": "CUST-SCN-01",
  "channel": "MOBILE",
  "amount": "4200.00",
  "currency": "AUD",
  "payee": {"bsb": "062-000", "account_number": "12345678", "account_name": "Jordan Citizen"},
  "reference": "Invoice 4471",
  "confirmation_of_payee": {"status": "MATCH"},
  "session": {"remote_access": "DETECTED"},
  "customer_history": {
    "status": "AVAILABLE", "previously_paid_payee": false, "payments_last_90d": 12,
    "avg_amount_90d": "300.00", "payees_added_last_24h": 0
  }
}
```

| Field | Rule (a failure means REJECT) |
|---|---|
| `payment_id`, `customer_id` | required, 1–64 chars, `[A-Za-z0-9-]` |
| `channel` | `MOBILE` or `WEB` |
| `amount` | **JSON string**, parsed to `Decimal`; > 0, at most 2 dp, ≤ 1,000,000. A JSON number is rejected so floats never touch money |
| `currency` | `AUD` only |
| `payee.bsb` / `payee.account_number` / `payee.account_name` | `^\d{3}-?\d{3}$` / `^\d{6,10}$` / 1–140 chars |
| `reference` | optional, ≤ 140 chars |
| `confirmation_of_payee.status` | `MATCH`, `CLOSE_MATCH`, `NO_MATCH`, `UNAVAILABLE` |
| `session.remote_access` | `DETECTED`, `NOT_DETECTED`, `UNAVAILABLE` |
| `customer_history` | `status: AVAILABLE` with all four fields (strict bool/int, non-negative), or `status: UNAVAILABLE` with none |
| anything else | unknown fields are rejected |

**Missing vs unavailable.** A missing block is a malformed request: REJECT. A block sent as `UNAVAILABLE` means the upstream dependency failed: HOLD. This separates "the caller sent something wrong" from "a dependency is down".

### Response (ALLOW / WARN / HOLD)

`decision`, `reason_codes` (≥ 1), `ruleset_version` and `correlation_id` form the minimum contract. The response also carries:
- `scam_category`: which warning to show;
- `requires_customer_confirmation`: true only for WARN;
- `reasons[]`: code, category and message;
- `decision_id`, `payment_id`, `review_id` (HOLD only), `degraded` and `evaluated_at` (UTC).

The README has real examples.

### Response (REJECT)

`{"decision": "REJECT", "correlation_id": "...", "errors": [{"field": "amount", "issue": "Input should be greater than 0"}]}`. Errors give the field path and message only, never the submitted value.

## 4. Data model (SQLite)

| Table | One row per | Holds | Never holds |
|---|---|---|---|
| `decisions` | ALLOW / WARN / HOLD | `decision_id`, `payment_id` (UNIQUE), `correlation_id` (UNIQUE), `customer_id`, decision, `reason_codes`, `scam_category`, `ruleset_version`, `config_hash` (SHA-256 of the config file applied), amount, currency, `payee_token`, `payee_masked`, `signals`, `request_fingerprint`, stored `response` (for replay), `degraded`, `latency_ms`, `created_at` (UTC) | account number, payee name, reference |
| `review_queue` | HOLD (same transaction as the decision) | `review_id`, `decision_id` (FK), `payment_id`, `customer_id`, amount, currency, `payee_masked`, `payee_token`, `reason_codes`, `scam_category`, `ruleset_version`, `priority`, `status` = `PENDING`, `created_at` | the same |
| `rejections` | REJECT | `correlation_id`, `payment_id` (only if well formed), error field paths and messages | any submitted value |

- **`payee_token`** = HMAC-SHA256(`APP_SHIELD_TOKEN_KEY`, BSB + account), truncated to 20 hex characters. Analysts can link payments to the same payee without the account being stored.
- **`payee_masked`** looks like `062-*** *****678`.
- **`request_fingerprint`** is an HMAC of the validated request, used for idempotent replay.
- **`signals`** holds the derived inputs the rules used: CoP status, remote-access status, history status and figures, mule-list result, channel.
- **Review priority:** 1 = mule match or any dependency failure, 2 = `REMOTE_ACCESS_NEW_PAYEE` / `PAYEE_MISMATCH_HIGH_VALUE` / `NEW_PAYEE_VERY_HIGH_VALUE`, 3 = escalated soft signals.
- **Case resolution** (moving a review out of `PENDING`) is out of scope, as the intent states.

## 5. Rules, configuration and decision precedence

### Configuration: `config/rules.json`

```json
{
  "ruleset_version": "2026.11.1",
  "thresholds": {
    "new_payee_warn_amount": "1000.00",   "new_payee_hold_amount": "10000.00",
    "high_value_amount": "25000.00",      "payee_mismatch_hold_amount": "5000.00",
    "unusual_payment_multiplier": "5",    "unusual_payment_min_amount": "500.00",
    "rapid_payee_additions_24h": 3,       "soft_signal_escalation_count": 3
  },
  "reference_keywords": {
    "INVESTMENT_SCAM": ["crypto", "bitcoin", "investment", "trading"],
    "IMPERSONATION_SCAM": ["ato", "tax debt", "police", "warrant"]
  },
  "dependencies": {"mule_list_enabled": true}
}
```

- **Reload on change.** The file's modification time and size are checked on every request. A change takes effect on the next request with no restart.
- **Validation and fallback.** Bounds: amounts > 0, multiplier ≥ 1, counts ≥ 1, escalation ≥ 2, and warn amount below hold amount. An invalid edit is logged and ignored; the last good config stays active and `/health` reports `config: stale`. If no valid config exists at startup, the service will not start.
- **Audit trail.** Every decision records `ruleset_version` and `config_hash`, so audit can prove the exact thresholds even if someone forgot to bump the version.

### Rules (`app/services/rules.py`)

| Code | Category | Fires when | `scam_category` |
|---|---|---|---|
| `MULE_LIST_UNAVAILABLE` | DEPENDENCY | mule list switched off, missing, unreadable or raising | — |
| `COP_UNAVAILABLE` | DEPENDENCY | `confirmation_of_payee.status = UNAVAILABLE` | — |
| `REMOTE_ACCESS_UNAVAILABLE` | DEPENDENCY | `session.remote_access = UNAVAILABLE` | — |
| `HISTORY_UNAVAILABLE` | DEPENDENCY | `customer_history.status = UNAVAILABLE` | — |
| `ENGINE_ERROR` | DEPENDENCY | unexpected exception, or the decision could not be stored | — |
| `MULE_ACCOUNT_MATCH` | HARD | payee BSB + account on the mule list | MULE_PAYEE |
| `REMOTE_ACCESS_NEW_PAYEE` | HARD | remote access detected **and** payee not previously paid | REMOTE_ACCESS_SCAM |
| `PAYEE_MISMATCH_HIGH_VALUE` | HARD | CoP `NO_MATCH` and amount ≥ `payee_mismatch_hold_amount` | INVOICE_SCAM |
| `NEW_PAYEE_VERY_HIGH_VALUE` | HARD | new payee and amount ≥ `new_payee_hold_amount` | GENERAL_SCAM |
| `MULTIPLE_RISK_SIGNALS` | HARD | soft hits ≥ `soft_signal_escalation_count` | from first soft hit |
| `REMOTE_ACCESS_DETECTED` | SOFT | remote access detected, payee not new (or history unknown) | REMOTE_ACCESS_SCAM |
| `PAYEE_MISMATCH` | SOFT | CoP `NO_MATCH` below the hold amount | INVOICE_SCAM |
| `PAYEE_CLOSE_MATCH` | SOFT | CoP `CLOSE_MATCH` | INVOICE_SCAM |
| `NEW_PAYEE_HIGH_VALUE` | SOFT | new payee, warn amount ≤ amount < hold amount | GENERAL_SCAM |
| `HIGH_VALUE_PAYMENT` | SOFT | amount ≥ `high_value_amount`, any payee | GENERAL_SCAM |
| `UNUSUAL_PAYMENT_PATTERN` | SOFT | amount ≥ min amount and (≥ multiplier × 90-day average, or no 90-day baseline) | GENERAL_SCAM |
| `RAPID_PAYEE_ADDITIONS` | SOFT | `payees_added_last_24h` ≥ threshold | GENERAL_SCAM |
| `SCAM_KEYWORD_IN_REFERENCE` | SOFT | reference contains a configured keyword (whole word, any case) | keyword's group |
| `NO_RISK_SIGNAL` | INFO | nothing else fired | — |

All amount comparisons use `Decimal` and `≥`, and every boundary has a test. A new payee alone does not warn; friction is targeted.

### Precedence (`app/services/decision_engine.py`)

1. **Invalid → REJECT.** Stop: no enrichment, no scoring, no queue.
2. **Run every rule whose inputs are available.** A rule with an unavailable input is skipped, and its DEPENDENCY code is reported instead. Other rules still run so the analyst sees every signal on a HOLD. This changes the reasons listed, never the outcome.
3. **Escalate:** soft hits ≥ the configured count add `MULTIPLE_RISK_SIGNALS`.
4. **Decide.**
   - Any DEPENDENCY or HARD hit → **HOLD**, which is queued.
   - Otherwise any SOFT hit → **WARN**, with `requires_customer_confirmation: true`.
   - Otherwise → **ALLOW** with `NO_RISK_SIGNAL`.
5. **Order reasons:** DEPENDENCY → HARD → SOFT. `scam_category` comes from the first reason that has one.
6. **Fail closed:** any exception after validation, or a failed database write, gives **HOLD / `ENGINE_ERROR`**, never a 500. When the write failed, the log line (`persisted: false`) is the fallback audit trail.

**Owner decision:** while the mule list is unavailable, **every** payment is held (strictest reading of fail-closed). See [decisions.md](decisions.md).

## 6. Scenario suite

34 labelled synthetic scenarios live as data in `tests/scenarios/scenarios.json`: S01–S22 assessed, R01–R12 rejected. Each runs over HTTP against a fresh app and SQLite file. The runner checks the expected and actual decision and reason codes, ruleset version, correlation ID, persistence, and whether the request was scored and enriched (call counters). It writes `reports/scenario-report.md`. See [verify.md](verify.md) for results.

## 7. Logging

Every log line is one JSON object on stdout with a UTC `ts` (milliseconds, `Z`), `level`, `logger`, `event` and `correlation_id`. Fields are passed explicitly by name; the request is never dumped. A real decision line:

```json
{"ts":"2026-11-08T23:40:49.979Z","level":"INFO","logger":"app_shield.api","event":"payment_decision",
 "correlation_id":"demo-hold-0001","decision_id":"5b6a9ae8-…","payment_id":"PMT-DEMO-0003","decision":"HOLD",
 "reason_codes":["REMOTE_ACCESS_NEW_PAYEE","NEW_PAYEE_HIGH_VALUE","UNUSUAL_PAYMENT_PATTERN"],
 "scam_category":"REMOTE_ACCESS_SCAM","ruleset_version":"2026.11.1","config_hash":"29ade3d0ee34",
 "payee_token":"e237934f61835eb27272","amount_band":"1k-10k","degraded":false,
 "review_id":"3167e6aa-…","persisted":true,"latency_ms":23.56}
```

- **Never logged:** the account number, BSB, payee name, reference text or customer ID. The amount appears only as a band; the exact amount is kept in the database for review.
- **Errors** are logged by exception type only, because exception text can carry payment data.
- **Event types:** `payment_decision`, `payment_rejected`, `payment_decision_replayed`, `audit_write_failed`, `assess_failed`, plus config and mule-list warnings.

## 8. Security, privacy and operational risks

| # | Risk | Mitigation in this slice | Beyond this slice |
|---|---|---|---|
| 1 | **Fail-open bug**: an error leads to release | Safety net returns HOLD / `ENGINE_ERROR`; tests inject engine and database failures | Channel treats timeout/5xx as HOLD; alert on `ENGINE_ERROR` rate |
| 2 | **Sensitive data in logs** via validation errors, tracebacks or request dumps | Log fields passed explicitly; Pydantic `input` stripped; exception type only; tests scan logs **and raw DB files** for every scenario's account, BSB, name and reference | Central log DLP |
| 3 | **Weak payee token** (an unkeyed hash of BSB+account can be brute forced) | HMAC with `APP_SHIELD_TOKEN_KEY`; `/health` shows `token_key: dev-default` when unset | Secrets manager and key rotation |
| 4 | **Bad or tampered thresholds** | Bounds-checked schema, last-known-good, `config_hash` on every decision, `GET /config/rules` | Approval workflow, signed config, alert on version/hash change |
| 5 | **Mule-list outage holds all traffic** | `/health` degraded; `MULE_LIST_UNAVAILABLE` on each decision | Business call on outage scope; bounded stale cache |
| 6 | **Caller-supplied signals are trusted** | Out of scope per intent; documented | mTLS, signed attestations, server-side CoP |
| 7 | **No authentication/authorisation** on any endpoint, including the review queue | Out of scope per intent; review data minimised | AuthN plus analyst role-based access |
| 8 | **Synchronous latency** | No network calls in the engine; one short transaction; `latency_ms` recorded per decision | p99 SLO and load test (not done) |
| 9 | **SQLite single writer** | WAL; short transactions | Managed Postgres |
| 10 | **Retries and duplicates** | Identical retry replays; conflicting reuse gets 409. Two simultaneous first attempts: the second write hits the UNIQUE constraint and fails closed (HOLD) | Distributed idempotency store |
| 11 | **Oversized or abusive payloads** | 16 KB body cap, length caps, amount cap | Gateway rate limiting |
| 12 | **Floating-point money** | Money is a decimal string, never a JSON number; `Decimal` throughout | — |
| 13 | **Friction fatigue** | New payee alone does not warn; decision mix visible in logs | Per-rule WARN and confirm-through monitoring |
| 14 | **Data retention** | Synthetic data only; no raw account, name or reference stored | Retention and purge policy |

## 9. Technology choices and trade-offs

| Choice | Why | Trade-off accepted |
|---|---|---|
| Rules with reason codes, no ML and no fraud score | Intent scopes ML out; rules are deterministic, explainable and versioned | Lower recall on novel patterns. A model could later add a signal behind the same contract |
| Python 3.11+, FastAPI, Pydantic v2 | Typed validation is the REJECT path; OpenAPI for free | Slower than JVM/Go; fine with no network calls in the engine |
| SQLite (stdlib) | Runs from a clean checkout with no setup | Single writer, not highly available; plain SQL ports to Postgres |
| JSON config reloaded on change | Tune without a release; stdlib only (PyYAML was not installed) | No approval workflow; becomes a versioned config service in production |
| Signals supplied by the channel | Matches the intent's assumptions | Service trusts the caller (risk 6) |
| Synchronous HTTP | Decision needed before release | Service availability is in the payment path, hence fail closed |

## 10. Likely production and cloud shape (not built)

- **Runtime:** a container image on the bank's managed container platform (e.g. EKS or AKS), at least 3 replicas across zones, an internal load balancer, mTLS from the channel.
- **Data:** managed Postgres for the three tables. Decision events also go to the enterprise event bus for analytics and case management.
- **Dependencies:** the mule list becomes an upstream call with a short timeout, a circuit breaker and a bounded cache. The fail-closed rules are unchanged.
- **Config and secrets:** thresholds in a versioned config repo, with risk-owner approval and GitOps promotion. The HMAC key in the cloud secrets manager.
- **Observability:** JSON logs to the central platform; metrics on decision mix, reason-code rates, `ENGINE_ERROR`, degraded mode and p99 latency.
- **CI/CD:** see [verify.md](verify.md) and [operate.md](operate.md). PR runs `verify.sh`, plus lint, type checks and scans. Main builds and signs an image and replays the scenarios against the container. Promotion goes shadow mode → canary → full, and rollback is by image or config revert.
