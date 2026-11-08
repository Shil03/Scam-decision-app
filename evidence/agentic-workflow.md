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

## How generated output is verified

- Every step is reviewed by a human before commit. No commit is made without approval.
- Behaviour is pinned by the labelled scenario suite. Expected outcomes were written in the design **before** the code, so tests check the agent against an independent specification, not against its own implementation.
- Privacy is checked by an automated scan of logs and the database for raw account numbers, names and references, rather than by reading code.
