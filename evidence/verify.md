# Verify

What is checked, the **actual** results, what is not covered, and where each check runs.

## One command

```bash
./verify.sh          # Windows: run from Git Bash, or: bash verify.sh
```

It exits non-zero on the first failure. Steps:

1. Python 3.11+.
2. Dependencies import.
3. Byte-compile `app/`.
4. Shipped `config/rules.json` and the mule-list stub are valid.
5. Repository hygiene: no tracked `.db`, `.pdf`, `.pem`, `.key` or `.env` files, and no obvious secrets.
6. `pytest`: every test below.

It also writes `reports/scenario-report.md`, with expected vs actual results for each scenario.

## Actual result

Final local run after the Step 9 usability fixes (Windows 11, Git Bash, Python 3.15.2):

```
== 1/6 Python version (3.11+ required)                  3.15.2
== 2/6 Dependencies importable                          fastapi 0.162.2 | pydantic 2.14.3
== 3/6 Byte-compile application code                    ok
== 4/6 Shipped rule config and mule list stub are valid ruleset 2026.11.1 | config sha256 0afe906337af | mule list up
== 5/6 Repository hygiene                               ok
== 6/6 Tests                                            183 passed in 13.45s
Scenario report: 36/36 scenarios passed.
VERIFY PASSED            (exit code 0)
```

**It fails when it should.** With the `REMOTE_ACCESS_NEW_PAYEE` rule deliberately disabled, `verify.sh` **exited 1**: 4 tests failed, and the report row read `S08 | HOLD | ALLOW | … | FAIL`. The rule was then restored.

**CI:** `.github/workflows/verify.yml` runs `verify.sh` on every push and PR, on Python 3.11, 3.12 and 3.13 (Ubuntu), and uploads the scenario report as an artifact. Results are on the repository's **Actions** tab. The first CI run (Step 5 commit) passed on all three versions with 159 tests and 34/34 scenarios each.

## Test inventory (183)

| File | Tests | What it proves |
|---|---|---|
| `tests/test_schemas.py` | 36 | Valid requests parse. Every malformed variant fails validation, including floats, NaN, 3 dp amounts, unknown fields and loose types. Amount errors are clear (a negative gives "greater than 0"). Non-ASCII digits are rejected |
| `tests/test_config_and_mule_list.py` | 14 | Threshold change picked up without restart. Invalid edits keep the last good config and mark it stale. No valid config means no start. Mule stub match / no-match / disabled / missing / corrupt / below minimum size |
| `tests/test_engine.py` | 30 | Every rule and its `≥` boundary, precedence, escalation, reason order, `scam_category`, degraded flag, config-driven thresholds, exception gives HOLD |
| `tests/test_pipeline.py` | 18 | `assess()`: REJECT never reaches the mule list or the rules. Errors never echo submitted values. Contract fields always present. WARN asks for confirmation and HOLD is queued. Mule failure gives HOLD |
| `tests/test_api.py` | 37 | Full HTTP workflow on a real SQLite file: persistence, review queue and detail, 404s, REJECT 422/413, replay and 409s, correlation IDs, mule switch-off and `/health`, config hot reload and `/config/rules`, DB write failure gives HOLD, exception gives HOLD (not 500), log lines and **raw DB bytes** free of sensitive values. Review regressions: parser `RecursionError` gives REJECT, duplicate keys give REJECT, safety-net HOLD logs a decision. Assessment regressions: race loser replays or gets 409; 12 concurrent identical requests get one decision; empty mule list gives HOLD and degraded; minimum size configurable; `/health` is 503 for each of 5 degraded causes while `/health/live` stays 200. `/docs` advertises the request body, and each documented example returns the decision its label claims |
| `tests/test_scenarios.py` | 36 | The labelled scenario suite (below) |
| `tests/test_acceptance.py` | 12 | The owner's acceptance checks, swept across the whole suite (below) |

## Labelled scenario suite: actual results

Defined as data in `tests/scenarios/scenarios.json`. For each scenario the runner checks, against the real HTTP app and a fresh database:
- expected vs actual decision and reason codes;
- ruleset version, matching the active config **and** the stored row;
- correlation ID in the response header, the body and the stored row;
- persistence: decision only, decision plus queue row, or rejection only;
- whether the request was **scored** and **enriched**, using call counters around the engine and the mule lookup.

| ID | Scenario | Expected | Actual | Persisted | Scored |
|---|---|---|---|---|---|
| S01 | Clean payment, known payee | ALLOW `NO_RISK_SIGNAL` | ALLOW `NO_RISK_SIGNAL` | decision | yes |
| S02 | New payee, small amount | ALLOW | ALLOW | decision | yes |
| S03 | New payee, high amount (at the warn threshold) | WARN `NEW_PAYEE_HIGH_VALUE` | same | decision | yes |
| S04 | Payee close match | WARN `PAYEE_CLOSE_MATCH` | same | decision | yes |
| S05 | Payee mismatch, low amount | WARN `PAYEE_MISMATCH` | same | decision | yes |
| S06 | Payee mismatch ≥ hold amount | HOLD `PAYEE_MISMATCH_HIGH_VALUE` | same | decision + queue | yes |
| S07 | Remote access, known payee | WARN `REMOTE_ACCESS_DETECTED` | same | decision | yes |
| S08 | Remote access, new payee | HOLD `REMOTE_ACCESS_NEW_PAYEE` | same | decision + queue | yes |
| S09 | Mule account match | HOLD `MULE_ACCOUNT_MATCH` | same | decision + queue | yes |
| S10 | Unusual pattern (8× average) | WARN `UNUSUAL_PAYMENT_PATTERN` | same | decision | yes |
| S11 | New payee ≥ hold amount | HOLD `NEW_PAYEE_VERY_HIGH_VALUE` | same | decision + queue | yes |
| S12 | "Bitcoin investment" reference | WARN `SCAM_KEYWORD_IN_REFERENCE` | same | decision | yes |
| S13 | 4 payees added in 24 h | WARN `RAPID_PAYEE_ADDITIONS` | same | decision | yes |
| S14 | Three soft signals | HOLD `MULTIPLE_RISK_SIGNALS` + 3 soft | same | decision + queue | yes |
| S15 | Mule list switched off | HOLD `MULE_LIST_UNAVAILABLE` | same | decision + queue | yes |
| S16 | CoP unavailable | HOLD `COP_UNAVAILABLE` | same | decision + queue | yes |
| S17 | Remote-access signal unavailable | HOLD `REMOTE_ACCESS_UNAVAILABLE` | same | decision + queue | yes |
| S18 | History unavailable | HOLD `HISTORY_UNAVAILABLE` | same | decision + queue | yes |
| S19 | Mule match + CoP unavailable | HOLD `COP_UNAVAILABLE`, `MULE_ACCOUNT_MATCH` | same | decision + queue | yes |
| S20 | High value to known payee | WARN `HIGH_VALUE_PAYMENT` | same | decision | yes |
| S21 | Mule list off + soft signal | HOLD (never WARN) | same | decision + queue | yes |
| S22 | S10 with multiplier tuned to 10× in config | ALLOW | ALLOW (`2026.11.1-tuned`) | decision | yes |
| R01–R14 | Not JSON; negative amount; missing `payee`; missing `customer_id`; zero; 3 dp; JSON number; USD; bad BSB; unknown field; history UNAVAILABLE with fields; JSON array; mule account in fullwidth digits; duplicate `amount` key | REJECT | REJECT (all 14) | rejection only | **no** (engine 0 calls, mule list 0 calls) |

**36/36 PASS.** The table regenerates on every run, to `reports/scenario-report.md`.

## Acceptance checks (owner's list): actual results

| Check | How | Result |
|---|---|---|
| REJECT never invokes the risk engine | All 12 REJECT scenarios with call counters on the engine and the mule lookup | 0 and 0 calls; 0 decision rows; 12 rejection rows. **PASS** |
| Every non-REJECT result has ≥ 1 reason code | All plain assessed scenarios | **PASS** |
| Every decision records the active ruleset version | 9 scenarios, version bumped in config, 9 more; decisions **and** queue rows checked | **PASS** |
| Mule-list failure can never return ALLOW | Every assessed scenario × {switched off, file missing, file corrupt, file empty, lookup raising `TimeoutError`} | All HOLD `MULE_LIST_UNAVAILABLE`. **PASS** (5 tests) |
| Health reports degraded when the dependency is disabled | `/health` before and after the switch | `ok` (HTTP 200) → `degraded` (HTTP 503), `mule_list: disabled`. **PASS** |
| Config changes reflected without code change | Same payment before and after editing only `rules.json` | WARN → ALLOW with the new version. **PASS** |
| Logs do not expose raw account details | All 36 scenarios; every account number, BSB, name and reference searched in all log output | No leaks; every line valid JSON. **PASS** |
| Held payments appear in the review queue | All HOLD scenarios; list and detail endpoints; priority order | **PASS** |

## Mapping to the intent's success criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | End to end locally: validate, enrich, score, decide, log, queue if held | `test_api.py`; scenario suite; live uvicorn check in Step 3 |
| 2 | Every non-REJECT has ≥ 1 reason code and the ruleset version | Scenario runner checks on every assessed scenario; acceptance check |
| 3 | Labelled scenario suite passes via a single verify command | `./verify.sh` → 36/36, exit 0 |
| 4 | Malformed requests rejected with a clear error, never scored | R01–R14 (0 engine calls); `test_amount_errors_are_clear` |
| 5 | Mule list disabled gives HOLD, and health reports degraded | S15, S21; 4-mode acceptance check; health test |
| 6 | Thresholds changed in config, reflected without code change | S22; `test_threshold_change_applies_without_restart`; acceptance check |
| 7 | Structured log with correlation ID, no raw sensitive fields | Log tests over every scenario; raw-DB-bytes scan |

## How the tests themselves were checked

Passing tests prove little if they can't fail. During the build, bugs were deliberately introduced and the suite was run against each:

| Step | Deliberate bug | Caught by |
|---|---|---|
| 2 | Warn boundary `>=` changed to `>`; mule outage ignored (fail open); remote-access rule double-fires; dependency hits no longer HOLD | engine tests |
| 2b | Enrichment before validation; errors echo input; mule exception crashes; HOLD asks for customer confirmation | pipeline tests |
| 3 | DB write failure keeps the original decision; raw account logged; retries not replayed; `/health` ignores the mule list; HOLD not queued | API tests |
| 4 | `REMOTE_ACCESS_NEW_PAYEE` disabled | 4 tests, including scenario S08; `verify.sh` exit 1 |
| 6 | Review fixes reverted to the Step 5 code | All 10 new regression tests fail (R13 → ALLOW for a mule account, R14 → WARN on an ambiguous amount) |
| 7 | Assessment fixes reverted to the Step 6 code | All 8 new regression tests fail (concurrency test: 1 ALLOW + 11 HOLD; empty list: health `ok`) |
| 8 | Health fix reverted | All 5 new health tests fail (HTTP 200 while degraded) |

Every bug was caught, and the code was restored after each check.

## Where each check runs

| Check | Local (`verify.sh`) | PR (GitHub Actions, built) | Pipeline (not built) |
|---|---|---|---|
| Unit, integration, scenario, acceptance tests | ✓ | ✓ Python 3.11 / 3.12 / 3.13 | ✓ |
| Config and mule stub validation, repository hygiene | ✓ | ✓ | ✓ |
| Lint and type check (e.g. ruff, mypy) | — | to add | ✓ |
| Dependency vulnerability and secret scanning | basic grep only | to add (e.g. pip-audit, gitleaks) | ✓ |
| Scenario replay against the built container | — | — | ✓ |
| Load and latency test (p99 SLO) | — | — | ✓ |
| Shadow-mode comparison before go-live | — | — | ✓ |

## Independent review (Step 6)

A senior-engineer review of the critical acceptance requirements, done after Step 5. Each finding was proven against the running app before it was fixed.

| # | Finding | Impact | Fix |
|---|---|---|---|
| F1 | `\d` in the BSB, account and money patterns matched **any Unicode digit**. A mule account in fullwidth digits (`１１１１０００１`) passed validation, missed the mule list and was **ALLOWED** | Fail-open past the strongest HOLD rule | ASCII `[0-9]` patterns; scenario R13; 5 schema tests |
| F2 | Deeply nested JSON raises `RecursionError` (Python ≤ 3.12), not `ValueError`, so it fell to the safety net: HOLD instead of REJECT | Invalid request not REJECTED; unqueued HOLD | `RecursionError` treated as malformed JSON; regression test |
| F3 | Duplicate JSON keys were accepted, last value wins | Assessed amount could differ from the amount the channel releases | Duplicate keys give REJECT; scenario R14; regression test |
| F4 | The safety-net HOLD logged only `assess_failed`, with no `payment_decision` line | Breaks "every decision emits a structured log line" | Safety net also logs `payment_decision` (`persisted: false`); regression test |
| D1 | README said every HOLD is queued | Inaccurate: a HOLD caused by a store failure cannot be queued | Wording corrected |

Checked and found sound, with no change made:
- **Precedence:** dependency and hard signals always beat soft, and soft always beats none.
- **Unavailable inputs:** every `UNAVAILABLE` input gives HOLD.
- **WARN:** `requires_customer_confirmation` is true for WARN and only WARN.
- **One config snapshot per request:** a request uses a single config snapshot, so `ruleset_version` and `config_hash` always describe the rules actually applied.
- **Correlation ID** on every response, including REJECT and the safety net.
- **Degraded health** when the mule list is disabled or down.
- **Logs:** no account number, BSB, name, reference or customer ID.

Accepted risks, not fixed (no new features in this review):
- **Body size checked after reading.** The 16 KB limit is checked after the body is read, so a gateway limit is needed in production (design risk 11).
- **Customer confirmation is not recorded.** WARN tells the channel to obtain confirmation, but the confirmation itself is not recorded back to the service. Enforcement is the channel's job under the intent.
- **Replay keeps the original correlation ID.** An identical retry with a *new* correlation ID gets the original response body, which carries the original correlation ID; only the response header has the new one.

## Technical assessment (Step 7)

A further assessment executed every scenario and control against the live backend with independent payloads. It found 24/25 checks PASS. The one FAIL was remote access to a *known* payee returning WARN, which is a deliberate design rule. Probing also found two weaknesses that the review in Step 6 had wrongly accepted as risks. Both were reproduced on the live server, then fixed:

| # | Finding (live server, before the fix) | Fix | After the fix (live server) |
|---|---|---|---|
| D1 | 12 simultaneous identical first attempts returned **1 ALLOW + 11 HOLD `ENGINE_ERROR`**; the 11 HOLDs were not stored or queued | A request that loses the insert race re-runs the duplicate check and replays the winner's stored decision (or gets 409) | 12 × ALLOW, one `decision_id`, 11 replays (3 runs). The HOLD variant gave 12 × HOLD and **1** queue row |
| D2 | Mule list file `{"accounts": []}`: health `ok/up`, and a known mule account was **ALLOWED** | `dependencies.mule_list_min_entries` (default 1): a smaller list is unavailable | Health `degraded/down`; the mule account gave HOLD `MULE_LIST_UNAVAILABLE` |

**D3, fixed in Step 8:** `/health` returned HTTP 200 while degraded. It now returns **503** when degraded. A separate `/health/live` stays 200, so restart probes don't restart a service that is still answering safely. Live check: outage → `/health` 503, `/health/live` 200, payment HOLD. Recovery → `/health` 200.

Open from the assessment, not changed:
- **D4:** no authentication (out of scope).
- **D6:** remote access to a known payee gives WARN, which is a business decision.
- **D7:** the body size limit is checked after reading.

## Known gaps

- **Python versions.** Developed locally on Python 3.15.2. Python 3.11–3.13 are verified only in CI (Ubuntu).
- **No lint, type checking or coverage measurement.** The tools were not installed in the timebox.
- **Tests run in-process.** They use FastAPI's TestClient, not a deployed container. The live server was exercised once by hand in Step 3.
- **No load, concurrency or soak testing.** In particular, the simultaneous duplicate-first-attempt race (design risk 10) is reasoned about, not tested.
- **Starlette deprecation warning.** It recommends `httpx2` for the TestClient. The warning is filtered in `pyproject.toml`, and tests are unaffected.
- **Rules are tested against synthetic scenarios only.** Their real-world precision and recall are unknown, which is expected for a first release.
