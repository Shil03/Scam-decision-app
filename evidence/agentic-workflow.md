# Agentic workflow

## Tools

- **Claude Code** (VS Code extension, Claude Opus 5.5) is the primary implementation agent.
- The human owner frames the work, approves each gated step and reviews every diff before commit.

## How the work was decomposed

The build is split into gated steps: design, then engine, then API and persistence, then the scenario suite and verify command, then docs. The agent may not start a step until the previous one is reviewed and committed. Each step is timed with `tools/timelog.sh` and ends in its own commit (see [TIMELOG.md](../TIMELOG.md) and `git log`).

## Step log

### Step 1: Design

**Steering prompt (excerpt):**
> Use the project intent as the source of truth. First: propose the architecture, define API contracts, request and response schemas, database entities, decision precedence, a synthetic scenario suite, and security and operational risks. Do not implement everything at once. Do not introduce ML, a customer UI or real payment rail integrations. Keep the implementation small, modular and explainable.

**What the agent did:** it read `intent.md` and the installed packages in the venv, then wrote [design.md](design.md).

**Interventions and review:**
- The agent found PyYAML was not installed and chose JSON config (stdlib only) instead of adding a dependency.
- The agent raised an open question instead of guessing: when the mule list is down, should fail-closed HOLD all payments or only payments to new payees? The intent says "affected payments". *Owner decision: HOLD all payments while the mule list is unavailable (strictest fail-closed reading; simplest to audit). Recorded as a decision record in Step 5.*
- The owner then supplied the assessment brief. The agent restructured the repo to the brief's suggested layout (`evidence/`, `sample-data/`, `verify.py`). It also added technology trade-offs, the production and cloud shape, and CI/CD promotion to the design.
- The owner asked to remove "rebuild" naming from the repo and time log, so the project is not mistaken for a re-creation of the assignment. The public repo was named `app-shield`, then renamed by the owner to `Scam-decision-app`.

### Step 2: Engine

**Steering prompt (excerpt):**
> Yes, let's start with step 2.

The agent built from the approved design §3 and §5. It wrote `app/schemas.py`, `app/config.py`, `app/mule_list.py`, `app/rules.py`, `app/engine.py`, and unit tests whose expected outcomes come from the design's decision table.

**Interventions and review:**
- The keyword "fine" was dropped from `IMPERSONATION_SCAM` and replaced with "warrant", because "fine dining" would trigger a scam warning. This was caught while writing the tests.
- The config `max_amount` was removed from the design. A request cap that depends on config would make validation depend on config, so a single schema cap (1,000,000) is used instead. Validation stays static.
- **All 69 tests passed on the first run.** That alone proves little, so the agent ran a **mutation check**: it deliberately broke the code four ways and confirmed at least one test failed each time:
  - the warn boundary changed from `>=` to `>`;
  - a mule-list outage ignored (fail open);
  - the remote-access rule firing alongside the remote-access new-payee rule;
  - dependency hits no longer causing HOLD.

### Step 2b: Engine aligned to the owner's specification

**Steering prompt (excerpt):**
> Implement the APP Shield core decision engine. Create app/services/decision_engine.py, app/services/validator.py, app/config.py, config/rules.json. […] Implement configurable rules for: Confirmation of Payee mismatch, new payee, high payment amount, unusual payment compared with supplied customer history, remote-access session detected, mule account match. Use transparent reason codes. Example reason codes: PAYEE_MISMATCH, NEW_PAYEE_HIGH_VALUE, UNUSUAL_PAYMENT_PATTERN, REMOTE_ACCESS_DETECTED, MULE_ACCOUNT_MATCH, DEPENDENCY_UNAVAILABLE, NO_RISK_SIGNAL. […] Each non-REJECT response must contain decision, reason_codes, ruleset_version, correlation_id. […] Write unit tests for the decision engine. Run tests and explain the decision logic.

**What the agent did:** it compared the prompt with the committed Step 2 engine instead of rebuilding it. It then closed the gaps:
- moved the engine to `app/services/` and added `validator.py`, whose errors never echo submitted values;
- added an `assess()` pipeline in which REJECT stops before enrichment;
- added a configurable `HIGH_VALUE_PAYMENT` rule;
- added `correlation_id`, `requires_customer_confirmation` and `queue_for_review` to the decision;
- renamed reason codes and fields to the owner's vocabulary.

**Interventions and review:**
- **Kept specific dependency codes** (`MULE_LIST_UNAVAILABLE`, `COP_UNAVAILABLE`, …) instead of one generic `DEPENDENCY_UNAVAILABLE`. The prompt listed codes as examples, and a single code would hide which dependency failed. All of them carry category `DEPENDENCY`.
- **Precedence:** the prompt lists dependencies → hard → soft. The engine evaluates all rules after validation, then applies that precedence. The outcome is identical, but the analyst sees every signal on a HOLD, not just the first.
- **Tests:** 87 pass (18 new). A second mutation check introduced four bugs: enrichment before validation, errors echoing input, a mule exception crashing the request, and HOLD asking for customer confirmation. A test failed for each one.

### Step 3: API and persistence

**Steering prompt (excerpt):**
> Implement the complete APP Shield REST API and persistence layer. Create app/main.py, app/api/payments.py, app/database.py, app/models/entities.py, app/services/enrichment.py, app/services/mule_service.py, app/services/audit_service.py. Required endpoints: POST /payments/assess, GET /review-queue, GET /review-queue/{payment_id}, GET /health, GET /config/rules. […] Unique payment and correlation IDs. Structured JSON logging. UTC timestamps. […] Keep only necessary data in review records. Do not log raw account numbers […]. Create integration tests for the full workflow. Do not add a frontend or real banking integration.

**What the agent did:**
- built the files requested;
- added `app/api/operations.py` (health and config), `app/state.py` (shared service objects) and `app/logging_setup.py`, so that `payments.py` holds only payment and review routes;
- added 23 integration tests over HTTP against a real SQLite file.

**Interventions and review:**
- **Duplicate IDs.** "Unique payment and correlation IDs" did not say what a duplicate should do. The agent chose idempotent replay for an identical retry and 409 REJECT for a conflicting reuse, because a payment channel that times out will retry.
- **The agent caught its own SQL bug before running anything.** A join between `review_queue` and `decisions` would have failed on ambiguous columns, so every queue column is now prefixed with its table.
- **A broad command was blocked, and that was correct.** The agent's first live-server check ended with a command that force-killed every `python.exe`. Claude Code's permission check denied it. The agent re-ran the server as a tracked background task and stopped only that task.
- **Live check:** under uvicorn, a payment with remote access, a new payee and a payee-name mismatch returned HOLD with five reason codes and was queued at priority 2. A malformed body returned 422.
- **Mutation check:** five bugs were introduced, and a test failed for each one:
  - a database write failure keeps the original decision (fail open);
  - the raw account number is logged;
  - retries are not replayed;
  - `/health` ignores the mule list;
  - a HOLD is not queued.
- **Totals:** 110 tests pass, 23 of them new.

## How generated output is verified

- Every step is reviewed by a human before commit. No commit is made without approval.
- Behaviour is pinned by the labelled scenario suite. Expected outcomes were written in the design **before** the code, so tests check the agent against an independent specification, not against its own implementation.
- Privacy is checked by an automated scan of logs and the database for raw account numbers, names and references, rather than by reading code.
