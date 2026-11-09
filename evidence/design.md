# How APP Shield works

This document explains how APP Shield is built and why, in plain language. It describes what is **actually implemented**. Anything that is only proposed is clearly marked "not built". The requirements it meets are in [intent.md](intent.md), and technical terms are explained in the [glossary](#glossary) at the end.

---

## 1. The big picture

**The problem.** In an authorised push payment (APP) scam, a criminal talks a real customer into sending money themselves. Typical stories are "I'm from the bank, move your money to this safe account" and "this is your new invoice account". Normal fraud checks miss it, because the customer, the device and the login are all genuine. Once the money has gone, it is very hard to get back. The best moment to step in is **just before the payment is released**.

**What APP Shield does.** Just before releasing a payment, the bank's payment channel (the mobile or web banking backend) asks APP Shield: *"Is this payment safe to send?"* APP Shield checks it against a set of clear rules and answers with one of four decisions:

| Decision | What it means | What the payment channel does |
|---|---|---|
| **ALLOW** | Nothing looks wrong | Sends the payment |
| **WARN** | Something looks a bit unusual | Shows the customer a scam warning that fits the situation, and sends the payment only if the customer confirms |
| **HOLD** | Something looks seriously wrong, **or** APP Shield can't check the payment properly | Does not send it. The payment goes into a queue for a fraud analyst to review |
| **REJECT** | The request itself is broken or invalid | Nothing is checked. The channel must fix the request |

Every answer except REJECT explains itself with **reason codes** (for example `PAYEE_MISMATCH`). It also names the **rule set version** used, so anyone can later see exactly why a decision was made.

**The one rule above all others:** when in doubt, hold. If anything APP Shield needs is unavailable or broken, the answer is HOLD, never ALLOW. This is called *failing closed*.

---

## 2. The journey of one payment

This is what happens, in order, when the payment channel sends a payment to `POST /payments/assess`.

```
  Payment channel
       │  "Is this payment safe?"
       ▼
  ① Give the request a tracking number (correlation ID)
  ② Basic checks: is it under 16 KB? Is it valid JSON? No repeated fields?  ── no ──► REJECT
  ③ Detailed checks: every field present and sensible?                     ── no ──► REJECT
  ④ Look up the payee on the mule account list
  ⑤ Run the scam rules
  ⑥ Decide: HOLD, WARN or ALLOW
  ⑦ Save the decision (and put a HOLD in the review queue)
  ⑧ Write one log line
       │
       ▼
  Answer back to the channel

  Anything unexpected going wrong in steps ④ to ⑦  ──►  HOLD (never ALLOW, never a crash)
```

**① Tracking number.** Each request gets a correlation ID. If the channel sends a valid one, it is used; otherwise APP Shield creates one. The same ID appears in the answer, the database and the log, so one payment can be traced end to end.

**② and ③ Checking the request.** Before any risk checking happens, the request must be well formed. Examples of what gets rejected:
- the amount is negative, zero, has 3 decimal places, or is sent as a number instead of text like `"250.00"`;
- the currency is not AUD;
- the BSB or account number is in the wrong format, **including look-alike digits from other alphabets**, such as fullwidth `１２３`;
- a required section is missing;
- there is an unknown extra field, or the same field appears twice.

A rejected request is **never** risk-checked, and the error message names the field but never repeats the value that was sent.

**④ Mule list check.** A *mule account* is a bank account used to receive and move stolen money. APP Shield checks the payee against a list of known mule accounts. In this project the list is a small local file of made-up accounts, standing in for a real bank service. It can be switched off to simulate an outage.

**⑤ and ⑥ Rules and decision.** See section 4.

**⑦ Saving.** The decision is saved to a database (SQLite). A HOLD also gets a row in the review queue, saved at the same moment, so a HOLD can never be saved without its queue entry.

**⑧ Logging.** One structured log line per decision, without sensitive details (see section 7).

**What APP Shield does not work out itself.** The channel tells APP Shield three things:
- the Confirmation of Payee result (does the name match the account?);
- whether a remote-access app is active;
- the customer's recent payment history.

APP Shield trusts these inputs, as the intent assumes. The only thing it looks up itself is the mule list.

---

## 3. "Missing" versus "unavailable"

This distinction matters:

- If the channel **leaves out** a required section, such as the Confirmation of Payee result, the request is broken: **REJECT**.
- If the channel sends the section but says `"status": "UNAVAILABLE"`, that means the service behind it is down. APP Shield can't check the payment safely: **HOLD**.

This keeps "the caller made a mistake" separate from "a system is down".

---

## 4. How the rules decide

### Three kinds of signal

Think of the rules as producing three kinds of flag:

| Kind | Meaning | Leads to |
|---|---|---|
| **Can't check** (DEPENDENCY) | Something needed to judge the payment is unavailable | **HOLD** |
| **Red flag** (HARD) | A strong sign of a scam | **HOLD** |
| **Amber flag** (SOFT) | A warning sign worth asking the customer about | **WARN** |

If no flags appear, the answer is **ALLOW**, with the reason `NO_RISK_SIGNAL`.

**Three amber flags together count as a red flag.** For example, a near-miss payee name, the word "crypto" in the reference, and four new payees added today: each on its own is a WARN, but together they become a HOLD (`MULTIPLE_RISK_SIGNALS`).

**All the rules run, even after a HOLD is certain.** This means the analyst sees every warning sign on a held payment, not just the first one found. It changes the list of reasons, never the decision.

### The rules, in plain words

Amounts below are the current settings in `config/rules.json`. They can be changed without touching code.

| Reason code | In plain words | Result | Warning type shown |
|---|---|---|---|
| `MULE_LIST_UNAVAILABLE` | The mule list is switched off, missing, unreadable, failing, or suspiciously small (for example empty). An empty list is never taken to mean "no mules" | HOLD | — |
| `COP_UNAVAILABLE` | The name-check service (Confirmation of Payee) is down | HOLD | — |
| `REMOTE_ACCESS_UNAVAILABLE` | We can't tell whether remote access is active | HOLD | — |
| `HISTORY_UNAVAILABLE` | The customer's payment history is unavailable | HOLD | — |
| `ENGINE_ERROR` | Something unexpected went wrong, or the decision couldn't be saved | HOLD | — |
| `MULE_ACCOUNT_MATCH` | The payee is a known mule account | HOLD | Mule payee |
| `REMOTE_ACCESS_NEW_PAYEE` | Someone may be remotely controlling the device, **and** the payee is new | HOLD | Remote-access scam |
| `PAYEE_MISMATCH_HIGH_VALUE` | The payee name doesn't match the account, and the amount is $5,000 or more | HOLD | Invoice scam |
| `NEW_PAYEE_VERY_HIGH_VALUE` | First payment to this payee, and $10,000 or more | HOLD | General scam |
| `MULTIPLE_RISK_SIGNALS` | Three or more amber flags at once | HOLD | from the first amber flag |
| `REMOTE_ACCESS_DETECTED` | Remote access is active, but the payee is known | WARN | Remote-access scam |
| `PAYEE_MISMATCH` | The payee name doesn't match the account (under $5,000) | WARN | Invoice scam |
| `PAYEE_CLOSE_MATCH` | The payee name is close, but not exact | WARN | Invoice scam |
| `NEW_PAYEE_HIGH_VALUE` | First payment to this payee, $1,000 to under $10,000 | WARN | General scam |
| `HIGH_VALUE_PAYMENT` | $25,000 or more, to any payee | WARN | General scam |
| `UNUSUAL_PAYMENT_PATTERN` | At least $500 **and** at least 5× the customer's 90-day average (or they have no recent payments) | WARN | General scam |
| `RAPID_PAYEE_ADDITIONS` | Three or more payees added in the last 24 hours | WARN | General scam |
| `SCAM_KEYWORD_IN_REFERENCE` | The payment reference contains words like "bitcoin", "crypto", "ATO" or "warrant" (whole words only, so "Cryptography" doesn't count) | WARN | Investment or impersonation scam |
| `NO_RISK_SIGNAL` | Nothing found | ALLOW | — |

**Deliberately, a new payee on its own does not trigger a warning.** People pay new payees all the time, and too many warnings teach customers to click through them. A new payee only matters together with the amount or another signal.

**A design choice to note:** remote access to a payee the customer has paid before is a WARN, not a HOLD. Some banks would hold any payment made while remote access is active. That is a business decision for the risk owners, and changing it would be a small rule change.

### Which warning to show

For WARN and HOLD, the answer includes a `scam_category`, such as *investment scam* or *remote-access scam*. The channel uses it to show a warning that matches what is probably happening to the customer, rather than a generic one.

---

## 5. When something breaks: failing closed

APP Shield sits directly in the payment path, so it must never let a payment through just because something went wrong.

| What goes wrong | What APP Shield does |
|---|---|
| The mule list is switched off, missing, corrupt, empty, or raises an error | Every payment is **HELD** (`MULE_LIST_UNAVAILABLE`), and `/health` says **degraded** |
| The channel reports the name-check, remote-access or history service as unavailable | **HOLD** with the matching reason |
| A bug or unexpected error while checking | **HOLD** (`ENGINE_ERROR`). Never a crash, never an error page the channel could misread |
| The decision can't be saved to the database | **HOLD** (`ENGINE_ERROR`). The log line records `persisted: false`, so there is still a trail |
| Someone edits the rules file and makes a mistake | The mistake is ignored, the last good rules stay in force, and `/health` says **degraded** (`config: stale`) |
| There are no valid rules at all when starting up | The service refuses to start |
| APP Shield itself is down or too slow | The channel must treat that as HOLD. This is part of the agreed contract with the channel, not something APP Shield can enforce |

**Why hold *every* payment during a mule-list outage?** It is the strictest and simplest reading of "fail closed", and the easiest to audit. The cost is that an outage stops all payments, so this needs revisiting with the business. See [decisions.md](decisions.md) (D2).

---

## 6. Changing the rules without a code release

All amounts, keyword lists and switches live in one file, `config/rules.json`:

```json
{
  "ruleset_version": "2026.11.1",
  "thresholds": {
    "new_payee_warn_amount": "1000.00",   "new_payee_hold_amount": "10000.00",
    "high_value_amount": "25000.00",      "payee_mismatch_hold_amount": "5000.00",
    "unusual_payment_multiplier": "5",    "unusual_payment_min_amount": "500.00",
    "rapid_payee_additions_24h": 3,       "soft_signal_escalation_count": 3
  },
  "reference_keywords": {
    "INVESTMENT_SCAM": ["crypto", "bitcoin", "investment", "trading"],
    "IMPERSONATION_SCAM": ["ato", "tax debt", "police", "warrant"]
  },
  "dependencies": {"mule_list_enabled": true, "mule_list_min_entries": 1}
}
```

- **Changes apply straight away.** APP Shield notices when the file changes and uses the new values on the next payment. There is no restart and no code change.
- **Bad edits are caught.** Every value is checked. For example, amounts must be positive, and the "warn" amount must be below the "hold" amount. A bad edit is rejected and the previous good rules stay in force.
- **Every decision records which rules were used:** the `ruleset_version` and a fingerprint (hash) of the exact file. Even if someone forgets to update the version number, the fingerprint proves which settings applied.
- **Anyone can see the active rules** at `GET /config/rules`. It contains no secrets.

---

## 7. What gets recorded, and what never does

### The audit record

Every decision is saved, so audit and compliance can see what was decided, why, and under which rules.

| Saved record | One per | What it contains |
|---|---|---|
| **Decision** | ALLOW, WARN or HOLD | Decision, reason codes, warning type, rule set version and config fingerprint, amount, masked payee, the signals the rules used, tracking number, time (UTC) |
| **Review queue entry** | HOLD | What an analyst needs: payment, customer, amount, masked payee, reasons, priority, status |
| **Rejection** | REJECT | Tracking number and which fields were wrong. Never the values that were sent |

**Review priority:**
1. Mule match, or a "can't check" problem.
2. Another red flag.
3. Three amber flags together.

Resolving a case (moving it out of `PENDING`) is outside this project's scope.

### Protecting customer data

The full account number, payee name and payment reference are **never stored and never logged**.

- **Masked payee:** the database keeps a masked form like `062-*** *****678`, which shows the bank and the last three digits.
- **Payee token:** a scrambled fingerprint of the account, made with a secret key (HMAC). An analyst can see that two payments went to the same payee without ever seeing the account number.
- **Log lines contain only safe fields:** decision, reasons, rule version, tracking number, payee token, and an **amount band** (e.g. "1k-10k") instead of the exact amount. Customer IDs are not logged either.
- **Errors are logged by type only** (e.g. "IntegrityError"), because an error message could accidentally contain payment details.
- **Tests check this automatically.** They search all log output **and the raw database file** for every account number, BSB, name and reference used in the test scenarios.

A real log line (shortened; the full line also has the decision ID, review ID, warning type, config fingerprint and timing):

```json
{"ts":"2026-11-08T23:40:49.979Z","level":"INFO","event":"payment_decision","correlation_id":"demo-hold-0001",
 "payment_id":"PMT-DEMO-0003","decision":"HOLD","reason_codes":["REMOTE_ACCESS_NEW_PAYEE","NEW_PAYEE_HIGH_VALUE","UNUSUAL_PAYMENT_PATTERN"],
 "ruleset_version":"2026.11.1","payee_token":"e237934f61835eb27272","amount_band":"1k-10k","degraded":false,"persisted":true}
```

---

## 8. Talking to APP Shield (the API)

| Endpoint | What it's for | Possible answers |
|---|---|---|
| `POST /payments/assess` | Check one payment before it is sent | 200 with ALLOW / WARN / HOLD. 422 REJECT (invalid). 409 REJECT (ID already used differently). 413 REJECT (over 16 KB) |
| `GET /review-queue` | List held payments for analysts, most urgent first | 200 (503 if the database is down) |
| `GET /review-queue/{payment_id}` | Full details of one held payment | 200, or 404 if it wasn't held |
| `GET /health` | Is everything working? | `ok` or `degraded`, with the state of the mule list, database and rules |
| `GET /config/rules` | Which rules are active right now | 200 |

Interactive documentation is at `/docs` while the service runs. The README has a full real example request and answer.

**Why ALLOW, WARN and HOLD all return HTTP 200.** They are all valid answers, not errors. The channel reads the `decision` field.

**Retries and duplicates.** Payment channels often retry when a request times out, so APP Shield handles repeats safely:
- **Same payment ID, same details:** the original answer is returned again, marked `Idempotent-Replay: true`. Even if 12 identical requests arrive at the same instant, they all get the **same single decision**, and only one review-queue entry is created.
- **Same payment ID, different details** (for example a changed amount): REJECT (409). This is suspicious, so the payment is not assessed.
- **A tracking number reused for a different payment:** REJECT (409).

**Known gap:** `/health` returns HTTP 200 even when it reports "degraded". Monitoring that only looks at the status code would miss it. Returning 503 while degraded would fix this.

---

## 9. How the code is organised

The core, meaning the rules and the decision logic, does no input or output of its own: it reads no files, touches no database and writes no logs. That makes it easy to test and to reason about. Everything that touches files, the database or logs sits around the edges.

| Part | Job |
|---|---|
| `app/main.py` | Starts the service and gives each request its tracking number |
| `app/api/payments.py` | The assess and review-queue endpoints. Coordinates the journey in section 2, handles duplicates, and turns any unexpected error into a HOLD |
| `app/api/operations.py` | The health and active-rules endpoints |
| `app/schemas.py` | Exactly what a valid request and answer look like |
| `app/services/validator.py` | Checks a request and lists what's wrong, without repeating the submitted values |
| `app/services/enrichment.py`, `mule_service.py` | Look up the mule list. Any problem becomes "unavailable", never a crash |
| `app/services/rules.py` | The rules: one small function each |
| `app/services/decision_engine.py` | Runs the rules and applies the order of importance |
| `app/services/audit_service.py`, `app/database.py` | Masks and tokenises the payee, saves decisions, and serves the review queue |
| `app/config.py` | Loads and checks the rules file, and notices when it changes |
| `app/logging_setup.py` | Writes the one-line JSON logs with UTC times |

**A weakness worth knowing:** the step-by-step coordination lives in the API file rather than in its own service module. It works and is tested, but a separate module would be cleaner as the system grows.

---

## 10. Risks and how they are handled

| Risk | How this project handles it | What production would add |
|---|---|---|
| A bug lets a risky payment through | Any error becomes HOLD; tests deliberately inject failures | Alerts on `ENGINE_ERROR` |
| Customer data leaks into logs | Only named safe fields are logged; automated scans of logs and the database | Central data-loss-prevention scanning |
| Look-alike input tricks the checks (other alphabets' digits, repeated fields) | Only plain 0–9 digits accepted; repeated fields rejected | Clean identifiers at the channel boundary |
| Someone sets dangerous thresholds | Values are range-checked; bad edits ignored; every decision records the exact rules | Approval workflow and signed config |
| The mule list fails or arrives empty | Treated as unavailable: every payment HELD, health degraded | A short-lived cached copy; alerts on sudden list shrinkage |
| Retries produce different answers | One stored decision per payment ID, replayed to every repeat, including simultaneous ones | A shared store across multiple servers |
| The channel's inputs are false | Trusted, as the intent allows | Signed inputs, or APP Shield calling those services itself |
| No login on any endpoint | Out of scope for this project; review data is minimised | Service-to-service authentication; analyst roles |
| Money rounding errors | Amounts are exact decimals, never floating-point numbers | — |
| Too many warnings annoy customers | New payee alone doesn't warn; the warning rate is visible in logs | Monitoring of warning and confirm-through rates per rule |
| Too much traffic for one small database | Fine for a local demo | A managed database (e.g. Postgres) |

---

## 11. Why it was built this way

| Choice | Why | The cost |
|---|---|---|
| Clear rules with reason codes, not machine learning or a "fraud score" | Every decision can be explained, tested and audited; the intent rules out ML | May miss brand-new scam patterns. A model could later add one more signal |
| Python with FastAPI and Pydantic | Strong, automatic request checking; API documentation for free | Slower than some languages, but the rules make no network calls, so it is fast enough |
| SQLite database | Runs on any laptop with no setup | Not built for high traffic or high availability |
| Rules in a JSON file, reloaded automatically | Risk owners can tune without a release | No approval workflow yet |
| Hold every payment during a mule-list outage | Safest and simplest to audit | An outage stops all payments |

---

## 12. What production would look like (not built)

- **Running it:** packaged as a container and run as at least three copies across data centres, behind an internal load balancer, with secure connections from the channel.
- **Data:** a managed database, plus a stream of decisions to the bank's event platform for analytics and case management.
- **Mule list:** a call to the real upstream service, with a short timeout and a small cache. It would still fail closed.
- **Rules and secrets:** rules kept in version control with risk-owner approval before release; the secret key kept in a secrets manager.
- **Monitoring:** dashboards and alerts for the decision mix, errors, degraded mode and response times.
- **Release process:**
  1. Every change runs the full test suite (`verify.sh`, already running in GitHub Actions).
  2. Then a test environment.
  3. Then "shadow mode": decisions are recorded but not acted on, and compared with the current system.
  4. Then a small live slice, then everyone.
  5. Rollback means redeploying the previous version or reverting the rules file.

---

## Glossary

| Term | Meaning |
|---|---|
| **APP scam** | Authorised push payment scam: the customer is tricked into sending the money themselves |
| **Payment channel** | The bank's mobile or web banking backend, which asks APP Shield before sending a payment |
| **BSB** | Bank-State-Branch number: the 6-digit code that identifies an Australian bank branch |
| **Confirmation of Payee (CoP)** | A check of whether the account name the customer typed matches the real account holder |
| **Mule account** | A bank account used to receive and pass on stolen money |
| **Remote access** | Screen-sharing or remote-control software. Scammers often ask victims to install it |
| **Fail closed** | When unsure or when something breaks, choose the safe option (HOLD), not the convenient one (ALLOW) |
| **Reason code** | A short label explaining why a decision was made, e.g. `PAYEE_MISMATCH` |
| **Rule set version / config fingerprint** | Which version of the rules made a decision, and a hash proving the exact settings used |
| **Correlation ID** | A tracking number that follows one request through the answer, the logs and the database |
| **Idempotent replay** | Sending the same request twice gives the same answer, without assessing or saving it twice |
| **HMAC token** | A scrambled fingerprint made with a secret key. It identifies the same payee without revealing the account number |
| **Degraded** | The service is running, but something it relies on isn't working, so it holds payments to stay safe |
