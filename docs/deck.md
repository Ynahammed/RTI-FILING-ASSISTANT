# RTI Pilot — deck outline

Eight slides, three minutes. Numbers in brackets map to the scoring criteria:
agentic capability (25), problem fit (20), technical implementation (20),
innovation (15), user experience (10), presentation (10).

---

## Slide 1 — Problem

**Title:** Filing an RTI is easy to get wrong

- Citizens ask for action, not records. Portals reject or misroute those requests.
- Vague wording gets a vague answer. "Why did this happen?" is not a records request.
- The fee is paid before anyone checks the destination. A wrong office wastes the fee.
- Nobody tracks the 30-day clock, so the first appeal window quietly closes.

**Speaker note:** RTI is a records law, not a complaint box. An experienced activist
knows the wording, the office, the exemption risk and the deadline. That is the gap.

---

## Slide 2 — What the agent does

**Title:** Plan, ask, draft, gate, file, verify, track

- Reads English, Telugu, Hindi, Marathi, Kannada or Tamil; asks one question at a time
  for missing fields.
- Labels the request RTI, grievance, mixed, exempt or uncertain, with a cited reason.
- Routes central vs state, then department, then PIO, with a confidence score.
- Rewrites opinion questions into numbered record requests and explains every change.
- Files only after a human approves destination, draft and fee.
- Re-opens the portal independently to verify the registration number and deadline.
- Runs the response clock and drafts a Section 19 first appeal when it lapses.

**Speaker note:** The reasoning trail is the product. Every claim has a source and a
retrieval time.

---

## Slide 3 — Architecture

**Title:** Two systems, one gate

- Agent service (FastAPI, SQLite) and mock portal service (FastAPI, own SQLite).
- Portal has its own database, so verification is genuinely independent.
- Playwright files the form and stops at the CAPTCHA.
- Shared HMAC approval token; the portal never reads the agent database.
- Evidence ledger records every decision, rule reference and source lookup.

**Visual:** the two-box diagram from `docs/architecture.md`.

**Speaker note:** The separation is the point. If the agent could write the portal's
database, "verification" would be theatre.

---

## Slide 4 — Safety gates

**Title:** Rules the database enforces, not the prompt

1. A payment or submit action cannot be inserted without an approved approval for the
   same draft and destination.
2. An idempotency key is unique, so a double click cannot file twice.
3. Verifications require the verifier's live lease.
4. Approved cards are immutable; declined runs create no actions.

- CAPTCHA is always a human handoff.
- Prompt injection inside the request is logged as data, stripped from the draft and
  cannot alter the gate.
- A suspected 8(1)(j) request is routed to human review before drafting and is never
  filed unattended; the deterministic rule still blocks outright.
- The operator surface needs a token, and each citizen gets a token for their own run
  only — 401 without it, 403 for someone else's run.

**Speaker note:** The first four are SQLite triggers. A bug in the agent cannot talk its
way past them, and the operator routes deny by default.

---

## Slide 5 — The four official runs

**Title:** TC1–TC4

| Run | Result |
|---|---|
| TC1 road repair spending | RTI, PWD Maharashtra 97%, Rs 10, filed, verified registration and 30-day deadline |
| TC2 broken street light | Grievance, redirected, zero actions |
| TC3 officer's home address | Section 8(1)(j), review queue, zero actions |
| TC4 fee | Token-less payment rejected, approval timestamp before payment, verified |

**Evidence to show:** registration `MOCK/PWD/R/2026/…`, the verifier comparison table,
the empty actions table for TC2 and TC3.

---

## Slide 6 — Adversarial runs

**Title:** Where the judgment shows

- X1 mixed: one run, two decisions, grievance link plus RTI draft.
- X2 missing state: asks, does not guess, does not route.
- X3 timeout: one payment, verification pending, registration recovered by idempotency.
- X5 injection: instruction logged, draft clean, gate still closed.
- X6 "why": rewritten as file notings and recorded reasons.
- X7 double click: one submission, one payment.
- X8 wrong authority: Section 6(3) transfer, tracker follows.
- X9 five non-English languages: same route as English.
- X10 deadline: Section 19 appeal drafted for approval.
- X11/X12 review: flag claimed, then upheld (closed) or released (approval card) — both
  recorded against the operator's name.

**Speaker note:** Failures stay in the results view. X3 is deliberately a pending
verification, not a success.

---

## Slide 7 — Results and honest failures

**Title:** 16/16 cases, with limits we can name

- 16/16 evaluation cases pass through the same HTTP interface the UI uses.
- 156 unit, gate and API tests pass.
- 40 real RTI-Bench requests through the live system: 100% reached a terminal state,
  0 duplicates, 0 payments without approval, p95 3.9 s at concurrency 4.
- The decision engine is deterministic; the Claude tool-use layer is an adapter point,
  not a dependency. The demo runs with no API key.
- Central rules are quoted from the Act with URL and retrieval date; the four state fee
  entries are still assumptions.
- SQLite is single-instance, so a Render restart can reset data.

**Speaker note:** The honest failure is the state fee, not the pipeline. We would not
file a real RTI without checking it.

---

## Slide 8 — What is next

**Title:** From mock to responsible production

- Verify the four state fee entries against their state rules; the central rules are
  already cited.
- Give the exemption review queue an owner: assignment, an SLA and an audit trail for
  each decision (the queue itself already exists).
- Support real portal adapters behind the same filing interface, with rate limits.
- Add voice intake (the first cut item) once the core is stable.
- Add a second reviewer for appeals and a notification channel for deadlines.
- Model evaluation: compare deterministic labels with Claude suggestions and log both.

**Ask:** a pilot with one state information commission's public documents, using
synthetic data until the legal review is complete.

---

## Appendix — scoring map

| Criterion | Slide / demo moment |
|---|---|
| Agentic capability (25) | Slides 2 and 6; timeline, UNCERTAIN, gate, verifier, tracker |
| Problem fit (20) | Slide 1; TC1/TC2 separation, plain-language chat |
| Technical implementation (20) | Slides 3 and 4; triggers, idempotency, separate databases |
| Innovation (15) | Slides 2 and 6; rewriter, mixed split, transfer, multilingual, appeal |
| User experience (10) | Four-zone dashboard, approval card, result card |
| Presentation (10) | Three-minute script, this deck, architecture diagram |
