# Decisions

Short records of the decisions that shaped the slice, and what was deliberately left out.

## D1. Rules with reason codes, not a model or a fraud score

- **Context:** the intent says a rules-based first release is acceptable and puts ML out of scope. Every outcome must be explainable to the channel, the analyst and audit.
- **Decision:** 16 small rules in three categories (DEPENDENCY, HARD, SOFT) and a fixed precedence. There is no probability score; each outcome is justified by named reason codes and a `scam_category`.
- **Trade-off:** easy to explain, test and version, but weaker on scam patterns no rule describes. A model could later add one more signal behind the same response contract.

## D2. Fail closed, including holding every payment while the mule list is down

- **Context:** the intent says a missing dependency must give HOLD, never ALLOW. It says "affected payments" for a mule-list outage, which could mean all payments or only payments to new payees.
- **Decision (owner):** while the mule list is unavailable, **all** payments are held. The same rule covers the other cases:
  - a CoP, remote-access or history input marked `UNAVAILABLE`;
  - a failed database write ("cannot record means cannot release");
  - any unexpected exception, which returns HOLD / `ENGINE_ERROR` with HTTP 200, never a 500.

  A *missing* input block is treated differently: it is a malformed request and gets REJECT.
- **Empty is not healthy (assessment finding D2, fixed):** a mule list with fewer entries than `mule_list_min_entries` (default 1) is treated as unavailable. Without this, an empty or truncated feed silently meant "no mules", and a known mule account was ALLOWED.
- **Trade-off:** the strictest and simplest rule to audit. The cost is that an outage holds all traffic. This is mitigated by `/health` and the reason code, and should be revisited with the business, for example by trusting payees already paid or allowing a bounded stale cache.

## D3. Thresholds in a hot-reloaded JSON file, with a last-known-good fallback

- **Context:** risk owners must tune thresholds without a code release.
- **Decision:**
  - `config/rules.json` is re-read when it changes and validated with bounds.
  - An invalid edit is ignored, the last good config stays active, and `/health` reports `config: stale`.
  - Every decision stores `ruleset_version` **and** the SHA-256 of the config file.
  - JSON was chosen because PyYAML was not installed and the standard library is enough.
- **Trade-off:** no approval workflow and no signing in the slice; the production path is a versioned config repo with approvals.

## D4. Privacy: store and log only what review needs

- **Context:** no raw sensitive payment data in logs; review records keep only what is necessary.
- **Decision:**
  - **Payee:** stored as an HMAC token plus a masked account. The token lets analysts link payments to the same payee without the account number.
  - **Never stored or logged:** the account number, payee name and reference.
  - **Log lines:** fields are passed explicitly by name; the amount is logged as a band; exceptions are logged by type only; validation errors never echo submitted values.
  - **Tests:** they scan the logs **and the raw database files** for every scenario's sensitive values.
- **Trade-off:** analysts can't see the full account in this slice. In production the core banking system would provide it, behind its own access control.

## D5. Unique IDs: replay an identical retry, refuse a conflicting one

- **Context:** "unique payment and correlation IDs", in a synchronous path where channels retry after timeouts.
- **Decision:**
  - **Same `payment_id` and same payload:** the stored response is returned (`Idempotent-Replay: true`), even if thresholds have changed since.
  - **Same `payment_id` with a different payload, or a correlation ID reused for another payment:** 409 REJECT.
  - **Database:** both IDs are `UNIQUE`.
- **Concurrency (assessment finding D1, fixed):** originally, a request that lost the insert race got an unrecorded HOLD `ENGINE_ERROR`. 12 simultaneous attempts produced 1 ALLOW and 11 HOLDs. Now the loser re-runs the duplicate check and replays the winner's decision (or gets 409), so every caller sees one decision.
- **Trade-off:** this relies on the database's UNIQUE constraint, so it holds for one database. Several replicas would need a shared idempotency store.

## Deliberately not built

| Not built | Why |
|---|---|
| ML or model scoring | Out of scope (intent) |
| Authentication and authorisation | Out of scope (intent). Every endpoint, including the review queue, is open locally |
| Real payment rails, real CoP or session services | Out of scope (intent); these signals are request inputs |
| Mule detection | Out of scope (intent); the mule list is a synthetic stub |
| Customer UI, warning content | Out of scope (intent); the service returns `scam_category` for the channel to use |
| Case management (resolving a review) | Out of scope beyond a basic queue endpoint; status stays `PENDING` |
| Cloud deployment, container image | Out of scope (intent); the cloud shape is described in [design.md](design.md) §10 and [operate.md](operate.md) |
| Load or performance testing | Not attempted in the timebox; `latency_ms` is recorded per decision |
| Lint and type-check gates | Tools not installed in the timebox; listed as a PR-stage addition |
