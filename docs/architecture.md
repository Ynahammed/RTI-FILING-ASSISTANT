# Architecture

Two independently running services with separate SQLite databases:

| Service | Port | Database | Responsibility |
|---|---|---|---|
| Agent | 8000 | `data/agent.db` | intake, judgment, drafting, approval gate, filing, verification, tracking, dashboard |
| Mock portal | 8001 | `data/portal.db` | application form, CAPTCHA handoff, payment, acknowledgement, transfer, grievance, demo clock |

The verifier reads the portal's confirmation page. It never trusts the filing
response, and it never writes to the portal database. The services share the HMAC key
that signs approval tokens, plus the portal admin token the agent uses to complete the
demo CAPTCHA handoff; the agent's operator token is its own and the portal never sees
it.

```mermaid
flowchart LR
  Citizen[Citizen text<br/>EN / TE / HI / MR / KN / TA] --> Intake
  Intake --> Triage[Triage + Section 8 review router]
  Triage -->|grievance| Grievance[Grievance redirect<br/>no filing]
  Triage -->|8(1)(j) flag| Review[Review queue<br/>human decides<br/>no unattended filing]
  Triage -->|rti / mixed| Urgency
  Urgency --> Routing[Routing<br/>registry lookup]
  Routing --> Drafting[Query rewriter]
  Drafting --> Fee[Fee]
  Fee --> Gate{Approval gate<br/>SQLite trigger}
  Gate -->|approved + signed token| Filing[Playwright filing]
  Gate -->|declined| Closed[Nothing filed]
  Filing --> Captcha[CAPTCHA handoff<br/>human operator]
  Captcha --> Pay[Payment<br/>token + idempotency]
  Pay --> Verify[Independent verifier]
  Verify --> Tracker[Deadline tracker]
  Tracker --> Appeal[Section 19 appeal draft]
```

## Supervisor state machine

`runs.status` moves through:

```
intake
  ├─ awaiting_input ──▶ (answer) ──▶ intake
  ├─ review_queue ──▶ closed            (reviewer upholds the exemption)
  │             └──▶ intake            (reviewer releases it to filing)
  ├─ redirected              (grievance; terminal)
  └─ awaiting_approval
        ├─ declined          (terminal)
        └─ filing ──▶ awaiting_captcha ──▶ filing
              ├─ verified
              ├─ filed_unverified
              └─ failed
verified / filed_unverified
  └─ tracking ──▶ appeal_ready (terminal until the appeal is approved)
```

The supervisor is the only writer of run status. Decision agents return structured
results and write decisions/evidence; they never perform an irreversible action.

## Sequence: TC1 filed and verified

```mermaid
sequenceDiagram
  participant C as Citizen
  participant S as Supervisor
  participant A as Decision agents
  participant DB as agent.db
  participant P as Mock portal
  participant V as Verifier

  C->>S: road repair spending, Pune, 2025-26
  S->>A: intake
  A->>DB: fields + extraction evidence
  S->>A: triage (no exemption; records signal)
  A->>DB: decision rti (rule 6(1))
  S->>A: urgency (no life/liberty)
  S->>A: routing
  A->>DB: registry evidence REG-MH-PWD-01 + retrieved_at
  A->>DB: decision routed (confidence 0.97)
  S->>A: drafting + fee
  A->>DB: draft v1 + rewrite notes; fee Rs 10
  S->>DB: approval row status=pending
  S-->>C: approval card (destination, draft, fee)
  C->>S: approve
  S->>DB: approval status=approved + HMAC token
  S->>DB: INSERT action submit (trigger checks approval)
  S->>P: Playwright fills /apply and submits
  P-->>S: /captcha/{app_id}
  S-->>C: human handoff
  Note over P: demo operator completes CAPTCHA
  S->>DB: INSERT action payment (trigger checks approval)
  S->>P: Playwright pays with token + idempotency key
  P-->>S: /confirmation/MOCK/PWD/R/2026/00001
  S->>V: verify(run)
  V->>P: fresh browser opens confirmation page
  P-->>V: registration number, deadline, filing date
  V->>DB: verifications row (verifier lease required)
  V-->>S: matches intent
  S-->>C: registration number + deadline, observed vs intended
```

## Enforcement points

| Rule | Where | Test |
|---|---|---|
| No payment/submit without an approved approval for the same draft and destination | SQLite trigger `actions_require_approval` | `tests/test_gate.py` |
| One filing per idempotency key | `UNIQUE` index on `actions.idempotency_key`; dedupe in portal `payment_attempts` (accepted only) | gate tests + X7 |
| Approved cards are immutable | SQLite trigger `approvals_locked_after_approval` | gate test |
| Verifier-only writes | SQLite trigger `verifications_verifier_only` + short-lived lease | gate test |
| CAPTCHA never solved | Filing stops at `/captcha`; only an operator endpoint completes it | TC1 / demo |
| Instructions inside the request are data | Injection patterns logged as evidence, sentences stripped before drafting | X5 |
| Exemptions route to a human | deterministic 8(1)(j) rule or calibrated classifier at the review threshold; supervisor sets `review_queue` | TC3, `bench_review_routing` |
| Operator routes need the operator token | `agent/auth.py` dependency on every queue, tracker, test and captcha-handoff route | `tests/test_api_auth.py` |
| A citizen can only touch their own run | run token minted at creation, checked by `auth.authorize_run` | `tests/test_api_auth.py` |

The 8(1)(j) rule fires only when a private *record* (address, phone, health, money,
family, an identity document) meets a *subject* who is a person, or a named reference
("Dr Kulkarni"). An identity-only request — "names of the officers who approved" — is
treated as a request about a post unless the subject is a private individual, which is
the codebook's designation-is-public, home-address-is-not line. The rule stops a run
before drafting; the calibrated classifier only ever queues one for review.

## Access control

| Surface | Credential | What it opens |
|---|---|---|
| Citizen chat | none to start | create a run |
| A single run | run token, returned once at creation | read it, answer its follow-up, approve or decline its own card |
| Operator surface | `X-Operator-Token` = a per-account token, stored hashed; `RTI_OPERATOR_TOKEN` seeds the first account | review queue, all runs, tracker view, stored test results, CAPTCHA handoff |
| Portal demo console | `RTI_ADMIN_TOKEN`, entered once as a sign-in; the cookie carries it | mock clock, CAPTCHA completion, failure switches |

Accounts live in `operators` with a SHA-256 token hash, a role and a revocation time.
`python -m scripts.operator add --name priya` prints a token once; `revoke` stops it
without touching anyone else's. Every operator mutation writes an `operator_audit` row
with the account name, so "who released this flagged request" has an answer.

The dashboard keeps the run token in memory only and the operator token in
`sessionStorage` after the **Operator access** button is used. Neither the operator
token nor the portal admin token is ever rendered into a page, a draft or a log line;
`tests/test_portal_auth.py` opens `/admin/clock` and a CAPTCHA page and asserts the
string does not appear.

## Review ownership

`reviews` holds one row per flagged run: reason, source (`rule` or `classifier`), score,
a due time, and who claimed and decided it. A flag is created by the supervisor in the
same transaction that sets `review_queue`; the queue cannot contain a flag without a row.
Claiming is exclusive, deciding requires the claim, and releasing requires a written
reason. Releasing sets `review_released`, which the pipeline reads so the same rule does
not re-queue the run on resume. Upholding closes the run with `exemption_upheld`; no fee
and no filing exist in either case. `RTI_REVIEW_SLA_HOURS` (default 48) sets the target,
and anything past it is surfaced as overdue rather than silently aging. `tests/test_api_auth.py` checks that
every scoped route answers 401 without it, 403 for another client's run, and 404 for a
run id that does not exist. Run creation is rate-limited per client (default 120/min).

## Why an independent verifier

The filing response is the agent's own view of what happened. It can be wrong,
truncated by a timeout, or optimistic. The verifier therefore:

1. asks the portal for the registration number (read-only status lookup),
2. opens the confirmation page in a **new browser context**,
3. parses the registration number, displayed deadline, filing date and authority,
4. recomputes the deadline from the portal's own filing date and the Section 7(1)
   policy, and
5. writes the result, including a mismatch or pending state, through the verifier
   lease.

If the page is unavailable (X3), the row says `pending`, never `verified`.

## Failure modes and how they are exercised

| Failure | Portal control | Expected agent behaviour |
|---|---|---|
| Timeout after payment | `timeout_next_filing` | One payment; registration recovered by idempotency; verification pending |
| Duplicate submission | idempotent `/apply`; repeated key returns existing app | No second application |
| Duplicate payment | accepted-payment unique index | Existing receipt returned |
| Wrong authority | authority does not match canonical state/department | Portal transfers under Section 6(3); tracker follows |
| Deadline shown wrong | `skew_next_deadline_days` | Verifier records mismatch, not success |
| Evasive reply / no reply | operator reply + advance clock | Section 19 first-appeal draft |
| CAPTCHA | placeholder page | Agent stops; operator handoff |

## Approval token

Signed with HMAC-SHA256 over canonical JSON:

```json
{
  "approval_id": "APR-...",
  "run_id": "RUN-...",
  "draft_id": "DRF-...",
  "fee_inr": 10,
  "destination": "MH-PWD-01",
  "approved_at": "2026-10-19T10:00:00+00:00"
}
```

The portal verifies the signature and checks destination and fee against the
application. It never reads the agent database. `approved_at` gives the ordering
evidence for TC4.

## Portal adapters

Filing and verification talk to a `PortalAdapter`, never to a transport directly:

| Adapter | Used for |
|---|---|
| `HttpAdapter` | the hosted mock, where Chromium is not installed |
| `PlaywrightAdapter` | local runs, driving the same form in a real browser |
| `FixtureAdapter` | replaying recorded pages so the verifier can be tested with no portal |

The adapter is a transport only: it submits an already approved payload and returns
what the portal said. It holds no decision, and the gate stays in SQLite where the
transport cannot reach it. `RTI_FILING_MODE` selects the transport (`auto` picks HTTP in
production and Playwright locally).

## Deadline worker

`agent/worker.py` owns a `checks` queue in the same SQLite file: a filed request is
enqueued with its next check time, and a claim (with a lease) makes sure only one
worker runs a job. It starts as a thread inside the agent process by default, because
the hosted SQLite lives on the instance; `python -m agent.worker` runs the same loop
standalone. A restart re-reads the queue, so a 30-day deadline does not depend on
somebody having the dashboard open. `tests/test_worker.py` kills and restarts the loop
mid-claim and advances the clock.

## Deterministic judgment

`agent/nlu.py` holds the multilingual lexicons and patterns. Every label maps to a
named rule, and every routing decision carries a registry source. This is intentional:

- reproducible demo without an API key,
- no model call can bypass the gate,
- the reasoning trail is auditable.

If the plan's Claude tool-use layer is added, it should sit before the deterministic
checks as a suggestion provider, never as the authority for exemptions, fees or the
gate. The evidence ledger is the place to record the suggestion and its source.
