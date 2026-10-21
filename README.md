# RTI Pilot

An agent that behaves like an experienced RTI activist: it works out what to file,
where, and in what wording, files only after a human approves the exact card, then
verifies the result against a system it does not control and tracks the request to
its deadline.

**Everything runs against a mock RTI portal with synthetic applicant data.** No real
government portal, payment or personal data is involved. The mock is labelled on
every screen.

---

## What works today

| Capability | Status |
|---|---|
| Operator accounts (hashed tokens, add/revoke without a redeploy) on the queue, tracker, tests and CAPTCHA handoff; per-run tokens for citizens | Working |
| Multilingual intake (English, Telugu, Hindi, Marathi, Kannada, Tamil) with one follow-up per missing field | Working |
| Multi-label triage: RTI, grievance, mixed, exempt, uncertain, with cited reasons | Working |
| Section 8(1)(j) review routing with an owner: claim, 48-hour target, overdue marking, release or uphold, per-operator audit | Working |
| Jurisdiction registry: all 36 states and union territories routed, 4 with verified PIO entries | Working |
| Life-and-liberty urgency track (Section 7(1), 48 hours) | Working |
| Central vs state routing to a PIO, registry source ID and retrieval time, confidence threshold | Working |
| Query rewriter: splits compound asks, turns "why" into record requests, explains each change | Working |
| Fee card (Rs 10 under the 2012 Fee Rules; nil for BPL under section 7(5)) | Working |
| Hard approval gate enforced by SQLite triggers, HMAC approval token, idempotent actions | Working |
| Playwright filing that stops at the portal's CAPTCHA for a human handoff | Working |
| Independent verifier that re-opens the confirmation page and compares values | Working |
| Deadline tracker, Section 6(3) transfer following, Section 19 first-appeal drafter | Working |
| Durable deadline worker: queued checks run on a timer, so a lapsed deadline needs nobody watching | Working |
| Portal adapter seam: HTTP, Playwright and recorded-fixture transports behind one interface | Working |
| Evidence ledger and reasoning timeline in the UI | Working |
| Evaluation harness: TC1–TC4 plus X1–X12 | 16/16 passing |
| Similar past RTI retrieval (synthetic precedents) | Working, optional feature |
| Citizen-request annotation: consent + redaction, two independent labels, kappa, adjudication, frozen SHA-256 split | Working; 0 of 100 rows collected |

Deliberately not built: voice input (first stretch item in the plan's cut order),
automated CAPTCHA/OTP solving, real payments, real portals.

---

## Quickstart

Requirements: Python 3.11+, Playwright Chromium.

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m scripts.dev --reset
```

Open:

- Agent + dashboard: <http://127.0.0.1:8000>
- Mock portal: <http://127.0.0.1:8001>
- Demo console (clock, CAPTCHA handoff, failure switches): <http://127.0.0.1:8001/admin/clock>
  — sign in once with `RTI_ADMIN_TOKEN` (`rti-demo-admin` locally). The token is never
  rendered into a page.

The **Assistant** tab works as a citizen: type a request, answer a follow-up, approve the
card. The **Review queue**, **Tracker** and **Test results** tabs, and the CAPTCHA
handoff, are the operator surface and need an account: click **Operator access** and paste
a token (`rti-demo-operator` locally; in production the token from `python -m
scripts.operator add --name ...`, printed once). Each run gets its own run token, so one
browser cannot open another citizen's run.

A flagged request is work, not a status. Open **Review queue**, **Claim** it, then either
**Release to filing** (a reason is required) or **Uphold exemption** (the run closes with
nothing filed). Both write a decision and an audit row against your account name; the
queue marks anything past its 48-hour target as overdue. `RTI_REVIEW_SLA_HOURS` changes
the target.

Run the evaluation (both services must be up):

```bash
python -m scripts.run_eval
```

Run the unit and gate tests:

```bash
python -m pytest
```

Collect citizen-request evidence (needs two human annotators; see
[docs/citizen-data.md](docs/citizen-data.md)):

```bash
python -m scripts.annotate_server --port 8010
# contributor: http://127.0.0.1:8010/contribute
# annotator:   http://127.0.0.1:8010/label?annotator=A
# adjudicator: http://127.0.0.1:8010/adjudicate?name=C
python -m scripts.annotate_citizen stats     # progress toward 100
python -m scripts.annotate_citizen freeze    # refuses below 100 rows or kappa 0.6
```

Environment overrides live in [.env.example](.env.example). Defaults work offline.

---

## Demo walkthrough (about three minutes)

1. **TC1 — file and verify.** Paste:
   *"How much did the Public Works Department spend on repairing the road outside my house in Ward 12, Pune, during 2025-26?"*
   Watch the reasoning trail: RTI label, PWD Maharashtra at 97% with registry source
   `REG-MH-PWD-01` and retrieval time, rewrite notes, Rs 10 fee. Click
   **Approve and file**, then **Demo operator: complete CAPTCHA**. The verifier
   re-opens the portal in a fresh browser and the result card shows registration
   number, deadline and an intended-versus-observed comparison.
2. **TC2 — grievance, not RTI.** *"The street light near my house in Pune has been broken for three weeks. Please fix it immediately."*
   Triage says grievance, the run is redirected, and the actions table stays empty.
3. **TC3 — exemption.** *"Please give me the home address and phone number of the officer in charge of Ward 12, Pune."*
   Section 8(1)(j) fires, filing is blocked, and the run appears in **Review queue**.
4. **TC4 — money and ordering.** Run TC1 again. While the run waits at the CAPTCHA
   handoff, the portal rejects a payment without a token. After approval and handoff,
   the approved-at timestamp precedes the payment action.
5. **Stretch — deadline and appeal.** Open the tracker, use the demo console to
   advance the mock clock 31 days, click **Check now**. A Section 19 first appeal
   appears for approval. The X8 path (wrong authority) shows a Section 6(3) transfer
   followed by the tracker.

See [docs/demo-script.md](docs/demo-script.md) for a timed script and
[docs/deck.md](docs/deck.md) for the eight-slide deck.

---

## Architecture

Two services, two databases, one shared HMAC secret. The verifier checks a system the
agent does not control.

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ Agent (FastAPI, :8000)      │        │ Mock portal (FastAPI, :8001) │
│                             │        │                              │
│ supervisor                  │        │ application form             │
│  intake                     │        │ CAPTCHA handoff              │
│  triage + exemption         │        │ payment (token + idempotency)│
│  urgency                    │        │ confirmation page            │
│  routing (registry file)    │        │ Section 6(3) transfer        │
│  drafting + fee             │        │ grievance page               │
│  approval gate (SQLite)     │        │ demo clock + failure switches│
│  filing (Playwright)  ──────┼───────▶│                              │
│  verifier (fresh browser) ──┼───────▶│                              │
│  tracker + appeal drafter   │        │ SQLite: portal.db            │
│ SQLite: agent.db            │        │                              │
└─────────────────────────────┘        └──────────────────────────────┘
        ▲                                            ▲
        └──────── HMAC approval token ───────────────┘
          (portal verifies; it never reads agent.db)
```

More detail, including the sequence for a filing and every failure mode:
[docs/architecture.md](docs/architecture.md).

### The three rules enforced in code

1. `actions` rows of type `payment` or `submit` cannot be inserted without an
   `approvals` row with `status='approved'` for the same draft and destination
   (SQLite `BEFORE INSERT` trigger).
2. `actions.idempotency_key` is unique, so a double click or retry returns the stored
   result rather than filing again.
3. `verifications` rows require the verifier service's live lease token, so only the
   verifier can write them.

An approved approval is immutable: it cannot be declined or re-tokened afterwards.
These are tested in [tests/test_gate.py](tests/test_gate.py).

---

## Repository map

```
agent/            supervisor, decision agents, filing, verifier, tracker, dashboard
portal/           mock RTI portal with its own database and demo console
common/           config, HMAC approval tokens
rules/            seeded RTI rules and authority registry with source IDs
scripts/          dev runner, evaluation harness, RTI-Bench benchmarks
tests/            gate, idempotency and NLU tests
docs/             architecture, deck, demo script
deploy/           Dockerfiles and Render blueprint
agent/static/     four-zone dashboard (plain HTML/CSS/JS)
```

### Data model (agent database)

`runs`, `fields`, `decisions`, `evidence`, `drafts`, `approvals`, `actions`,
`verifications`, `tracker`, `test_results`, `precedents`, `service_leases`.
Every row carries a run ID. Evidence rows carry a source ID, source URL and retrieval
time, including the registry lookup used for routing.

### Rules file

[rules/rti_rules.json](rules/rti_rules.json) holds the 30-day period, the 48-hour
life-and-liberty period, the five-day Section 6(3) transfer, Section 8(1)(j) and the
Section 19 appeal. Each of those five rules carries a quote, an official URL, a retrieval
date and a verification flag, and `scripts/check_rules` fails the build if one loses its
citation. State fee entries are still explicitly marked as unverified seeded assumptions:
state rules under sections 27 and 28 vary and were not checked here. Verify any state fee
before a real filing.

---

## Evaluation

The evaluation has two layers, and they answer different questions.

**Statistical benchmarks** score the decision components against RTI-Bench, the public
corpus of Central Information Commission decisions. Measured on 1,084 labelled cases
of 1,218 (134 `UNKNOWN` excluded):

| Benchmark | Metric | Score |
|---|---|---|
| Section 8(1)(j) classifier | ROC AUC / nested F1 | 0.7170 / 0.2980 |
| Section 8(1)(j) keyword rule | F1 (baseline) | 0.1449 |
| Multi-label exemptions, 5 sections | macro AP / macro AUC | 0.2269 / 0.7245 |
| Multi-label, base rate | macro AP | 0.0638 (3.55× lift) |
| Precedent retrieval | P@10 / MRR | 0.1705 / 0.3452 |
| Retrieval chance | P@10 | 0.0807 (2.11× lift) |
| Multi-label calibration | ECE before → after isotonic | 0.1046 → **0.0127** |
| Binary calibration | ECE before → after isotonic | 0.1259 → **0.0312** |
| Binary selective precision | at 25% / 50% coverage | 0.2177 / 0.1642 |
| Retrieval, same authority | P@10 vs chance | 0.0274 vs 0.0066 (4.15×) |
| Source B | documents carrying order text | 17 of 298 (not scoreable) |
| Citizen requests | adjudicated rows | **0 of 100** — collection app ready, [protocol](docs/citizen-data.md) |
| Operations, 40 real requests, C=4 | completion / verified | **100% / 80%** |
| Exemption review routing | recall / precision / review load | 0.3950 / 0.3072 / 14.11% |
| Review routing, keyword rule alone | recall | 0.0840 (4.7× lower) |
| Citizen-phrasing fixtures | rule recall on 140 author-written rows | 1.00 in-sample — **not evidence** |

```bash
python -m scripts.bench_all             # runs every benchmark and enforces gates
python -m scripts.bench_all --with-eval # also runs the 16 scenarios below
python -m scripts.bench_all --with-ops  # also runs 40 real requests through the live system
```

`bench_all` exits non-zero when a score falls below its floor, and gates on the 14
scenarios below when their artifact is present. All sixteen gates currently pass. Full
methodology, calibration numbers and the limitations are in
[docs/benchmarks.md](docs/benchmarks.md).

The benchmarks run on the real public RTI-Bench dataset in `data/rti-bench/`, not on
synthetic data. Source B's 298 orders carry labels but no request text, so they are
examined rather than scored: `bench_source_b` reports why, and measures the transfer gap.

**End-to-end scenarios.** The harness talks to the agent's HTTP API exactly as the
dashboard does, so a passing run exercises the delivered interface, not an internal
shortcut. It needs both services up.

| ID | Scenario | Expected |
|---|---|---|
| TC1 | Road repair spending | RTI, PWD Maharashtra, filed after approval, verified |
| TC2 | Complaint, not information | Grievance redirect, nothing filed |
| TC3 | Third-party personal data | 8(1)(j), review queue, filing blocked |
| TC4 | Rs 10 fee | Approval before payment; token-less payment rejected |
| X1 | Mixed request | Grievance link plus RTI draft, two decisions |
| X2 | State missing | Asks, does not guess, does not route |
| X3 | Timeout after payment | Verifier pending, one payment, registration recovered |
| X4 | Approval declined | Nothing filed |
| X5 | Prompt injection | Ignored and logged, gate intact |
| X6 | "Why did the officer…" | Rewritten as a records request |
| X7 | Double-click approve | One filing |
| X8 | Wrong authority | Section 6(3) transfer, tracker follows |
| X9 | Telugu/Hindi/Marathi/Kannada/Tamil | Same route as the English request |
| X10 | Deadline passes | Section 19 appeal drafted for approval |
| X11 | Review upheld | Flag claimed, exemption upheld, run closed, nothing filed, decision attributed |
| X12 | Review released | Flag claimed, release recorded with a reason, run reaches the approval card |

Latest local run: **16/16 passed**. Results are written to `artifacts/eval_results.json`
and stored in the `test_results` table, visible on the dashboard's **Test results** tab.
Benchmark scores are written to `artifacts/benchmarks.json`.

### Failure injection (demo console)

- **Timeout after payment, before confirmation renders** — verifier records pending.
- **Deadline shown wrong by N days** — verifier records a mismatch, never success.
- **Wrong authority** — portal transfers under Section 6(3).
- **Duplicate submission or payment** — idempotency returns the existing receipt.
- **CAPTCHA** — always a human handoff; the agent has no solver.

---

## Deployment

The Render blueprint is [deploy/render.yaml](deploy/render.yaml). The public services
are:

| Service | URL |
|---|---|
| Mock portal | <https://rti-pilot-portal.onrender.com> |
| Agent + dashboard | <https://rti-pilot-agent.onrender.com> |

The Render Free configuration uses `deploy/Dockerfile.portal` for the portal and
`deploy/Dockerfile.agent.free` for the agent. It sets `RTI_FILING_MODE=http`, so the
hosted demo uses the portal API rather than launching Chromium; local runs can still
use Playwright.

Both services must have the same `RTI_APPROVAL_SECRET`. The agent additionally needs
`RTI_OPERATOR_TOKEN` (guards the operator surface) and `RTI_PORTAL_URL` set to
`https://rti-pilot-portal.onrender.com`. Generate all three with
`python -m scripts.gen_secrets`, which writes `deploy/.secrets.env`, prints nothing,
and is safe to re-run with `--force` to rotate.

`RTI_ENV=production` refuses to start on a missing, placeholder, short or reused secret,
or on a placeholder `RTI_PORTAL_URL`, so a misconfigured deployment fails at boot instead
of at the first filing.

**Free-tier caveats:** SQLite files live on ephemeral storage and may be reset when a
service restarts, redeploys, or spins down. Free services sleep after inactivity and
take time to wake; the workspace's free instance-hours are shared between services.
Use this deployment for demos/evaluation, not durable storage or always-on service.
For Render setup details, see [deploy/README.md](deploy/README.md).

**Last verified deployment: 2026-10-21.** Both services answered (portal `/healthz` and
agent `/api/health` 200), but the agent was **misconfigured**: `RTI_PORTAL_URL` still
pointed at `https://example.invalid`, so filings failed after the submit step and the
deployed evaluation scored 8/14.

That configuration can no longer start. In `RTI_ENV=production` the agent refuses to boot
on a placeholder portal URL, a missing or short secret, or one secret reused for two
roles, and `/api/ready` returns 503 naming the portal it cannot reach. Set
`RTI_PORTAL_URL` to the portal's public URL, set `RTI_OPERATOR_TOKEN` on the agent, and
redeploy; then re-run the evaluation against the public URLs before calling it verified.

---

## Honest limitations

- **The decision engine is deterministic, not an LLM call.** Every label comes from a
  named rule, which is why the reasoning trail is reproducible and the demo works with
  no API key. The plan's Claude tool-use layer is an optional adapter point, not a
  dependency; adding it should not be allowed to bypass triage or the gate.
- **The legal rules are cited, the state fees are not.** Sections 6(3), 7(1), 8(1)(j),
  19 and the Rs 10 central fee are quoted from the Act and the 2012 Fee Rules, with URL
  and retrieval date, and a test fails if a citation disappears. The four state fee
  entries are assumptions made under sections 27 and 28 and are labelled as such. Verify a
  state fee before relying on it.
- **The citizen gap was real, and the fixtures that closed it prove nothing.** A
  synthetic development check (author-written, author-labelled, explicitly not evidence)
  first put the keyword screen at 0.14 recall on citizen-style wording against 0.39 on
  appellate text. The screen's vocabulary now covers those phrasings, so it scores 1.00 on
  the two fixtures it was fitted to — that is memorisation, not generalisation. On
  appellate text the same rule stays conservative: 0.53 precision at 0.08 recall. Only the
  real 100-row set can say whether the fix transfers.
- **SQLite on a single instance.** Fine for a demo; two replicas would diverge.
- **The portal's CAPTCHA is a placeholder**, not a real challenge. That is intentional:
  automated solving is out of scope.
- **Filing is proven against the bundled mock only.** The `PortalAdapter` seam means a
  real portal is a transport, not a rewrite, and the verifier is tested by replaying
  recorded pages — but no real government portal has been driven.
- **The deployed agent points at a placeholder portal URL**, so it currently fails 6 of
  14 deployed scenarios. The fix is one environment variable plus a redeploy; it cannot be
  applied from this repository alone.
- **Voice input is not built** (first item in the plan's cut order).

## Security notes

- Approval tokens are HMAC-signed over approval ID, run ID, draft ID, fee, destination
  and approval time. The portal rejects a token whose destination or fee does not match
  the application.
- The approval token is stored server-side; the dashboard never renders it.
- The dashboard does not call operator endpoints directly. The demo console requires a
  login with `RTI_ADMIN_TOKEN` and is clearly labelled as an operator surface. No page
  renders either token: opening `/admin/clock` or a `/captcha/` page without signing in
  shows a sign-in form, not the secret.
- Text inside a citizen's request is data. Instruction-like sentences are logged as
  evidence, stripped from the draft and cannot change the gate. See X5.
- Hosted images set `RTI_ENV=production`. In that mode both services refuse to start if
  `RTI_APPROVAL_SECRET` or `RTI_ADMIN_TOKEN` is missing, a known placeholder, shorter than
  32 characters, or the same value as the other. Errors never print the secret. Local runs
  (`RTI_ENV=local`, the default) keep the demo values.
