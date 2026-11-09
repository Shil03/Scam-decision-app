# Agentic workflow

## Tools

- **Claude Code** (VS Code extension, Claude Opus 5.5) is the primary implementation agent. It wrote the code, tests and documentation, and ran every command.
- The human owner frames the work, writes the steering prompts, makes the business decisions, and approves each gated step before it is committed and pushed.
- Commit messages carry no AI co-author line, at the owner's request. AI use is disclosed here instead.

## At a glance

| Step | Owner's steering | Commit (local time, 9 Nov 2026) |
|---|---|---|
| 0 | Repository, intent and time log set up by the owner | 09:44 |
| 1 | Propose architecture, contracts, schemas, entities, precedence, scenarios, risks | 09:58 |
| 2 | "Start step 2", building the approved design | 10:13 |
| 2b | Owner's engine specification: `services/` layout, reason-code vocabulary, contract fields | 10:20 |
| 3 | Owner's API and persistence specification: five endpoints, unique IDs, logging, privacy | 10:32 |
| 4 | "Act as a Senior Banking QA Automation Engineer": scenario suite, acceptance checks, `verify.sh` | 10:38 |
| 5 | Documentation against the actual implementation | 10:48 |
| 6 | Independent senior engineering review; fix critical defects only | 10:58 |
| 7 | "Act as a Senior Software Engineering Technical Assessor" (read-only), then "fix D1 and D2" | 11:16 |
| — | Re-assessment and score; plain-language rewrite of `design.md` | 11:33 |
| 8 | "Yes please fix" (D3: health status code) | see `git log` |

Agent working time per step is in [TIMELOG.md](../TIMELOG.md). Between steps, the owner reviewed the summary and the diff, and either approved it or steered with the next prompt.

**Where the human changed the outcome:**
- **Mule-list outage scope:** hold all payments (D2 in [decisions.md](decisions.md)).
- **Engine specification:** the owner's Step 2b prompt reshaped the engine: file layout, reason-code names, a configurable high-value rule, and explicit customer-confirmation and review-queue flags.
- **API specification:** the owner's Step 3 prompt added two endpoints and the unique-ID requirement.
- **Project hygiene:** repository naming, commit attribution, and keeping the confidential brief out of the repo.

**Where the agent pushed back instead of complying literally:**
- **Dependency reason codes:** it kept a specific code per dependency instead of one generic `DEPENDENCY_UNAVAILABLE`.
- **Rule order:** it kept evaluating every rule after validation, which changes the reasons listed, not the outcome.
- **"Remote access → HOLD":** it labelled S07 (known payee → WARN) separately rather than silently changing an approved rule.

Each was explained to the owner at the time and is recorded in the step log below.

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
- The owner then supplied the assessment brief. The agent restructured the repo to the brief's suggested layout (`evidence/`, `sample-data/`, `verify.sh`). It also added technology trade-offs, the production and cloud shape, and CI/CD promotion to the design.
- The owner asked to remove "rebuild" naming from the repo and time log, so the project is not mistaken for a re-creation of the assignment. The public repo was named `app-shield`, then renamed by the owner to `Scam-decision-app`.

### Step 2: Engine

**Steering prompt (excerpt):**
> Yes, let's start with step 2.

The agent built from the approved design §3 and §5. It wrote `app/schemas.py`, `app/config.py`, `mule_list.py`, `rules.py` and `engine.py` (later moved into `app/services/` in Steps 2b and 3), and unit tests whose expected outcomes come from the design's decision table.

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

### Step 4: Scenario suite and verify command

**Steering prompt (excerpt):**
> Act as a Senior Banking QA Automation Engineer. Build a labelled synthetic scenario suite for APP Shield. […] For each scenario, verify: expected decision, actual decision, expected reason codes, actual reason codes, ruleset version, correlation ID, persistence behaviour, whether the request was scored. […] Create verify.sh that executes all checks and exits non-zero on failure. Execute the tests. Report actual results, not assumed results.

**What the agent did:**
- **Scenarios as data.** 34 labelled scenarios (22 assessed, 12 REJECT) live in `tests/scenarios/scenarios.json`, so a reviewer can read and extend them without reading Python.
- **One runner checks every scenario.** `tests/test_scenarios.py` sends each one over HTTP to a fresh app and SQLite file. It wraps the engine and the mule lookup with call counters, to prove whether the request was scored and enriched. It checks up to 13 properties per scenario and writes `reports/scenario-report.md` from the actual responses and database rows.
- **Acceptance checks.** `tests/test_acceptance.py` holds the eight checks the owner listed, swept across the whole suite where possible. For example, "mule-list failure can never return ALLOW" runs every assessed scenario under four failure modes: switched off, file missing, file corrupt, and lookup raising.
- **One command for CI.** `verify.sh` checks the Python version and dependencies, byte-compiles the app, validates the shipped config and mule stub, checks repository hygiene (no tracked databases, PDFs or obvious secrets), then runs all tests.

**Interventions and review:**
- **"Remote-access session → HOLD" did not match the approved design.** Under the design, remote access to a new payee is HOLD, but to a known payee it is WARN. The agent labelled both cases (S08 HOLD, S07 WARN) instead of silently changing a rule, and flagged it to the owner.
- **"Unusual pattern → configured intended decision"** is covered twice: S10 shows the default (SOFT → WARN), and S22 shows the same payment becoming ALLOW after the multiplier is tuned in config.
- **One test failed on its first run, and the bug was in the test.** The ruleset-version check sampled only S01–S05, which contain no HOLD, so it compared an empty review queue. The sample was widened to S01–S09, and the assertion tightened to require both ruleset versions in the queue.
- **`verify.sh` was proven to fail.** With the remote-access/new-payee rule disabled, it exited 1 with 4 failing tests. The report showed `S08 | expected HOLD | actual ALLOW | FAIL`. The rule was then restored, and a clean run exited 0.
- **Actual result:** 155 tests passed, the scenario report shows 34/34 PASS, and `verify.sh` exits 0.

### Step 5: Documentation and evidence pack

**Steering prompt (excerpt):**
> Prepare the submission documentation for APP Shield using the actual implementation. Create all six evidence files. Also complete README.md. […] The documentation must reflect actual implemented functionality. Do not claim ML, cloud deployment, authentication or payment rail integration exists. Document them as out of scope where appropriate. Include the exact command to run the application and verify.sh.

**What the agent did:**
- **Design doc.** It re-read `design.md` (written in Step 1, before any code) and corrected the drift: module names, the separate `rejections` table, column lists, log fields, the 34-scenario suite, and the actual build steps.
- **New evidence files.** It wrote `verify.md`, `operate.md` and `decisions.md`, and the README.
- **Real examples only.** Every example response and log line in the docs was captured from the running code, not written by hand.
- **CI.** It added `.github/workflows/verify.yml`, so `verify.sh` runs in CI on Python 3.11–3.13. The 3.11 minimum had only been claimed until then; the local environment is 3.15.
- **`intent.md` left unchanged**, because it is the source of truth.

**Interventions and review:**
- **A real usability bug found while capturing examples.** A negative amount produced "must be a decimal string with at most 2 decimal places", which is technically true but not a *clear* error (success criterion 4). The format check now allows a sign, so negatives reach the bound check and say "Input should be greater than 0". A test pins the messages.
- **Final run:** 159 tests passed, 34/34 scenarios, `verify.sh` exit 0.

### Step 6: Independent review

**Steering prompt (excerpt):**
> Perform an independent senior engineering review of APP Shield against the original technical assessment and project intent. Prioritise these critical acceptance requirements: […] Inspect for: decision precedence defects, fail-open paths, missing validation, race conditions and persistence issues, inconsistent rule versions, sensitive information in logs, inaccurate documentation. Run the verification command and report actual results. Fix critical defects only. Do not introduce new features or restructure working components unnecessarily.

**What the agent did:**
- **Treated green tests as a baseline, not proof.** `verify.sh` passed (159 tests, 34/34), so the agent probed the running app with inputs the suite did not cover: Unicode digits, deep nesting, duplicate keys, `NaN` tokens, nulls, and control characters in IDs.
- **Read the remaining paths:** concurrency, replay and the safety net.

**Findings:** four defects (F1–F4) and one documentation inaccuracy. The full table is in [verify.md](verify.md#independent-review-step-6).
- **The most serious was F1.** A mule account written in fullwidth digits was **ALLOWED**. The 159 passing tests had never exercised this.

**Interventions and review:**
- **Every finding was reproduced before it was fixed.** Every fix got a regression test (10 in total, including scenarios R13 and R14).
- **The new tests were then run against the original Step 5 code:** all 10 failed. That proves both that the defects were real and that the tests detect them.
- **One new test was wrong at first.** It patched `json.loads` globally, which also broke the test client. It was re-scoped to the payments module.
- **Fixes stayed minimal**, as the prompt asked:
  - two regex patterns;
  - one parser hook;
  - one extra exception type;
  - one extra log call.

  Five accepted risks are recorded instead of fixed, because fixing them would add features.
- **Result:** 169 tests passed, 36/36 scenarios, `verify.sh` exit 0.

### Step 7: Technical assessment, then fixes D1 and D2

**Steering prompts (excerpts):**
> Act as a Senior Software Engineering Technical Assessor […] Do not modify the code. Do not fabricate test results. Do not assume a feature works because it is documented.

and then:

> lets fix the D1 and D2

**What the agent did:**
- **Ran the assessment without modifying code.** It started the backend with isolated copies of the config, mule list and database, and drove it over HTTP with payloads written independently of the project's scenario file. It read the SQLite file directly and scanned the server's log output.
- **Results:** 24/25 checks PASS. A5b failed (remote access to a known payee is WARN by design). C3b is NOT VERIFIED (the channel enforces customer confirmation).
- **Stated its own conflict of interest.** The agent had built the code, so it declared that the review was not independent.

**Interventions and review:**
- **The assessment overturned the agent's own earlier judgement.** In Step 6 the agent had recorded "concurrent duplicates" and "empty mule list" as accepted, *safe* risks. When the assessment executed them, they turned out to be contradictory decisions (D1) and a fail-open path (D2). Accepted risks were then judged by executing them, not by reasoning about them.
- **Owner decision:** fix D1 and D2 only.
- **How the fixes were proven:**
  - every fix got a regression test (8 new);
  - all 8 fail on the Step 6 code;
  - the 12-way concurrency test passed 10/10 consecutive runs before it was relied on;
  - both original probes were re-run against the live fixed server.
- **Ruleset version unchanged.** `ruleset_version` stays `2026.11.1` because no threshold changed. Adding `mule_list_min_entries` changes the config file, and therefore `config_hash` (`0afe906337af`), which is what audit relies on to identify the exact config.
- **Result:** 177 tests passed, 36/36 scenarios, `verify.sh` exit 0.

### Step 8: Health status code (D3)

**Steering prompt:**
> yes please fix

**What the agent did:**
- **Before coding,** it pointed out a trap in the obvious fix. If `/health` returns 503 while degraded and a platform uses it as a liveness probe, a mule-list outage would put a service that is still answering safely (HOLD) into a **restart loop**.
- **So the fix is two-part:**
  - `/health` returns 503 while degraded, for alerting;
  - a new `/health/live` always returns 200 while the process is up, for restart probes.

**Interventions and review:**
- **5 regression tests**, one per degraded cause: mule list switched off, missing or empty; config stale; database down. All 5 fail on the old code.
- **One new test assertion was muddled** at first (a mixed `and`/`or` condition). It was rewritten to state the expected outcome per cause.
- **A live server check:** outage → `/health` 503, `/health/live` 200, payment HOLD; recovery → 200.
- **The agent made a mistake outside the project.** One tool call wrote a stray file, `C:\AKASHP~1\placeholder.txt`, with no purpose. The agent noticed immediately, checked the contents, and deleted the file. Removing the empty folder that the call had also created was blocked by Claude Code's safety check. The agent left it for the owner and reported it, rather than working around the check.
- **Result:** 182 tests passed, 36/36 scenarios, `verify.sh` exit 0.

## How generated output is verified

- Every step is reviewed by a human before commit. No commit is made without approval.
- Behaviour is pinned by the labelled scenario suite. Expected outcomes were written in the design **before** the code, so tests check the agent against an independent specification, not against its own implementation.
- Privacy is checked by an automated scan of logs and the database for raw account numbers, names and references, rather than by reading code.
- **Tests were tested.** At each build step the agent deliberately introduced bugs (14 in total, plus reverting the Step 6 fixes) and confirmed a test failed for each one. It also proved `verify.sh` exits 1 on a broken rule. See [verify.md](verify.md).
- **Examples are real.** Response and log examples in the README and design were captured from the running code.
- **The human stays accountable.** The owner reviewed each step's summary before approving the commit, and owns every design decision recorded in [decisions.md](decisions.md).
