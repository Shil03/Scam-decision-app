# Build time log

Wall-clock times recorded by `tools/timelog.sh` at the start and end of each phase. Each phase also ends in its own git commit, so `git log --format="%ad %s" --date=iso` independently confirms the timeline.

| Phase | Start | End | Duration | Notes |
|---|---|---|---|---|
| Step 0: Repository | 2026-11-09 09:44:37 +1100 | 2026-11-09 09:44:42 +1100 | 0m 05s | Local git repo, intent.md (source of truth), README, .gitignore, phase timer |
| Step 1: Design | 2026-11-09 09:52:16 +1100 | 2026-11-09 09:58:04 +1100 | 5m 48s | Architecture, API contracts, schemas, DB entities, decision precedence, scenario suite, risks, cloud shape; evidence/ layout per brief |
| Step 2: Engine | 2026-11-09 10:08:40 +1100 | 2026-11-09 10:12:39 +1100 | 3m 59s | Schemas, hot-reload config, mule stub, 15 rules + precedence, fail-closed wrapper; 69 unit tests + 4-mutant check |
| Step 2b: Engine aligned to owner prompt | 2026-11-09 10:17:27 +1100 | 2026-11-09 10:19:14 +1100 | 1m 47s | services/ layout, validator, assess() pipeline, HIGH_VALUE_PAYMENT, owner reason codes, contract fields; 87 tests + 4-mutant check |
| Step 3: API and persistence | 2026-11-09 10:24:47 +1100 | 2026-11-09 10:30:13 +1100 | 5m 26s | 5 endpoints, SQLite decisions/review_queue/rejections, JSON logs, idempotent replay, fail-closed DB path; 110 tests + 5-mutant check + live uvicorn check |
| Step 4: Scenario suite and verify | 2026-11-09 10:34:11 +1100 | 2026-11-09 10:38:02 +1100 | 3m 51s | 34 labelled scenarios as data, per-scenario report, 8 acceptance checks, verify.sh (proven to exit 1 on a broken rule); 155 tests |
| Step 5: Documentation and evidence pack | 2026-11-09 10:39:47 +1100 | 2026-11-09 10:46:54 +1100 | 7m 07s | README + 6 evidence files from actual implementation; design drift fixed; clear negative-amount error; CI workflow; 159 tests, 34/34 scenarios |
| Step 6: Independent review | 2026-11-09 10:52:07 +1100 | 2026-11-09 10:57:06 +1100 | 4m 59s | 4 defects fixed (Unicode-digit mule bypass, RecursionError, duplicate keys, safety-net log) + 1 doc fix; 10 regression tests fail on old code; 169 tests, 36/36 |
| Step 7: Assessment fixes D1 D2 | 2026-11-09 11:10:33 +1100 | 2026-11-09 11:14:46 +1100 | 4m 13s | D1 race loser replays winner's decision; D2 mule list below min entries = unavailable; 8 regression tests fail on old code; live re-probe; 177 tests, 36/36 |
