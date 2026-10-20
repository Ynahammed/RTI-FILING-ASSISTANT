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
| Multilingual intake (English, Telugu, Hindi) with one follow-up per missing field | Working |
| Multi-label triage: RTI, grievance, mixed, exempt, uncertain, with cited reasons | Working |
| Section 8(1)(j) exemption screen that stops filing and opens a review queue | Working |
| Life-and-liberty urgency track (Section 7(1), 48 hours) | Working |
| Central vs state routing to a PIO, registry source ID and retrieval time, confidence threshold | Working |
| Query rewriter: splits compound asks, turns "why" into record requests, explains each change | Working |
| Fee card (Rs 10; nil for BPL where the seeded rules say so) | Working |
| Hard approval gate enforced by SQLite triggers, HMAC approval token, idempotent actions | Working |
| Playwright filing that stops at the portal's CAPTCHA for a human handoff | Working |
| Independent verifier that re-opens the confirmation page and compares values | Working |
| Deadline tracker, Section 6(3) transfer following, Section 19 first-appeal drafter | Working |
| Evidence ledger and reasoning timeline in the UI | Working |
| Evaluation harness: TC1–TC4 plus X1–X10 | 14/14 passing |
| Similar past RTI retrieval (synthetic precedents) | Working, optional feature |

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

Run the evaluation (both services must be up):

```bash
python -m scripts.run_eval
```

Run the unit and gate tests:

```bash
python -m pytest
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
scripts/          dev runner and evaluation harness
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
Section 19 appeal. Fees and state variations are explicitly marked as seeded
assumptions. Verify against the official Act text before any real filing.

---

## Evaluation

The harness talks to the agent's HTTP API exactly as the dashboard does, so a passing
run exercises the delivered interface, not an internal shortcut.

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
| X9 | Telugu/Hindi | Same route as the English request |
| X10 | Deadline passes | Section 19 appeal drafted for approval |

Latest local run: **14/14 passed**. Results are written to `artifacts/eval_results.json`
and stored in the `test_results` table, visible on the dashboard's **Test results** tab.

### Failure injection (demo console)

- **Timeout after payment, before confirmation renders** — verifier records pending.
- **Deadline shown wrong by N days** — verifier records a mismatch, never success.
- **Wrong authority** — portal transfers under Section 6(3).
- **Duplicate submission or payment** — idempotency returns the existing receipt.
- **CAPTCHA** — always a human handoff; the agent has no solver.

---

## Deployment

Both services ship as Docker images. The blueprint is
[deploy/render.yaml](deploy/render.yaml); the same Dockerfiles work on Railway or Fly.

1. Deploy the portal with `deploy/Dockerfile.portal`, then the agent with
   `deploy/Dockerfile.agent`.
2. Set the same `RTI_APPROVAL_SECRET` on both services. Set `RTI_ADMIN_TOKEN` on the
   portal and on the agent (the agent uses it only for the demo operator handoff).
3. Set `RTI_PORTAL_URL` on the agent to the portal's public URL.
4. Mount a persistent volume at `/app/data` if you want the SQLite files to survive
   restarts. Without one, a redeploy starts with an empty demo.

Deployment is not performed by this repository. Running `sites_deploy`, a cloud
platform CLI or a paid plan requires your own account and explicit authorization.

---

## Honest limitations

- **The decision engine is deterministic, not an LLM call.** Every label comes from a
  named rule, which is why the reasoning trail is reproducible and the demo works with
  no API key. The plan's Claude tool-use layer is an optional adapter point, not a
  dependency; adding it should not be allowed to bypass triage or the gate.
- **The legal rules are seeded.** The 30-day, 48-hour, five-day and exemption rules
  reflect general knowledge and are cited, but they were not checked against the Act
  text in this build. State fees are assumptions. Verify before any real filing.
- **SQLite on a single instance.** Fine for a demo; two replicas would diverge.
- **The portal's CAPTCHA is a placeholder**, not a real challenge. That is intentional:
  automated solving is out of scope.
- **Deployment configs are provided but not executed.** No public URLs exist until you
  deploy with your own accounts.
- **Voice input is not built** (first item in the plan's cut order).

## Security notes

- Approval tokens are HMAC-signed over approval ID, run ID, draft ID, fee, destination
  and approval time. The portal rejects a token whose destination or fee does not match
  the application.
- The approval token is stored server-side; the dashboard never renders it.
- The dashboard does not call operator endpoints directly. The demo console uses a
  separate token and is clearly labelled as an operator surface.
- Text inside a citizen's request is data. Instruction-like sentences are logged as
  evidence, stripped from the draft and cannot change the gate. See X5.
- Change `RTI_APPROVAL_SECRET` and `RTI_ADMIN_TOKEN` before exposing the services.
