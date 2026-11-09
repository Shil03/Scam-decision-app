# Operate

How to run APP Shield locally, what it logs, how to change it safely, and how it behaves when things fail. Everything below works on the code in this repository. The "Local to cloud" section describes production and is not built.

## Run

```bash
python -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

- **Interactive API docs:** http://127.0.0.1:8000/docs
- **Data:** decisions are written to `data/app_shield.db`, which is created on first start and ignored by git. Delete the file to reset.
- **Stop:** Ctrl+C. All state is in the SQLite file and the two JSON files.

### Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `APP_SHIELD_CONFIG` | `config/rules.json` | Rule thresholds, keywords, ruleset version, mule-list switch |
| `APP_SHIELD_MULE_LIST` | `sample-data/mule_accounts.json` | Mule-list stub (synthetic accounts) |
| `APP_SHIELD_DB` | `data/app_shield.db` | SQLite file |
| `APP_SHIELD_TOKEN_KEY` | `dev-only-token-key-not-secret` | HMAC key for payee tokens and request fingerprints |

**Secrets.** The only secret is `APP_SHIELD_TOKEN_KEY`. The built-in default is for local use, and `/health` reports `"token_key": "dev-default"` while it is in use. Set a real value outside source control for anything shared. The key is never returned by `/config/rules` or written to logs. No other credentials or API keys exist.

## Status: `GET /health`

```json
{"status": "ok", "ruleset_version": "2026.11.1",
 "dependencies": {"mule_list": "up", "database": "up", "config": "current"},
 "token_key": "dev-default"}
```

`status` is `degraded` when any of the following holds:
- `mule_list` is `disabled` (switched off in config) or `down` (file missing, unreadable, or fewer entries than `mule_list_min_entries`);
- `database` is `down`;
- `config` is `stale` (the last edit was invalid and ignored).

It returns **HTTP 200 when `ok` and HTTP 503 when `degraded`**, so status-code monitors see it. While degraded, affected payments are HELD, never allowed.

**`GET /health/live`** returns HTTP 200 `{"status": "alive"}` whenever the process is up. Point **alerting** at `/health`. Point **restart (liveness) probes** at `/health/live`: a mule-list outage must not trigger restarts, because the service is still answering safely (HOLD) and recording each HOLD for review.

## Logs

One JSON line per event on stdout, each with a UTC `ts` and a `correlation_id`. Search by `correlation_id` to follow one payment from the channel's request to the stored decision.

| `event` | When | Action |
|---|---|---|
| `payment_decision` | Every ALLOW / WARN / HOLD | Normal. `persisted: false` means the database write failed and the payment was HELD |
| `payment_rejected` | Every REJECT | Look at `error_fields`. A spike points to a channel integration bug |
| `payment_decision_replayed` | Identical retry of a known `payment_id` | Normal after channel timeouts |
| `audit_write_failed` | Database write failed | **Investigate.** Payments are being HELD with `ENGINE_ERROR` |
| `assess_failed` | Unexpected exception in the request path | **Investigate.** The payment was HELD |
| `rule config reloaded …` | Config file changed and was valid | Confirm the version and hash are what you expected |
| `invalid config edit ignored …` | Config file changed but failed validation | Fix the file. The last good config is still active and `/health` is degraded |
| `mule list unavailable …` | Mule stub missing or unreadable | Restore the file or check the upstream |

Logs never contain the account number, BSB, payee name, reference text or customer ID. Errors are logged by exception type only.

## Runbooks

**Change a threshold (no restart, no code change)**
1. Edit `config/rules.json`. Bump `ruleset_version` too, e.g. `2026.11.1` → `2026.11.2`.
2. Check `GET /config/rules` shows the new values and `"stale": false`.
3. New decisions carry the new `ruleset_version` and `config_hash`; older decisions keep theirs.

**Simulate a mule-list outage**
1. Set `"dependencies": {"mule_list_enabled": false}` in `config/rules.json`.
2. `GET /health` → HTTP 503 `degraded`, `mule_list: disabled` (`/health/live` stays 200). Every payment → HOLD `MULE_LIST_UNAVAILABLE`.
3. Set it back to `true` to recover. Deleting, corrupting or emptying `sample-data/mule_accounts.json` (or setting `mule_list_min_entries` above its size) has the same effect, but reports `down`.

**Roll back**
- **Config:** restore the previous `config/rules.json` (`git checkout <commit> -- config/rules.json`). It takes effect on the next request. Decisions record the version and hash, so the rollback window is auditable.
- **Code:** check out the previous commit and restart. The schema uses `CREATE TABLE IF NOT EXISTS` and is unchanged between versions, so the database needs no migration.

**Review held payments**
- `GET /review-queue` lists held payments by priority: 1 = mule or dependency, 2 = hard signal, 3 = escalated soft signals.
- `GET /review-queue/{payment_id}` shows the reasons, the signals used, and the ruleset version and config hash.
- Resolving a case is out of scope, so status stays `PENDING`.

## Failure modes

| Failure | What happens | Decision | Visible in |
|---|---|---|---|
| Mule list switched off, missing, corrupt, raising, or below `mule_list_min_entries` (e.g. empty) | Lookup returns `UNAVAILABLE` | HOLD `MULE_LIST_UNAVAILABLE` (all payments) | `/health` degraded; reason code; `degraded: true`; `mule list unavailable` log |
| CoP / remote-access / history sent as `UNAVAILABLE` | Dependent rules skipped | HOLD `*_UNAVAILABLE` | Reason code; `degraded: true` |
| Config edit invalid | Edit ignored; last good config kept | Unchanged | `/health` `config: stale`; error log |
| No valid config at startup | Service refuses to start | — | Startup error |
| Database write fails | Decision cannot be recorded | HOLD `ENGINE_ERROR`, not queued | `audit_write_failed` + `payment_decision` with `persisted: false` |
| Unexpected exception | Caught in the request path | HOLD `ENGINE_ERROR` (HTTP 200, never 500) | `assess_failed` |
| Malformed or oversized request | Never enriched or scored | REJECT 422 / 413 | `payment_rejected` |
| Retry with the same payload, including simultaneous retries | Stored decision returned; a request that loses the insert race re-checks and replays | Same as first time, one stored decision | `payment_decision_replayed`; `Idempotent-Replay` header |
| `payment_id` or correlation ID reused for something else | Not assessed | REJECT 409 | `payment_rejected` |
| This service down or slow | — | Channel must treat it as HOLD (documented contract) | Channel side |

## Local to cloud (not built)

| Concern | Local slice | Production |
|---|---|---|
| Runtime | `uvicorn` process | Container on a managed platform, ≥ 3 replicas across zones, internal load balancer, mTLS from the channel |
| Store | SQLite file | Managed Postgres, plus decision events on the enterprise event bus |
| Mule list | JSON file, switch in config | Upstream service call with a short timeout, circuit breaker and bounded cache; fail-closed rules unchanged |
| Config | File reloaded on change | Versioned config repo, risk-owner approval, GitOps rollout |
| Secret | Environment variable with a dev default | Cloud secrets manager, rotation |
| Logs and status | stdout JSON, `/health` (503 when degraded), `/health/live` | Central log platform, dashboards, alerts on degraded mode, `ENGINE_ERROR` rate, p99 latency, config hash change |
| Access | None (out of scope) | Service-to-service auth for the channel; analyst roles for the review queue |
| CI/CD | `verify.sh`; GitHub Actions runs it on Python 3.11–3.13 | Pipeline below |

**Pipeline:**
- **PR:** `verify.sh` plus lint, type check, dependency and secret scanning.
- **Main:** build and sign an image, generate an SBOM, deploy to a test environment, replay the scenario suite against the running container.
- **Promotion:** shadow mode (decisions logged, the channel ignores them, results compared), then canary, then full.
- **Rollback:** redeploy the previous image or revert the config commit.
