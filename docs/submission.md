# Submission checklist

The plan's checklist, with the current state of each item and where the evidence
lives. Where a claim depends on a deployment or a measurement, the artifact is named.

| Requirement | State | Evidence |
|---|---|---|
| Agent deployed on a public URL | **Deployed, needs one fix** — `RTI_PORTAL_URL` still points at `example.invalid`, so filings fail after submit and the deployed evaluation scores 8/14. Production now refuses that value at boot. | <https://rti-pilot-agent.onrender.com>; README §Deployment; `GET /api/ready` |
| Mock portal deployed on a public URL | **Done** | <https://rti-pilot-portal.onrender.com>; `GET /healthz` |
| All four official cases run, with input, result and evidence saved | **Done** | `artifacts/eval_results.json`; `test_results` table; dashboard **Test results** |
| Adversarial runs and any failures documented | **Done** — X1–X12, including five non-English languages, both review decisions and a manual skewed-deadline check | `scripts/run_eval.py`, `artifacts/eval_results.json`, README |
| Review queue with an owner | **Done** — claim, 48-hour target with overdue marking, release or uphold, every decision on the operator audit trail | `agent/db.py`, `agent/main.py`, `tests/test_review_workflow.py` |
| Operator accounts and rotation | **Done** — hashed tokens, add/revoke without a redeploy, audit per operator | `scripts/operator.py`, `tests/test_operators.py` |
| State fees sourced | **Done, with a caveat** — each state entry now cites an official source; none is gazette-verified, and Telangana's tiered schedule is recorded rather than flattened | `rules/rti_rules.json`, `tests/test_rules_provenance.py` |
| Source evidence: authority registry with source IDs and retrieval times | **Done** — 36 jurisdictions covered, 51 entries, 4 with verified PIO records | `rules/authority_registry.json`; `artifacts/registry_coverage_results.json` |
| Statutory rules cited, not asserted | **Done** — 5 rules quoted with URL, retrieval date and verification flag; a test fails on a missing citation | `rules/rti_rules.json`; `scripts/check_rules.py`; `artifacts/rules_provenance_results.json` |
| Operator access control | **Done** — operator token on queue/tracker/tests/CAPTCHA; a run token scopes each citizen to their own run | `agent/auth.py`; `tests/test_api_auth.py` |
| Exemption review routing measured, not asserted | **Done** — recall 0.3950 at 0.3072 precision, 14.11% review load, nested threshold re-chosen | `scripts/bench_review_routing.py`; `artifacts/review_routing_results.json` |
| Citizen-phrasing robustness | **Done as fixtures, not evidence** — the screen was rebuilt after two author-written sets exposed 0.14 recall, then 0.55 on fresh phrasing; both now saturate, so they guard regressions and cannot be quoted as accuracy (`scripts/bench_citizen_dev.py`) | `agent/nlu.py`; `data/dev/`; [docs/citizen-data.md](citizen-data.md) |
| Deadlines enforced without anyone watching | **Done** — durable check queue with a leased claim and a restart test | `agent/worker.py`; `tests/test_worker.py` |
| Real requests through the live system | **Done** — 40 requests at C=1/4/8, 0 errors, 0 duplicates, p95 3.92 s at C=4 | `scripts/bench_operations.py`; `artifacts/operations_*.json` |
| Path to citizen-request accuracy | **Tooling done, data not collected** — contribution form with consent and redaction, two-annotator labelling, adjudication, kappa CI, frozen split; target 100 rows | `scripts/annotate_server.py`; `scripts/annotate_citizen.py`; [docs/citizen-data.md](citizen-data.md) |
| Deck exported | **Outline done**, not exported to PPTX/PDF | `docs/deck.md` |
| Demo recording as fallback | **Not recorded**; screenshots captured | `artifacts/ui-*.png` |
| Agent and portal running locally | **Done** | `python -m scripts.dev` |

## Reproduce the evidence

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m scripts.dev --reset          # terminal 1
python -m scripts.run_eval             # terminal 2
python -m scripts.bench_all --with-eval --with-ops
python -m pytest
```

The evaluation writes `artifacts/eval_results.json`, uploads each result to
`POST /api/tests`, and leaves every run visible in the dashboard. `bench_all` writes
`artifacts/benchmarks.json` and exits non-zero if any gate fails.

## What to export before submitting

1. **Deck**: convert `docs/deck.md` into eight slides. Add the architecture diagram
   from `docs/architecture.md` and the screenshots below.
2. **Demo recording**: follow `docs/demo-script.md`; 3 minutes, 1280×720 or better.
3. **Deployed URLs**: set `RTI_PORTAL_URL` on the agent to the portal's public URL, set
   `RTI_OPERATOR_TOKEN`, redeploy, confirm `/api/ready` returns 200, then run the
   evaluation against the deployed services and paste both URLs into the deck.

## Screenshots already captured

- `artifacts/ui-initial.png` — empty four-zone dashboard
- `artifacts/ui-approval.png` — reasoning trail and approval card for TC1
- `artifacts/ui-verified.png` — filed result with verifier comparison
- `artifacts/ui-review.png` — review queue with Section 8(1)(j)
- `artifacts/ui-tracker.png` — tracker rows, transfer and appeal state
- `artifacts/ui-tests-final.png` — results view (captured at 14/14; the harness now runs 16)
- `artifacts/ui-test-detail.png` — TC1 input, expected, actual
- `artifacts/ui-run-verified-recaptured.png` — a verified run opened from the results view (the earlier `ui-run-verified.png` was overwritten in error; delete it)
- `artifacts/portal-apply.png` — mock portal form with the mock banner

## Known gaps to state honestly on the deck

- The hosted agent is verified end to end: **16/16** scenarios against the deployed
  agent and portal (2026-10-22, `artifacts/eval_remote.json`). It depends on three
  environment variables that live in the host, not this repository: `RTI_PORTAL_URL`,
  `RTI_OPERATOR_TOKEN`, and an `RTI_APPROVAL_SECRET` identical to the portal's.
- State fees are reported from official state sources but not gazette-verified, and
  Telangana's schedule is tiered by authority level while the registry records no level.
  The central rules are quoted from the Act and the 2012 Fee Rules.
- The review queue now has an owner and an audit trail, but no notification when a flag
  is waiting or overdue; someone has to open the queue.
- No real government portal has been driven; filing is proven against the bundled mock,
  and the verifier against recorded pages.
- Citizen-request accuracy is unmeasured: 0 of the 100 adjudicated rows exist. The
  collection and labelling tooling is built and refuses to score anything unfrozen or
  low-agreement, but the data needs two human annotators and about three weeks.
- The judgment engine is deterministic; the Claude layer is optional and disabled.
- Voice input is not built.
- SQLite is single-instance; a hosted database is the next step for concurrency.
