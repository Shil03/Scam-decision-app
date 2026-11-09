# APP Shield: Authorised Push Payment Scam Decision Service

APP Shield sits in the outbound payment path. The payment channel calls it **synchronously, just before releasing a payment**, and it returns a decision the channel can act on:

| Decision | Meaning | What the channel does |
|---|---|---|
| **ALLOW** | No meaningful risk signals | Release the payment |
| **WARN** | Soft signals, e.g. payee name mismatch or a new payee at high value | Show a scam warning matched to `scam_category`; release only if the customer confirms (`requires_customer_confirmation: true`) |
| **HOLD** | Hard signals, e.g. mule account or remote access with a new payee, **or the service cannot assess safely** | Do not release; the payment is in the analyst review queue |
| **REJECT** | Malformed or invalid request | Fix the request; nothing was assessed |

Authorised push payment (APP) scams happen when a genuine customer is manipulated into sending money themselves. That is why the decision is made at the last moment before release.

Every non-REJECT response carries at least one **reason code**, the **ruleset version** and a **correlation ID**. Every decision is stored for audit and every HOLD is queued for review. The one exception: if the store itself fails, the HOLD is returned and logged with `persisted: false` but cannot be queued. Each decision emits one structured JSON log line with no raw account details.

**What this is not:** there is no ML or fraud score, no authentication, no real payment rails or bank integrations, no customer UI, and no cloud deployment. All of these are out of scope (see [evidence/intent.md](evidence/intent.md)). All data is synthetic.

## Prerequisites

- **Python 3.11 or newer.** Developed on 3.15; CI runs 3.11, 3.12 and 3.13.
- **Bash** for `verify.sh`. On Windows, use Git Bash.
- **curl**, optional, for the examples.

No API keys, credentials, Docker or external services are needed.

## Setup

```bash
git clone https://github.com/Shil03/Scam-decision-app.git
cd Scam-decision-app
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run the service

```bash
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

- **API docs:** http://127.0.0.1:8000/docs
- **Health:** http://127.0.0.1:8000/health
- **Data:** decisions are written to `data/app_shield.db`, which is created automatically. Delete it to start fresh.

## Run the checks

```bash
./verify.sh                        # Windows (Git Bash): bash verify.sh
```

It runs environment, config and repository-hygiene checks, then **all 177 tests**, including the 36-scenario labelled suite. It exits non-zero on any failure and writes `reports/scenario-report.md`. The last run passed 177 tests, 36/36 scenarios, exit code 0. GitHub Actions runs the same script on every push ([.github/workflows/verify.yml](.github/workflows/verify.yml)).

## Example

Four sample requests are in [sample-data/requests/](sample-data/requests/). With the service running:

```bash
curl -s -X POST http://127.0.0.1:8000/payments/assess \
  -H "Content-Type: application/json" -H "X-Correlation-ID: demo-hold-0001" \
  --data @sample-data/requests/hold-remote-access-new-payee.json
```

That request is a $4,200 payment to a **new payee** while a **remote-access session** is active, which is a classic remote-access scam pattern. Actual response:

```json
{
  "decision": "HOLD",
  "reason_codes": ["REMOTE_ACCESS_NEW_PAYEE", "NEW_PAYEE_HIGH_VALUE", "UNUSUAL_PAYMENT_PATTERN"],
  "ruleset_version": "2026.11.1",
  "correlation_id": "demo-hold-0001",
  "scam_category": "REMOTE_ACCESS_SCAM",
  "requires_customer_confirmation": false,
  "reasons": [
    {"code": "REMOTE_ACCESS_NEW_PAYEE", "category": "HARD", "message": "Remote access active during a payment to a new payee"},
    {"code": "NEW_PAYEE_HIGH_VALUE", "category": "SOFT", "message": "First payment to this payee at or above 1000.00"},
    {"code": "UNUSUAL_PAYMENT_PATTERN", "category": "SOFT", "message": "Amount is unusual for this customer (at least 5x the 90-day average)"}
  ],
  "decision_id": "5b6a9ae8-596c-4104-9c14-8d4750d1c6ea",
  "payment_id": "PMT-DEMO-0003",
  "review_id": "3167e6aa-feed-4a59-9e01-2261766c1f16",
  "degraded": false,
  "evaluated_at": "2026-11-08T23:40:49.956Z"
}
```

`GET /review-queue` then lists it at priority 2 with a masked payee (`062-*** *****678`). The account number, payee name and reference are not stored. The log line for the decision (abridged; the full line also has `decision_id`, `review_id` and `scam_category`):

```json
{"ts":"2026-11-08T23:40:49.979Z","level":"INFO","logger":"app_shield.api","event":"payment_decision","correlation_id":"demo-hold-0001","payment_id":"PMT-DEMO-0003","decision":"HOLD","reason_codes":["REMOTE_ACCESS_NEW_PAYEE","NEW_PAYEE_HIGH_VALUE","UNUSUAL_PAYMENT_PATTERN"],"ruleset_version":"2026.11.1","config_hash":"29ade3d0ee34","payee_token":"e237934f61835eb27272","amount_band":"1k-10k","degraded":false,"persisted":true,"latency_ms":23.56}
```

The other samples give **ALLOW** (`allow-known-payee.json`), **WARN** with `PAYEE_MISMATCH` (`warn-payee-mismatch.json`) and **REJECT** HTTP 422 (`reject-malformed.json`). The REJECT errors are `amount: Input should be greater than 0` and `payee: Field required`.

**Sending the same request twice** returns the stored decision with an `Idempotent-Replay: true` header. To assess it again as a new payment, change `payment_id` in the file.

### Try the safety behaviour

- **Mule-list outage:** set `"mule_list_enabled": false` in [config/rules.json](config/rules.json). Every payment is then HELD with `MULE_LIST_UNAVAILABLE`, and `/health` reports `degraded`. No restart is needed.
- **Tune a threshold:** change any value under `thresholds`, and bump `ruleset_version`. The next decision uses it. `GET /config/rules` shows the active values.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/payments/assess` | Assess a payment: 200 ALLOW / WARN / HOLD, 422 / 409 / 413 REJECT |
| GET | `/review-queue` | Held payments, most urgent first |
| GET | `/review-queue/{payment_id}` | Reasons, signals and ruleset for one held payment |
| GET | `/health` | `ok` or `degraded`, with mule list, database and config state |
| GET | `/config/rules` | Active thresholds, ruleset version and config hash (no secrets) |

## Project layout

```
app/
  main.py                 app factory, correlation-ID middleware
  api/                    payments.py (assess, review queue), operations.py (health, config)
  services/               validator, enrichment, mule_service, rules, decision_engine, audit_service
  config.py  schemas.py  database.py  models/entities.py  logging_setup.py  state.py
config/rules.json         thresholds, keywords, ruleset version, mule-list switch
sample-data/              synthetic mule list and example requests
tests/                    unit, integration, acceptance and the labelled scenario suite (scenarios/)
evidence/                 evidence pack (below)
verify.sh                 the one verify command
TIMELOG.md                build timeline per step
```

## Known limitations

- **No authentication or authorisation.** All endpoints, including the review queue, are open. This is out of scope for the slice.
- **Inputs are trusted.** Confirmation of Payee, remote-access and history signals are taken from the request, not verified.
- **The mule list is a local synthetic stub.** While it is unavailable, *all* payments are held, by owner decision. "Unavailable" includes switched off, missing, unreadable, or smaller than `mule_list_min_entries` (e.g. an empty, truncated feed). That is safe but blunt.
- **No case workflow.** Held payments can be listed but not resolved; status stays `PENDING`.
- **Local scale only.** SQLite is single-node with a single writer. There has been no load testing.
- **The token key has a dev default.** It is flagged as `dev-default` in `/health`. Set `APP_SHIELD_TOKEN_KEY` for anything shared.
- **Synthetic validation only.** Rules and thresholds are illustrative and tested only against synthetic scenarios.

The next steps would be: authentication and analyst roles, case resolution, an upstream mule service with a circuit breaker and cache, Postgres, a container image with a CI/CD pipeline and shadow-mode rollout, and lint and type-check gates. See [evidence/decisions.md](evidence/decisions.md) and [evidence/operate.md](evidence/operate.md).

## Evidence pack

| File | Contents |
|---|---|
| [evidence/intent.md](evidence/intent.md) | Problem, users, assumptions, constraints, out of scope, success criteria (source of truth) |
| [evidence/design.md](evidence/design.md) | Architecture, data flow, components, API and schemas, data model, rules and precedence, logging, risks, trade-offs, cloud shape |
| [evidence/agentic-workflow.md](evidence/agentic-workflow.md) | How Claude Code was used: steering prompts, decomposition, human interventions, review |
| [evidence/verify.md](evidence/verify.md) | Tests and actual results, scenario table, mutation checks, gaps, local vs PR vs pipeline |
| [evidence/operate.md](evidence/operate.md) | Run, settings and secrets, health, logs, runbooks, failure modes, rollback, local to cloud |
| [evidence/decisions.md](evidence/decisions.md) | Five decision records and what was deliberately not built |
