"""Labelling UI for citizen-request annotation — run it on the annotator's laptop.

This exists because the fastest credible path to 100 adjudicated rows is not
CSV files and a shared spreadsheet: it is a page one non-engineer can use for
twenty minutes without a briefing on JSON. It is deliberately small and local:

* contributors submit their own request, tick consent, and get it redacted;
* two annotators label independently, one request at a time;
* a third person resolves disagreements on an adjudication page.

There is no login. Run it on loopback only (the default) and hand the machine or
a tunnel to the annotator; do not expose it as a service.

    python -m scripts.annotate_server --port 8010
    # annotator A: http://127.0.0.1:8010/label?annotator=A
    # adjudicator: http://127.0.0.1:8010/adjudicate?name=C
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from scripts import annotate_citizen as ac

app = FastAPI(title="RTI Pilot citizen annotation", docs_url=None, redoc_url=None)

CONSENT_TEXT = (
    "I wrote this request about a real matter, or I have the author's permission to share it. "
    "I understand it will be redacted, labelled by two people, and may be published as part "
    "of an evaluation set. I have not included anyone's name, address, phone number or "
    "identity document in the text."
)

CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.55 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f6f7f9; color: #17202a; }
main { max-width: 780px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 24px 0 10px; }
p.lede, p.hint { color: #5b6770; }
.card { background: #fff; border: 1px solid #e3e6ea; border-radius: 12px; padding: 22px; margin: 18px 0; }
.request { font-size: 19px; line-height: 1.6; background: #fbfcfd; border-left: 4px solid #0e7490; padding: 16px 18px; border-radius: 6px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
button { font: inherit; padding: 11px 18px; border-radius: 8px; border: 1px solid #c8d0d8;
         background: #fff; color: #17202a; cursor: pointer; }
button:hover { border-color: #0e7490; }
button.primary { background: #0e7490; border-color: #0e7490; color: #fff; }
button.warn { background: #b45309; border-color: #b45309; color: #fff; }
progress { width: 100%; height: 10px; }
.meta { color: #5b6770; font-size: 14px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2f6; font-size: 13px; }
label.inline { display: flex; gap: 8px; align-items: flex-start; margin: 14px 0; }
textarea, input[type=text] { width: 100%; font: inherit; padding: 10px; border: 1px solid #c8d0d8;
                           border-radius: 8px; box-sizing: border-box; background: #fff; color: #17202a; }
a { color: #0e7490; }
"""


def store() -> Path:
    return Path(os.getenv("RTI_CITIZEN_ANNOTATIONS", str(ac.ANNOTATIONS)))


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><main>{body}</main></body></html>"""
    )


def _annotators_or_default(value: str | None) -> str:
    return (value or "").strip()


def next_unlabelled(rows: list[dict[str, Any]], annotator: str, exclude: set[str]) -> dict[str, Any] | None:
    for row in rows:
        if row.get("id") in exclude:
            continue
        if not isinstance((row.get("labels") or {}).get(annotator), bool):
            return row
    return None


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "store": str(store())}


@app.get("/api/progress")
def api_progress(target: int = ac.TARGET_ROWS) -> dict[str, Any]:
    rows = ac.load(store())
    return {
        "progress": ac.progress(rows, target),
        "agreement": ac.agreement(rows),
        "question": ac.LABEL_QUESTION,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    rows = ac.load(store())
    state = ac.progress(rows)
    verdict = ac.agreement(rows)
    kappa = verdict.get("cohen_kappa") if verdict.get("evaluated") else "—"
    ci = verdict.get("kappa_ci95") if verdict.get("evaluated") else None
    body = f"""
<h1>Citizen-request annotation</h1>
<p class="lede">Target: <strong>{state['resolved']} of {state['target']}</strong> resolved rows.</p>
<progress value="{state['resolved']}" max="{state['target']}"></progress>
<div class="card">
  <p class="meta">Candidates {state['candidates']} · pairs {state['pairs']} ·
  open disagreements {state['disagreements_open']} ·
  positives {state['positives']} / negatives {state['negatives']}</p>
  <p class="meta">Cohen's kappa <strong>{kappa}</strong>
  {f'(95% CI {ci[0]}–{ci[1]})' if ci else ''} · threshold {ac.MIN_KAPPA}</p>
  <p class="meta">Someone must label each request whose label is blank for them:</p>
  <div class="row">
    <a href="/label?annotator=A"><button class="primary">Label as annotator A</button></a>
    <a href="/label?annotator=B"><button class="primary">Label as annotator B</button></a>
    <a href="/adjudicate?name=C"><button class="warn">Adjudicate disagreements</button></a>
    <a href="/contribute"><button>Contribute a request</button></a>
  </div>
</div>
<div class="card">
  <h2>Question</h2>
  <p>{html.escape(ac.LABEL_QUESTION)}</p>
  <p class="hint">Positive: a third party's address, phone, identity, health, family or
  finances. Negative: how a public body works, spends, decides or contracts; a public
  official's official duties; a company's records (that is 8(1)(d), a different label).</p>
</div>
<p><a href="/api/progress">JSON progress</a> · <span class="meta">store: {html.escape(str(store()))}</span></p>
"""
    return page("Citizen annotation", body)


@app.get("/label", response_class=HTMLResponse)
def label_page(annotator: str = "A", exclude: str = "") -> HTMLResponse:
    annotator = _annotators_or_default(annotator)
    if not annotator:
        return page("Annotator needed", "<p>Add <code>?annotator=A</code> to the URL.</p>")
    rows = ac.load(store())
    state = ac.progress(rows)
    skipped = {value for value in exclude.split(",") if value}
    row = next_unlabelled(rows, annotator, skipped)
    if row is None:
        return page(
            "All done",
            f"""<h1>Nothing left for {html.escape(annotator)}</h1>
<p class="lede">{state['resolved']} of {state['target']} rows resolved.</p>
<p><a href="/">Back to status</a></p>""",
        )
    done = state["labelled_by_annotator"].get(annotator, 0)
    body = f"""
<h1>Label as {html.escape(annotator)}</h1>
<p class="meta">{done} labelled by you · {state['resolved']} of {state['target']} rows resolved
· candidate {html.escape(row['id'])}</p>
<div class="card">
  <span class="badge">{html.escape(row.get('language', 'en'))}</span>
  <div class="request">{html.escape(row['text'])}</div>
  <p>{html.escape(ac.LABEL_QUESTION)}</p>
  <div class="row">
    <form method="post" action="/label/{quote(annotator)}/{quote(row['id'])}">
      <input type="hidden" name="value" value="yes">
      <button class="warn" type="submit">Yes — personal information</button>
    </form>
    <form method="post" action="/label/{quote(annotator)}/{quote(row['id'])}">
      <input type="hidden" name="value" value="no">
      <button class="primary" type="submit">No — about a public body</button>
    </form>
    <form method="post" action="/label/{quote(annotator)}/{quote(row['id'])}">
      <input type="hidden" name="value" value="skip">
      <button type="submit">Skip this one</button>
    </form>
  </div>
  <p class="hint">Two people label every row independently. Do not discuss a row before
  both of you have labelled it; that is what makes the agreement number meaningful.</p>
</div>
<p><a href="/">Status</a></p>
"""
    return page(f"Label {row['id']}", body)


@app.post("/label/{annotator}/{row_id}")
def submit_label(annotator: str, row_id: str, value: str = Form(...)) -> Any:
    if value == "skip":
        rows = ac.load(store())
        row = next((r for r in rows if r.get("id") == row_id), None)
        exclude = "" if row is None else row_id
        return RedirectResponse(f"/label?annotator={quote(annotator)}&exclude={quote(exclude)}", status_code=303)
    try:
        ac.locked_update(store(), lambda rows: ac.apply_label(rows, row_id, annotator, value))
    except (KeyError, ValueError) as exc:
        return page("Could not save", f"<p>{html.escape(str(exc))}</p><p><a href='/'>Back</a></p>")
    return RedirectResponse(f"/label?annotator={quote(annotator)}", status_code=303)


@app.get("/adjudicate", response_class=HTMLResponse)
def adjudicate_page(name: str = "C") -> HTMLResponse:
    rows = ac.load(store())
    disputes = ac.disputed(rows)
    if not disputes:
        return page("No disputes", "<h1>Nothing to adjudicate</h1><p>Every disagreement is resolved.</p><p><a href='/'>Status</a></p>")
    row = disputes[0]
    labels = row.get("labels") or {}
    names = list(labels)[:2]
    body = f"""
<h1>Adjudicate {html.escape(row['id'])}</h1>
<p class="meta">{len(disputes)} open disagreement(s). Decide which reading is right under the
codebook, and write a one-line reason for the record.</p>
<div class="card">
  <div class="request">{html.escape(row['text'])}</div>
  <p>{html.escape(ac.LABEL_QUESTION)}</p>
  <p class="meta">{' · '.join(f'{html.escape(n)}: {"yes" if labels[n] else "no"}' for n in names)}</p>
  <form method="post" action="/adjudicate/{quote(name)}/{quote(row['id'])}">
    <label class="inline">Reason (stored in the manifest review):</label>
    <input type="text" name="note" placeholder="e.g. asks for a named officer's home address">
    <div class="row">
      <button class="warn" type="submit" name="value" value="yes">It is personal</button>
      <button class="primary" type="submit" name="value" value="no">It is not personal</button>
    </div>
  </form>
</div>
"""
    return page("Adjudicate", body)


@app.post("/adjudicate/{name}/{row_id}")
def submit_adjudication(name: str, row_id: str, value: str = Form(...), note: str = Form("")) -> Any:
    state: dict[str, str] = {}

    def mutate(rows: list[dict[str, Any]]) -> None:
        ac.set_adjudication(rows, row_id, value, name)
        for row in rows:
            if row.get("id") == row_id:
                row["adjudication_note"] = note.strip()[:280]
                state["note"] = row["adjudication_note"]

    try:
        ac.locked_update(store(), mutate)
    except (KeyError, ValueError) as exc:
        return page("Could not save", f"<p>{html.escape(str(exc))}</p><p><a href='/adjudicate'>Back</a></p>")
    return RedirectResponse("/adjudicate", status_code=303)


@app.get("/contribute", response_class=HTMLResponse)
def contribute_page() -> HTMLResponse:
    return page(
        "Contribute a request",
        f"""<h1>Contribute a request</h1>
<p class="lede">Share a real RTI request you wrote. Only the wording is used, to measure
whether the screening works on ordinary citizen language rather than court orders.</p>
<div class="card">
<form method="post" action="/contribute">
  <label for="text">Your request</label>
  <textarea id="text" name="text" rows="6" required
    placeholder="How much was spent on road repairs in Ward 12, Pune during 2025-26?"></textarea>
  <p class="hint">Do not include names, addresses, phone numbers or ID numbers. Obvious
  identifiers are removed automatically, but you should not submit them.</p>
  <label for="source">Where it was filed or published (optional)</label>
  <input id="source" type="text" name="source" placeholder="e.g. my own filing, 2025">
  <label for="language">Language</label>
  <input id="language" type="text" name="language" value="en">
  <label class="inline"><input type="checkbox" name="consent" value="yes">
    <span>{html.escape(CONSENT_TEXT)}</span></label>
  <div class="row"><button class="primary" type="submit">Submit for annotation</button></div>
</form>
</div>
<p><a href="/">Status</a></p>""",
    )


@app.post("/contribute", response_class=HTMLResponse)
def submit_contribution(
    text: str = Form(...),
    source: str = Form("contributed request"),
    language: str = Form("en"),
    consent: str = Form(""),
) -> HTMLResponse:
    added: dict[str, Any] = {}

    def mutate(rows: list[dict[str, Any]]) -> None:
        row, reason = ac.add_candidate(
            rows, text=text, source=source or "contributed request",
            consent=consent.lower() == "yes", language=language or "en",
        )
        added["row"] = row
        added["reason"] = reason

    ac.locked_update(store(), mutate)
    if added.get("row") is None:
        return page(
            "Not added",
            f"""<h1>That was not added</h1><p>{html.escape(added.get('reason', 'unknown reason'))}</p>
<p><a href="/contribute">Try again</a></p>""",
        )
    row = added["row"]
    return page(
        "Thank you",
        f"""<h1>Thank you</h1><p class="lede">Saved as {html.escape(row['id'])} with
{row['redactions']} identifier(s) removed. It will be labelled by two people.</p>
<p><a href="/">Status</a></p>""",
    )


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Local citizen-annotation UI.")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.host != "127.0.0.1" and os.getenv("RTI_CITIZEN_ALLOW_REMOTE") != "1":
        raise SystemExit("Refusing to bind a non-loopback host without RTI_CITIZEN_ALLOW_REMOTE=1.")
    uvicorn.run(app, host=args.host, port=args.port)
