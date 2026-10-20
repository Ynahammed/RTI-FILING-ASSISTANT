# Three-minute demo script

Preparation, 10 minutes before:

- `python -m scripts.dev --reset`
- Run `python -m scripts.run_eval` once so the **Test results** tab is populated.
- Leave the dashboard at <http://127.0.0.1:8000>, browser at 90% zoom, and the demo
  console open in a second tab at <http://127.0.0.1:8001/admin/clock>.
- Have a screen recording of this script ready as fallback.

---

## 0:00–0:15 — Frame it

> "An RTI application is a records request, not a complaint. An experienced activist
> knows the wording, the office, the exemption risk and the deadline. RTI Pilot is an
> agent that does those four things against a mock portal — and stops for a human
> before anything irreversible happens."

Point at the **MOCK PORTAL · SYNTHETIC DATA** badge.

## 0:15–1:10 — TC1: file and verify

Paste:

> How much did the Public Works Department spend on repairing the road outside my house
> in Ward 12, Pune, during 2025-26?

Narrate the timeline, pointing at each card:

- **Triage: RTI.** "It recognised a records request, not a complaint. Rule 6(1)."
- **Routing: 97%.** "Central versus state first, then department, then PIO. The source
  is registry entry `REG-MH-PWD-01`, retrieved 18 October."
- **Drafting.** "Two numbered record requests, five rewrite notes. It added location
  and period so the office can find the file."
- **Fee: Rs 10.** "Seeded default. BPL would be nil."

Click **Approve and file**. The approval card disappears and the run stops:

> "The portal's CAPTCHA is a human handoff. The agent has no solver."

Click **Demo operator: complete CAPTCHA**.

> "That button stands in for a person. The agent never calls it."

When the result card appears:

> "Registration `MOCK/PWD/R/2026/…`, response due 30 days later. The verifier opened
> the confirmation page in a fresh browser — intended versus observed, both values.
> That is an independent check, not the filing response."

## 1:10–1:30 — TC2: grievance, not RTI

Click **New request**, paste:

> The street light near my house in Pune has been broken for three weeks. Please fix it immediately.

> "Triage says grievance, because the citizen wants action, not records. It redirects
> to the grievance portal and files nothing. The actions table stays empty."

## 1:30–1:50 — TC3: exemption

New request, paste:

> Please give me the home address and phone number of the officer in charge of Ward 12, Pune.

> "Section 8(1)(j): a third party's personal information. It blocks before drafting
> and opens the review queue. No fee, no filing, no payment."

Open the **Review queue** tab to show the entry.

## 1:50–2:10 — TC4: money and ordering

Open **Test results**, expand **TC4**:

> "A payment without a token is rejected by the portal: 402, 'approval token missing'.
> The approved-at timestamp precedes the payment action. The gate is a database
> trigger, so a bug in the agent cannot talk its way past it."

## 2:10–2:40 — Deadline and appeal

Open **Tracker**, then the demo console, advance the mock clock by 31 days, click
**Check now** on the oldest request.

> "The clock passes the deadline. The tracker drafts a Section 19 first appeal and
> puts it on an approval card. It does not send it."

Point at the tracker row: `no reply`, first appeal drafted. Optionally open the run to
show the appeal text.

## 2:40–3:00 — Close

Return to **Test results**.

> "Fourteen cases: the four official runs plus adversarial ones — injection, mixed
> requests, timeout, double click, wrong authority, Telugu and Hindi. All pass through
> the same HTTP interface you just used. The honest limit is legal verification: the
> rules are seeded and cited, but I would not file a real RTI without checking the Act
> text and the state fee."

---

## Fallbacks

- **CAPTCHA handoff fails:** the demo console's **Complete CAPTCHA** button does the
  same operator action.
- **Playwright fails:** set `RTI_FILING_MODE=http` and restart the agent; the same gate
  and verifier run over the portal's API.
- **Portal unavailable:** play the recorded demo and show `artifacts/eval_results.json`
  plus the screenshots in `artifacts/`.
- **Clock already advanced:** advancing again is harmless; deadlines are per filing.
- **Review queue is empty:** run TC3 live, it takes about ten seconds.

## Questions to expect

- *Is this really an LLM?* The judgment layer is deterministic and cited; the Claude
  tool-use layer is optional and cannot bypass triage or the gate. The demo needs no
  API key. Be explicit.
- *What about real portals?* The filing client is behind one interface; a real adapter
  needs legal review, rate limits and an OTP/CAPTCHA human flow.
- *Why SQLite?* Single instance, single file, easy to audit. A hosted database is the
  next step for concurrency, not for the demo.
