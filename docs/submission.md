# Submission checklist

The plan's checklist, with the current state of each item and where the evidence
lives. Nothing here claims a deployment that has not happened.

| Requirement | State | Evidence |
|---|---|---|
| Agent deployed on a public URL | **Not done** — container and Render blueprint ready, needs your account | `deploy/`, `deploy/README.md` |
| Mock portal deployed on a public URL | **Not done** — same | `deploy/Dockerfile.portal`, `deploy/render.yaml` |
| All four official cases run, with input, result and evidence saved | **Done** | `artifacts/eval_results.json`; `test_results` table; dashboard **Test results** |
| Adversarial runs and any failures documented | **Done** — X1–X10, plus a manual skewed-deadline mismatch check | `scripts/run_eval.py`, `artifacts/eval_results.json`, README |
| Source evidence: authority registry with source IDs and retrieval times | **Done** | `rules/authority_registry.json`; routing evidence in the ledger |
| Deck exported | **Outline done**, not exported to PPTX/PDF | `docs/deck.md` |
| Demo recording as fallback | **Not recorded**; screenshots captured | `artifacts/ui-*.png` |
| Agent and portal running locally | **Done** | `python -m scripts.dev` |

## Reproduce the evidence

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m scripts.dev --reset          # terminal 1
python -m scripts.run_eval             # terminal 2
python -m pytest
```

The evaluation writes `artifacts/eval_results.json`, uploads each result to
`POST /api/tests`, and leaves every run visible in the dashboard.

## What to export before submitting

1. **Deck**: convert `docs/deck.md` into eight slides. Add the architecture diagram
   from `docs/architecture.md` and the screenshots below.
2. **Demo recording**: follow `docs/demo-script.md`; 3 minutes, 1280×720 or better.
3. **Deployed URLs**: after deploying, set `RTI_PORTAL_URL` on the agent, run the
   evaluation against the deployed services, and paste both URLs into the deck.

## Screenshots already captured

- `artifacts/ui-initial.png` — empty four-zone dashboard
- `artifacts/ui-approval.png` — reasoning trail and approval card for TC1
- `artifacts/ui-verified.png` — filed result with verifier comparison
- `artifacts/ui-review.png` — review queue with Section 8(1)(j)
- `artifacts/ui-tracker.png` — tracker rows, transfer and appeal state
- `artifacts/ui-tests-final.png` — 14/14 results view
- `artifacts/ui-test-detail.png` — TC1 input, expected, actual
- `artifacts/ui-run-verified-recaptured.png` — a verified run opened from the results view (the earlier `ui-run-verified.png` was overwritten in error; delete it)
- `artifacts/portal-apply.png` — mock portal form with the mock banner

## Known gaps to state honestly on the deck

- No deployed URL yet; the deployment is prepared but not executed.
- Legal rules are seeded and cited, not verified against the Act text.
- The judgment engine is deterministic; the Claude layer is optional and disabled.
- Voice input is not built.
- SQLite is single-instance; a hosted database is the next step for concurrency.
