# Design

Step 1 of the build. [intent.md](intent.md) is the source of truth; this document explains how the slice meets it. Nothing here adds scope beyond the intent.

## 1. Architecture

One FastAPI process with a pure, deterministic rules engine at its centre. Everything that does I/O (config file, mule list, SQLite, logs) stays at the edges, so the engine can be tested without them.

```
 Payment channel
      │  POST /payments/assess   (X-Correlation-ID optional)
      ▼
┌──────────────────────────────────────────────────────────────────┐
│ main.py      routes, correlation-ID middleware, error handlers    │
│   1. VALIDATE   services/validator.py (strict) ── fail ─▶ REJECT  │
│   2. ENRICH     mule_list.py (local stub, can be switched off)    │
│   3. SCORE      services/rules.py  pure functions → RuleHit[]      │
│   4. DECIDE     services/decision_engine.py  precedence → Decision │
│   5. PERSIST    store.py   decisions + review_queue (if HOLD)      │
│   6. LOG        logging_setup.py  one JSON line, allowlisted       │
└──────────────────────────────────────────────────────────────────┘
      ▲                         ▲                          ▲
 config/rules.json    sample-data/mule_accounts.json   data/app_shield.db
 (reloaded on change)       (synthetic stub)              (SQLite)
```

Steps 3 and 4 never raise to the caller. Any unexpected exception in steps 2 to 5 becomes **HOLD / ENGINE_ERROR** (fail closed), never a 500 that the channel might treat as success.

### Module layout

```
app/
  main.py            app factory, routes, exception handlers, middleware
  schemas.py         request and response models (Pydantic v2, extra="forbid")
  config.py          RuleConfig model + loader that reloads on file mtime change
  mule_list.py       MuleListProvider: JSON stub, enable flag, raises when unavailable
  services/
    validator.py     raw payload → PaymentRequest or field errors (the REJECT path)
    rules.py         one small pure function per rule → RuleHit | None
    decision_engine.py  assess(): validate → mule check → evaluate → Decision; fail closed
  store.py           SQLite access: decisions, review_queue
  privacy.py         payee tokenisation (HMAC), masking
  logging_setup.py   JSON formatter, correlation-ID context var
config/rules.json    thresholds, keyword lists, rule-set version, dependency switches
sample-data/mule_accounts.json
sample-data/requests/   example ALLOW / WARN / HOLD / REJECT requests
tests/               unit tests + labelled scenario suite
verify.sh            single verify command (CI entry point)
```

Only installed dependencies are used: FastAPI, Pydantic, Uvicorn, pytest, httpx (TestClient) and the standard library (`sqlite3`, `json`, `hmac`, `logging`). The config is JSON so no YAML library is needed.

## 2. API contracts

| Method | Path | Purpose | Status codes |
|---|---|---|---|
| POST | `/payments/assess` | Assess one payment before release | 200 for ALLOW, WARN, HOLD · 422 REJECT (invalid) · 409 REJECT (duplicate ID) · 413 REJECT (body > 16 KB) |
| GET | `/review-queue` | Held payments awaiting review, by priority (`?limit=`, default 50) | 200 · 503 if the store is down |
| GET | `/review-queue/{payment_id}` | One held payment: reasons, signals used, ruleset version, config hash | 200 · 404 if not held |
| GET | `/health` | Dependency state: mule list, database, config | 200 with `ok` or `degraded` |
| GET | `/config/rules` | Active thresholds, keywords, ruleset version and config hash (no secrets) | 200 |

Design choices:

- **ALLOW, WARN and HOLD return 200.** They are decisions, not errors. The channel acts on `decision` in the body.
- **REJECT returns 422** with the same envelope (`decision: "REJECT"`), so the channel parses one shape. Invalid JSON, wrong types, missing fields, unknown fields and failed business validation (for example amount ≤ 0) all REJECT. The engine is never called.
- **Unique IDs and retries:**
  - **Same `payment_id`, same payload:** the stored decision is replayed with header `Idempotent-Replay: true`, so a channel retrying after a timeout gets the same answer even if config has changed since.
  - **Same `payment_id`, different payload:** 409 REJECT.
  - **Correlation ID already used for another payment:** 409 REJECT.
  - **Database:** both IDs are `UNIQUE` columns.
- **Correlation ID:** taken from the `X-Correlation-ID` header if present and well formed (≤ 64 chars, `[A-Za-z0-9-]`), otherwise generated (UUID4). It is echoed in the response header, the body, the log line and the DB row.
- **Channel contract:** a timeout, connection error or 5xx from this service must be treated as HOLD by the channel. This is documented, not enforced, because the channel is out of scope.

## 3. Request and response schemas

### Request: `POST /payments/assess`

```json
{
  "payment_id": "PMT-000123",
  "customer_id": "CUST-0042",
  "channel": "MOBILE",
  "amount": "2500.00",
  "currency": "AUD",
  "payee": {
    "bsb": "062-000",
    "account_number": "12345678",
    "account_name": "J Citizen"
  },
  "reference": "Invoice 4471",
  "confirmation_of_payee": { "status": "MATCH" },
  "session": { "remote_access": "NOT_DETECTED" },
  "customer_history": {
    "status": "AVAILABLE",
    "previously_paid_payee": false,
    "payments_last_90d": 14,
    "avg_amount_90d": "320.00",
    "payees_added_last_24h": 1
  }
}
```

Validation (failures REJECT):

| Field | Rule |
|---|---|
| `payment_id`, `customer_id` | required, 1–64 chars, `[A-Za-z0-9-]` |
| `channel` | `MOBILE` or `WEB` |
| `amount` | JSON string parsed to Decimal (never float), > 0, at most 2 dp, ≤ 1,000,000 (schema cap) |
| `currency` | `AUD` only in this slice |
| `payee.bsb` | `^\d{3}-?\d{3}$` |
| `payee.account_number` | `^\d{6,10}$` |
| `payee.account_name` | 1–140 chars |
| `reference` | optional, ≤ 140 chars |
| `confirmation_of_payee.status` | `MATCH`, `CLOSE_MATCH`, `NO_MATCH`, `UNAVAILABLE` |
| `session.remote_access` | `DETECTED`, `NOT_DETECTED`, `UNAVAILABLE` |
| `customer_history.status` | `AVAILABLE` (all history fields required, non-negative) or `UNAVAILABLE` (history fields must be absent) |
| any unknown field | REJECT (`extra="forbid"`) |

**Missing versus unavailable.** A missing input block is a malformed request and is REJECTED. An input that is present with `status: UNAVAILABLE` means the upstream dependency failed, and the decision is HOLD. This keeps "the caller sent garbage" apart from "a dependency is down".

### Response: decision (200)

```json
{
  "decision": "WARN",
  "reason_codes": ["NEW_PAYEE_HIGH_VALUE"],
  "ruleset_version": "2026.11.1",
  "correlation_id": "c0ffee-…",
  "scam_category": "GENERAL_SCAM",
  "requires_customer_confirmation": true,
  "decision_id": "6f1c…",
  "payment_id": "PMT-000123",
  "reasons": [
    { "code": "NEW_PAYEE_HIGH_VALUE", "category": "SOFT",
      "message": "First payment to this payee at or above 1000.00" }
  ],
  "review_id": null,
  "degraded": false,
  "evaluated_at": "2026-11-09T00:10:00Z"
}
```

- `reasons` always has at least one entry. A clean ALLOW returns `NO_RISK_SIGNAL`.
- `scam_category` is set for WARN and HOLD and comes from the highest-priority hit. It tells the channel which scam warning to show: `REMOTE_ACCESS_SCAM`, `INVESTMENT_SCAM`, `IMPERSONATION_SCAM`, `INVOICE_SCAM`, `MULE_PAYEE` or `GENERAL_SCAM`.
- `review_id` is set only for HOLD.
- `degraded` is true when any dependency was unavailable for this decision.

### Response: REJECT (422)

```json
{
  "decision": "REJECT",
  "correlation_id": "c0ffee-…",
  "errors": [ { "field": "amount", "issue": "Input should be greater than 0" } ]
}
```

Errors list the field path and message only. Pydantic's `input` echo is stripped so raw values never come back in errors or logs. REJECT carries no reason codes or rule-set version because nothing was scored.

## 4. Database entities (SQLite)

**`decisions`**: one row per call, REJECTs included, for audit.

| Column | Notes |
|---|---|
| `decision_id` TEXT PK | UUID4 |
| `correlation_id` TEXT | indexed |
| `payment_id` TEXT NULL | null if the REJECT happened before the field parsed |
| `customer_id` TEXT NULL | synthetic, pseudonymous |
| `decision` TEXT | ALLOW / WARN / HOLD / REJECT |
| `reason_codes` TEXT | JSON array |
| `scam_category` TEXT NULL | |
| `ruleset_version` TEXT NULL | null for REJECT |
| `config_hash` TEXT NULL | SHA-256 of the config file that was applied, so audit can prove the exact thresholds even if someone forgot to bump the version |
| `amount` TEXT NULL | Decimal as a string |
| `payee_token` TEXT NULL | HMAC-SHA256(secret, bsb+account), truncated. Lets analysts link payments to the same payee without storing the account |
| `payee_masked` TEXT NULL | e.g. `062-*** ****5678` |
| `signals` TEXT | JSON of derived, non-sensitive inputs: CoP status, remote-access status, history status, new-payee flag, amount/avg ratio, mule check status |
| `validation_errors` TEXT NULL | JSON field paths and messages (REJECT only) |
| `degraded` INTEGER | 0/1 |
| `latency_ms` REAL | |
| `created_at` TEXT | UTC ISO-8601 |

**`review_queue`**: one row per HOLD.

| Column | Notes |
|---|---|
| `review_id` TEXT PK | UUID4 |
| `decision_id` TEXT FK → decisions | unique |
| `payment_id`, `customer_id`, `amount`, `payee_masked` | denormalised for the queue view |
| `reason_codes` TEXT | JSON |
| `priority` INTEGER | 1 = mule match or dependency failure, 2 = other hard signal, 3 = escalated soft signals |
| `status` TEXT | `PENDING` (resolution workflow is out of scope) |
| `created_at` TEXT | |

The decision row and the queue row are written in a single transaction. The raw account number, account name and reference are **never** persisted.

**`rejections`**: one row per REJECT. It stores `correlation_id`, `payment_id` (only if well formed) and error field paths and messages, never the submitted values.

The mule list is a JSON file, not a table, because it stands in for an upstream service and must be switchable off.

## 5. Rules, configuration and decision precedence

### Configuration (`config/rules.json`)

```json
{
  "ruleset_version": "2026.11.1",
  "thresholds": {
    "new_payee_warn_amount": "1000.00",
    "new_payee_hold_amount": "10000.00",
    "high_value_amount": "25000.00",
    "payee_mismatch_hold_amount": "5000.00",
    "unusual_payment_multiplier": "5",
    "unusual_payment_min_amount": "500.00",
    "rapid_payee_additions_24h": 3,
    "soft_signal_escalation_count": 3
  },
  "reference_keywords": {
    "INVESTMENT_SCAM": ["crypto", "bitcoin", "investment", "trading"],
    "IMPERSONATION_SCAM": ["ato", "tax debt", "police", "warrant"]
  },
  "dependencies": { "mule_list_enabled": true }
}
```

- The path comes from `APP_SHIELD_CONFIG` (default `config/rules.json`).
- The loader checks the file's mtime on each request and reloads it when it has changed. A threshold change takes effect on the next request with no restart and no code change.
- The config is validated by a Pydantic model with bounds (amounts > 0, multiplier ≥ 1, counts ≥ 1). If an edited file is invalid, the last good config stays active, an error is logged, and `/health` reports `degraded` with `config: stale`. If no valid config exists at startup, the service refuses to start.

### Rules

| Code | Category | Fires when | scam_category |
|---|---|---|---|
| `MULE_LIST_UNAVAILABLE` | DEPENDENCY | mule list disabled or failed to load | — |
| `COP_UNAVAILABLE` | DEPENDENCY | `confirmation_of_payee.status = UNAVAILABLE` | — |
| `REMOTE_ACCESS_UNAVAILABLE` | DEPENDENCY | `session.remote_access = UNAVAILABLE` | — |
| `HISTORY_UNAVAILABLE` | DEPENDENCY | `customer_history.status = UNAVAILABLE` | — |
| `ENGINE_ERROR` | DEPENDENCY | unexpected exception during enrich, score or persist | — |
| `MULE_ACCOUNT_MATCH` | HARD | payee (normalised BSB and account) is on the mule list | MULE_PAYEE |
| `REMOTE_ACCESS_NEW_PAYEE` | HARD | remote access detected **and** payee not previously paid | REMOTE_ACCESS_SCAM |
| `PAYEE_MISMATCH_HIGH_VALUE` | HARD | CoP `NO_MATCH` and amount ≥ `payee_mismatch_hold_amount` | INVOICE_SCAM |
| `NEW_PAYEE_VERY_HIGH_VALUE` | HARD | new payee and amount ≥ `new_payee_hold_amount` | GENERAL_SCAM |
| `MULTIPLE_RISK_SIGNALS` | HARD | soft hits ≥ `soft_signal_escalation_count` | from top soft hit |
| `REMOTE_ACCESS_DETECTED` | SOFT | remote access detected, payee previously paid | REMOTE_ACCESS_SCAM |
| `PAYEE_MISMATCH` | SOFT | CoP `NO_MATCH` below the hold amount | INVOICE_SCAM |
| `PAYEE_CLOSE_MATCH` | SOFT | CoP `CLOSE_MATCH` | INVOICE_SCAM |
| `NEW_PAYEE_HIGH_VALUE` | SOFT | new payee and amount ≥ `new_payee_warn_amount` | GENERAL_SCAM |
| `HIGH_VALUE_PAYMENT` | SOFT | amount ≥ `high_value_amount`, any payee | GENERAL_SCAM |
| `UNUSUAL_PAYMENT_PATTERN` | SOFT | amount ≥ `unusual_payment_min_amount` and ≥ multiplier × `avg_amount_90d` (or no payments in the last 90 days) | GENERAL_SCAM |
| `RAPID_PAYEE_ADDITIONS` | SOFT | `payees_added_last_24h` ≥ threshold | GENERAL_SCAM |
| `SCAM_KEYWORD_IN_REFERENCE` | SOFT | reference contains a configured keyword (case-insensitive, whole word) | keyword's scam type |
| `NO_RISK_SIGNAL` | INFO | no other hit | — |

All amount comparisons are `≥` and use `Decimal`. Each boundary has a test.

### Decision precedence

1. **Validation fails → REJECT.** Stop. No enrichment, no scoring, no queue. Log it and record an audit row.
2. **Evaluate every rule that can be evaluated.** Rules whose input is unavailable are skipped and replaced by their DEPENDENCY code. Other rules still run, so the analyst sees the full picture.
3. **Escalate.** If soft hits ≥ `soft_signal_escalation_count`, add `MULTIPLE_RISK_SIGNALS` (HARD).
4. **Pick the outcome from the most severe category present:**
   - any DEPENDENCY or HARD hit → **HOLD** (and queue)
   - otherwise any SOFT hit → **WARN**
   - otherwise → **ALLOW** with `NO_RISK_SIGNAL`
5. **Order reasons** DEPENDENCY → HARD → SOFT, then by a fixed rule priority. `scam_category` is taken from the first reason that has one.
6. **Persistence failure → HOLD / ENGINE_ERROR.** The service never returns ALLOW or WARN for a decision it could not record. The structured log line is then the fallback audit trail.

Overall order: **REJECT > HOLD > WARN > ALLOW.**

## 6. Synthetic scenario suite

Scenarios live in `tests/scenarios/scenarios.json`. Each entry has an id, description, request, optional config overrides, optional `mule_list_enabled`, the expected decision and the expected reason codes. They run through the real HTTP app (TestClient), a temporary SQLite file and a temporary config. The single verify command is `./verify.sh`. It runs environment checks, config validation and repository hygiene, then all tests, and writes `reports/scenario-report.md` with expected versus actual results.

| ID | Scenario | Expected |
|---|---|---|
| S01 | Known payee, normal amount, CoP match, clean session | ALLOW `NO_RISK_SIGNAL` |
| S02 | New payee, small amount, CoP match | ALLOW |
| S03 | New payee at exactly the warn threshold (boundary) | WARN `NEW_PAYEE_HIGH_VALUE` |
| S04 | CoP close match | WARN `PAYEE_CLOSE_MATCH`, INVOICE_SCAM |
| S05 | CoP no match, low amount | WARN `PAYEE_MISMATCH` |
| S06 | CoP no match at or above the hold amount | HOLD `PAYEE_MISMATCH_HIGH_VALUE`, queued |
| S07 | Remote access, known payee | WARN `REMOTE_ACCESS_DETECTED` |
| S08 | Remote access, new payee | HOLD `REMOTE_ACCESS_NEW_PAYEE` |
| S09 | Payee on mule list, everything else clean | HOLD `MULE_ACCOUNT_MATCH` |
| S10 | Amount 8× the 90-day average to a known payee | WARN `UNUSUAL_PAYMENT_PATTERN` |
| S11 | New payee at or above the hold amount | HOLD `NEW_PAYEE_VERY_HIGH_VALUE` |
| S12 | Reference "bitcoin investment" | WARN `SCAM_KEYWORD_IN_REFERENCE`, INVESTMENT_SCAM |
| S13 | Four payees added in 24 h | WARN `RAPID_PAYEE_ADDITIONS` |
| S14 | Three soft signals together | HOLD `MULTIPLE_RISK_SIGNALS` plus the three soft codes |
| S15 | Mule list switched off, clean payment | HOLD `MULE_LIST_UNAVAILABLE`, `degraded=true`, `/health` degraded |
| S16 | CoP unavailable | HOLD `COP_UNAVAILABLE` |
| S17 | Remote-access signal unavailable | HOLD `REMOTE_ACCESS_UNAVAILABLE` |
| S18 | History unavailable | HOLD `HISTORY_UNAVAILABLE` |
| S19 | Mule match and CoP unavailable | HOLD with both codes, DEPENDENCY first |
| R01–R10 | Malformed: non-JSON body, missing payee, amount 0, negative amount, 3 dp amount, float-like amount as a JSON number, USD, bad BSB, unknown field, history `UNAVAILABLE` with fields present | REJECT 422, no reason codes, engine not called (spy), not queued |

Cross-cutting tests outside the table:

- **Config change without code (success criterion 6):** S02 returns ALLOW, then the test lowers `new_payee_warn_amount` in the temporary config file and the same request returns WARN with the new `ruleset_version`.
- **Invalid config edit:** the last good config is kept and `/health` reports degraded.
- **Engine exception:** a rule is monkeypatched to raise, and the result is HOLD `ENGINE_ERROR`, not a 500.
- **DB write failure:** the result is HOLD `ENGINE_ERROR`.
- **Every non-REJECT response:** has ≥ 1 reason and a `ruleset_version`. This is asserted across the whole suite.
- **Logging:** the captured log line is valid JSON with the correlation ID. The raw account number, account name and reference text never appear in logs or in the DB file.
- **Correlation ID:** a supplied header is echoed back. A malformed header is replaced with a generated ID.

## 7. Structured log line

One line per decision, built from an explicit allowlist of fields rather than by dumping the request:

```json
{"ts":"2026-11-09T00:10:00Z","level":"INFO","event":"payment_decision",
 "correlation_id":"c0ffee-…","decision_id":"6f1c…","payment_id":"PMT-000123",
 "decision":"HOLD","reason_codes":["MULE_ACCOUNT_MATCH"],"ruleset_version":"2026.11.1",
 "config_hash":"9a3e…","payee_token":"b71d…","amount_band":"1k-10k",
 "degraded":false,"latency_ms":3.2}
```

The log has no account number, BSB, payee name, reference text or customer ID. The amount is logged as a band, and the exact amount is kept in the access-controlled DB for review.

## 8. Security and operational risks

| # | Risk | Mitigation in this slice | Beyond this slice |
|---|---|---|---|
| 1 | **Fail-open bug**: an exception or 500 leads the channel to release the payment | Catch-all turns errors into HOLD / `ENGINE_ERROR`; tests inject failures | Channel treats timeout or 5xx as HOLD; alert on `ENGINE_ERROR` rate |
| 2 | **Sensitive data leaks into logs** through Pydantic error `input`, tracebacks or `repr(request)` | Allowlisted log fields; `input` stripped from errors; no request logging; test scans logs and DB for raw values | Central log DLP scanning |
| 3 | **Weak payee tokenisation** (plain hash of a small BSB+account space can be brute forced) | HMAC with a secret from `APP_SHIELD_TOKEN_KEY`; the dev default is flagged in `/health` and the README | Key in a secrets manager, with rotation |
| 4 | **Bad or tampered thresholds** (e.g. hold amount set to 10,000,000 disables a control) | Bounds-checked config schema; last-good fallback; `config_hash` and version on every decision | Config change approval, signed config, alert on version change |
| 5 | **Mule-list outage holds all traffic** | Visible in `/health`; reason code on every affected decision | Business decision on the scope of fail-closed; stale-cache tolerance window |
| 6 | **Caller-supplied signals are trusted** (a compromised channel could send `MATCH` / `NOT_DETECTED`) | Out of scope per intent; documented | mTLS or signed upstream attestations; fetch CoP server-side |
| 7 | **No authn/authz** on the review queue, which exposes customer and payment data | Out of scope per intent; data minimised (masked payee) | AuthN plus role-based access for analysts |
| 8 | **Latency in the synchronous path** | Engine is O(rules) with no network calls; one SQLite transaction; latency recorded per decision | p99 SLO, load test, async audit writer |
| 9 | **SQLite single-writer contention** | WAL mode; short transactions | Managed RDBMS |
| 10 | **Duplicate `payment_id` or retries** could produce a different decision after a config change | Same payload replays the stored decision; a different payload gets 409 REJECT. Two simultaneous first attempts: the UNIQUE constraint rejects the second write, which then fails closed (HOLD) | Distributed idempotency store |
| 11 | **Oversized or abusive payloads** | Length caps on every string, an amount cap and a body size limit | Gateway rate limiting |
| 12 | **Floating-point money errors** at threshold boundaries | `Decimal` end to end; JSON numbers with a fraction are rejected; boundary tests | — |
| 13 | **Friction fatigue** (too many WARNs train customers to click through) | New payee alone does not warn; decision mix is visible in the logs | Monitor WARN rate and confirm-through rate per rule; tune thresholds |
| 14 | **Data retention**: the DB holds payment amounts and customer IDs | Synthetic data only; no raw account or name stored | Retention and purge policy |

## 9. Technology choices and trade-offs

| Choice | Why | Trade-off accepted |
|---|---|---|
| Rules, not ML | Intent scopes ML out. Rules are deterministic, explainable through reason codes, and auditable per version | Lower recall on novel scam patterns. Model scoring can later add a SOFT/HARD signal behind the same contract |
| FastAPI + Pydantic v2 | Typed request validation is the REJECT path, and OpenAPI docs come for free | Python latency is higher than a JVM or Go service, which is fine for a rules engine with no network calls |
| SQLite | Zero setup, so assessors can run from a clean checkout | Single writer and not highly available. The schema is plain SQL that ports to Postgres |
| JSON config reloaded on mtime | Thresholds change without a release, and only the stdlib is needed | File-based, with no approval workflow. In production this becomes a versioned config service |
| Signals supplied by the caller | Matches the intent's assumptions and keeps the slice small | The service trusts the channel (see risk 6) |
| Synchronous HTTP | The channel needs a decision before release | Availability of this service sits in the payment path, hence the fail-closed contract |

## 10. Likely production and cloud shape

This section describes what would change between this local slice and production. It is not built here.

- **Runtime:** a container image (distroless Python) on the bank's managed container platform (e.g. EKS or AKS), with at least 3 replicas across availability zones, an internal load balancer, and mTLS from the payment channel.
- **Data:** a managed Postgres for `decisions` and `review_queue`. Decision events also go to the enterprise event bus (e.g. Kafka) for analytics and case management, so audit does not depend on one database.
- **Dependencies:** the mule list becomes a call to the upstream service with a short timeout, a circuit breaker and a bounded local cache, and the fail-closed rules stay unchanged. CoP and session signals come from their own services or are signed by the channel.
- **Config:** thresholds live in a versioned config repo, with PR approval by risk owners, promoted via GitOps and hot-reloaded. Secrets such as the token HMAC key live in the cloud secrets manager.
- **Observability:** JSON logs go to the central log platform. Metrics cover the decision mix, reason-code rates, `ENGINE_ERROR` rate, degraded mode and p99 latency. Alerts fire on degraded mode and on any change to the version or config hash.
- **CI/CD:**
  - **PR:** lint, type check, unit tests, the scenario suite, dependency and secret scanning.
  - **Main:** build and sign the image, generate an SBOM, deploy to a test environment, replay scenarios against the real container.
  - **Promotion:** staged rollout. Run in shadow mode (log decisions, but the channel ignores them), then canary, then full. Rollback means redeploying the previous image or reverting the config commit. Every decision records its version and config hash, so the effect of a rollback is auditable.

## 11. Build plan (gated steps)

The repo layout follows the brief: `app/`, `tests/`, `sample-data/` and `evidence/`. Each step ends with a time-log entry, a commit and a push to the public repo.

| Step | Scope | Budget |
|---|---|---|
| 1 | Design (this document), evidence folder, agentic-workflow log started | 20 min |
| 2 | Engine: `schemas.py`, `config.py`, `mule_list.py`, `rules.py`, `engine.py` + unit tests, `requirements.txt` | 45 min |
| 3 | API, persistence and logging: `main.py`, `store.py`, `privacy.py`, `logging_setup.py`, `/health`, review queue, `sample-data/` example requests | 45 min |
| 4 | Scenario suite, cross-cutting tests, `verify.sh` (the one command CI would run: tests + config validation + a scan for sensitive values in logs) | 30 min |
| 5 | README (brief's required sections) and evidence pack: `verify.md`, `operate.md`, `decisions.md`, final pass on `agentic-workflow.md` | 30 min |

The steps total 170 minutes, leaving about 10 minutes of buffer. Packaging (the final push and submission notes) uses the separate 15-minute allowance.
