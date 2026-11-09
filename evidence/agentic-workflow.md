# Agentic workflow

## Tools

- **Claude Code** (VS Code extension, Claude Opus 5.5): the implementation agent. Under my direction it wrote the code, tests and first drafts of the docs, and ran the commands.
- **GitHub Actions:** runs `verify.sh` on every push (Python 3.11, 3.12 and 3.13).
- **My role:** I framed the problem (`intent.md`), wrote the steering prompts, made the design and business decisions, reviewed each step before it was committed, and decided what to fix and what to leave.

## How I decomposed the work

I used gated steps. A step started only after I had reviewed and approved the previous one. Each step was timed ([TIMELOG.md](../TIMELOG.md)) and ended in its own commit.

| Step | What I asked for | Key decision I made |
|---|---|---|
| 1. Design | Architecture, contracts, schemas, decision precedence, scenarios and risks **before any code** | During a mule-list outage, hold **all** payments (strictest fail-closed reading) |
| 2. Engine | A rules engine to my specification: file layout, reason-code names, required response fields | Reason-code vocabulary, and a configurable high-value rule |
| 3. API and persistence | Five endpoints, SQLite, unique IDs, structured logs, privacy rules | A retry with the same payment ID gets the same answer; a conflicting reuse is rejected |
| 4. Verification | A labelled scenario suite (in a QA-engineer role) and one `verify.sh` for CI | The scenarios are stored as data, so they can be read without reading code |
| 5. Documentation | The evidence pack, written only from the actual implementation | Nothing claimed that isn't built |
| 6–8. Reviews | An independent senior review, then a technical assessment. **Fix critical defects only, no new features** | Fixed F1–F4 and D1–D3; left D6 (remote access to a known payee) as a business decision |
| 9. Hand testing | I ran the README myself on Windows | Fixed what I hit: the `/docs` example body, Windows commands, a busy port |

## Representative steering prompts

> "Use the project intent as the source of truth. First: propose the architecture, define API contracts, request and response schemas, database entities, decision precedence, a synthetic scenario suite, and security and operational risks. Do not implement everything at once. Do not introduce ML, a customer UI or real payment rail integrations."

> "Act as a Senior Banking QA Automation Engineer. Build a labelled synthetic scenario suite… For each scenario, verify expected vs actual decision and reason codes, ruleset version, correlation ID, persistence behaviour, and whether the request was scored… Report actual results, not assumed results."

> "Perform an independent senior engineering review… Inspect for decision precedence defects, fail-open paths, missing validation, race conditions… Fix critical defects only. Do not introduce new features."


## How I verified the AI's output

I didn't treat "tests pass" as proof. The checks that mattered:

1. **Expected outcomes were written in the design before the code existed,** so the tests check the code against an independent specification, not against itself.
2. **Mutation checks.** At each build step, bugs were deliberately inserted, such as a boundary off by one, a mule outage ignored, or a HOLD not queued. I confirmed a test failed for every one, including `verify.sh` exiting 1.
3. **Adversarial probing beyond the suite.** This found **F1**: a known mule account written in look-alike (fullwidth) digits passed validation and was **ALLOWED**, while all 159 tests were green.
4. **An independent assessment, executed rather than read.** Running the "accepted risks" showed they were not safe. **D1:** 12 simultaneous retries returned 1 ALLOW and 11 HOLDs. **D2:** an empty mule list was treated as healthy, and a mule account was allowed. The agent had previously judged both acceptable.
5. **Every fix has a regression test that fails on the old code,** re-run against a live server.
6. **Hand testing from a fresh clone**, following the README exactly. This found three issues that 182 automated tests could not: Windows `bash` not on PATH, a port clash, and `/docs` sending an empty body.

## Where I intervened or corrected course

- **Scope:** I supplied the engine and API specifications myself, and the agent's first-pass engine was reshaped to match them (Step 2b).
- **Fix scope:** I limited reviews to critical defects and chose which findings to fix. Known-payee remote access stays a WARN pending a business decision.
- **Instructions that were wrong:** the agent's test checklist told me to paste JSON into `/docs`, which didn't work. I hit it, and it was fixed and covered by a test.
- **Guardrails:** Claude Code's permission checks blocked one over-broad command (stopping all Python processes). It was re-run safely, scoped to its own server.
- **Hygiene:** the confidential brief was kept out of the repo, and only synthetic data was used.

## Accountability

I reviewed and approved every step and own every design decision in [decisions.md](decisions.md). My rule throughout: **nothing counts as working until it has been executed**, and I applied that to the AI's claims about its own work too. Full test results are in [verify.md](verify.md).
