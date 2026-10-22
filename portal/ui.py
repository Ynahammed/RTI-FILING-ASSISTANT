"""Server-rendered pages for the mock RTI portal.

Kept dependency-free (no Jinja) so the portal is a single small service. Every
screen carries a visible MOCK label, as the plan requires.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi.responses import HTMLResponse

CSS = """
:root{--ink:#111827;--muted:#4b5563;--line:#d1d5db;--bg:#f8fafc;--card:#fff;
--brand:#0f4c81;--brand-ink:#fff;--warn:#fde68a;--warn-ink:#78350f;--ok:#065f46;
--bad:#991b1b;--amber:#b45309}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
a{color:var(--brand)}
.mockbar{background:var(--warn);color:var(--warn-ink);font-weight:700;letter-spacing:.04em;
padding:10px 18px;text-align:center;border-bottom:2px solid #f59e0b}
header.site{background:var(--brand);color:var(--brand-ink);padding:14px 18px}
header.site .wrap{max-width:980px;margin:0 auto;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header.site h1{font-size:20px;margin:0}
nav{display:flex;gap:14px;flex-wrap:wrap;font-size:14px}
nav a{color:#e0f2fe;text-decoration:none;border-bottom:1px solid transparent;padding-bottom:2px}
nav a:hover,nav a:focus{color:#fff;border-color:#fff}
main{max-width:980px;margin:26px auto;padding:0 18px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px;margin:0 0 18px}
h2{margin:0 0 6px;font-size:22px} h3{margin:18px 0 8px;font-size:17px}
.lede{color:var(--muted);margin:0 0 18px}
label{display:block;font-weight:600;margin:14px 0 6px}
input[type=text],input[type=number],select,textarea{width:100%;padding:10px 12px;border:1px solid var(--line);
border-radius:8px;font:inherit;background:#fff;color:var(--ink)}
textarea{min-height:120px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.row{grid-template-columns:1fr}}
button,.btn{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:11px 18px;
font:inherit;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary{background:#e5e7eb;color:#111827}
button.danger{background:var(--bad)}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{
outline:3px solid #2563eb;outline-offset:2px}
.alert{background:#fef2f2;color:var(--bad);border:1px solid #fecaca;border-radius:8px;padding:12px 14px;margin:12px 0}
.notice{background:#ecfdf5;color:var(--ok);border:1px solid #a7f3d0;border-radius:8px;padding:12px 14px;margin:12px 0}
.warnbox{background:#fffbeb;color:var(--amber);border:1px solid #fde68a;border-radius:8px;padding:12px 14px;margin:12px 0}
.meta{display:flex;gap:26px;flex-wrap:wrap;margin:14px 0;font-size:14px}
.meta div span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.regno{font:700 28px/1.2 ui-monospace,Consolas,monospace;word-break:break-all;margin:8px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700}
.pill-filed{background:#d1fae5;color:#065f46}.pill-pending{background:#fef3c7;color:#92400e}
.pill-bad{background:#fee2e2;color:#991b1b}
footer{max-width:980px;margin:0 auto;padding:20px 18px;color:var(--muted);font-size:13px}
code{background:#f3f4f6;padding:1px 5px;border-radius:4px}
form.inline{display:inline}
"""

BANNER = "MOCK RTI PORTAL &middot; SYNTHETIC APPLICANTS &middot; NO REAL GOVERNMENT SYSTEM"


def e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def layout(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} | Mock RTI Portal</title>
<style>{CSS}</style></head>
<body>
<div class="mockbar">{BANNER}</div>
<header class="site"><div class="wrap">
<h1>Mock RTI Portal</h1>
<nav>
<a href="/apply">File an application</a>
<a href="/grievance">Grievance portal</a>
<a href="/admin/clock">Demo console</a>
<a href="/">Status</a>
</nav>
</div></header>
<main>{body}</main>
<footer>This portal is a simulation built for the RTI Pilot experiment. Registration numbers,
deadlines and replies are generated locally. Nothing is sent to any government body.</footer>
</body></html>"""
    return HTMLResponse(content=page, status_code=status)


def home(now: str, applications: list[Any], fee: int) -> HTMLResponse:
    rows = "".join(
        f"<tr><td><code>{e(a['app_id'])}</code></td><td>{e(a['reg_no'] or '—')}</td>"
        f"<td>{e(a['state'])} / {e(a['department'])}</td>"
        f"<td><span class='pill {'pill-filed' if a['status'] in ('filed', 'transferred') else 'pill-pending'}'>"
        f"{e(a['status'])}</span></td>"
        f"<td>{e(a['deadline'] or '—')}</td></tr>"
        for a in applications
    ) or "<tr><td colspan='5'>No applications yet.</td></tr>"
    body = f"""
<div class="card">
<h2>Mock RTI portal</h2>
<p class="lede">A simulated public-information portal for the RTI Pilot agent. Mock date:
<strong>{e(now)}</strong>. Application fee: <strong>Rs {fee}</strong> (nil for BPL applicants).</p>
<p><a class="btn" href="/apply">Start a new application</a></p>
</div>
<div class="card"><h3>Recent applications</h3>
<table><thead><tr><th>Application</th><th>Registration</th><th>Authority</th><th>Status</th><th>Deadline</th></tr></thead>
<tbody>{rows}</tbody></table></div>
"""
    return layout("Home", body)


def apply_form(authorities: list[Any], values: dict[str, str] | None = None, error: str = "", fee: int = 10) -> HTMLResponse:
    values = values or {}
    states = sorted({a["state"] for a in authorities}, key=lambda s: (s == "Central", s))
    state_opts = "".join(
        f"<option value='{e(s)}' {'selected' if values.get('state') == s else ''}>{e(s)}</option>" for s in states
    )
    depts: dict[str, str] = {}
    for a in authorities:
        depts.setdefault(a["department"], a["department_label"])
    dept_opts = "".join(
        f"<option value='{e(k)}' {'selected' if values.get('department') == k else ''}>{e(v)}</option>"
        for k, v in depts.items()
    )
    auth_opts = "".join(
        f"<option value='{e(a['code'])}' {'selected' if values.get('authority_code') == a['code'] else ''}>"
        f"{e(a['name'])} ({e(a['code'])})</option>"
        for a in authorities
    )
    err = f"<div class='alert' role='alert'>{e(error)}</div>" if error else ""
    body = f"""
<div class="card">
<h2>Application for information</h2>
<p class="lede">Submitted under the Right to Information Act, 2005 — simulation only.</p>
{err}
<form method="post" action="/apply">
<div class="row">
<div><label for="applicant_name">Applicant name (synthetic)</label>
<input id="applicant_name" name="applicant_name" type="text" required value="{e(values.get('applicant_name', ''))}"></div>
<div><label for="applicant_address">Applicant address</label>
<input id="applicant_address" name="applicant_address" type="text" value="{e(values.get('applicant_address', ''))}"></div>
</div>
<div class="row">
<div><label for="state">State</label>
<select id="state" name="state" required>{state_opts}</select></div>
<div><label for="department">Department</label>
<select id="department" name="department" required>{dept_opts}</select></div>
</div>
<label for="authority_code">Public authority and PIO</label>
<select id="authority_code" name="authority_code" required>{auth_opts}</select>
<label for="information_sought">Information sought</label>
<textarea id="information_sought" name="information_sought" required>{e(values.get('information_sought', ''))}</textarea>
<div style="margin-top:14px">
<label style="display:inline;font-weight:400"><input type="checkbox" name="bpl" value="1" {'checked' if values.get('bpl') else ''}> Applicant holds a BPL card (fee nil)</label><br>
<label style="display:inline;font-weight:400"><input type="checkbox" name="urgent" value="1" {'checked' if values.get('urgent') else ''}> Life or liberty matter (48-hour track)</label>
</div>
<input type="hidden" name="idempotency_key" value="{e(values.get('idempotency_key', ''))}">
<p class="lede" style="margin-top:14px">Fee payable after the CAPTCHA and before acknowledgement: <strong>Rs {fee}</strong>.</p>
<button type="submit">Continue to CAPTCHA</button>
</form>
</div>
"""
    return layout("Apply", body)


def already_received(app: Any) -> HTMLResponse:
    link = f"/confirmation/{e(app['reg_no'])}" if app["reg_no"] else f"/captcha/{e(app['app_id'])}"
    body = f"""
<div class="card">
<h2>Application already received</h2>
<div class="notice">This idempotency key was already used for application <code>{e(app['app_id'])}</code>.
The portal did not create a duplicate.</div>
<p>Current status: <strong>{e(app['status'])}</strong></p>
<p><a class="btn" href="{link}">Open the existing application</a></p>
</div>"""
    return layout("Already received", body)


def captcha_page(app: Any, error: str = "", status_code: int = 200) -> HTMLResponse:
    alert = f"<div class='alert' role='alert'>{e(error)}</div>" if error else ""
    body = f"""
<div class="card">
<h2>Step 2 of 3 &middot; Human verification</h2>
<p class="lede">Application <code>{e(app['app_id'])}</code> is held at a mock CAPTCHA challenge.</p>
<div class="warnbox"><strong>Automation must stop here.</strong> The agent is not allowed to solve a
challenge. A demo operator completes it below, or uses the demo console. This mirrors the brief's
manual-handoff rule.</div>
{alert}
<form method="post" action="/admin/captcha/{e(app['app_id'])}/complete">
<label for="admin_token">Demo console token</label>
<input id="admin_token" name="admin_token" type="password" value=""
       placeholder="paste the console token" autocomplete="off">
<p class="lede">This field wants the portal's own console token. Signing in once at
<a href="/admin/clock">the demo console</a> also works, and its buttons then stop asking.</p>
<p class="lede">Mock challenge: <strong>select the traffic lights</strong> (not implemented &mdash; this is a placeholder, not a real challenge).</p>
<button type="submit">Complete challenge as demo operator</button>
<p class="lede">The agent itself stops here. In the dashboard, the approval card's
<strong>Demo operator: complete CAPTCHA</strong> button does the same handoff with the
token held server-side.</p>
</form>
</div>"""
    return layout("CAPTCHA", body, status=status_code)


def pay_page(app: Any, fee: int, error: str = "") -> HTMLResponse:
    err = f"<div class='alert' role='alert'>{e(error)}</div>" if error else ""
    body = f"""
<div class="card">
<h2>Step 3 of 3 &middot; Fee payment</h2>
<p class="lede">Application <code>{e(app['app_id'])}</code> &middot; {e(app['authority_code'])} &middot;
fee <strong>Rs {fee}</strong>.</p>
{err}
<div class="warnbox">A payment is accepted only with a valid approval token signed by the filing
system, matching this destination and this fee, plus an idempotency key. Attempts without both are
recorded as rejected.</div>
<form method="post" action="/pay/{e(app['app_id'])}">
<label for="approval_token">Approval token</label>
<input id="approval_token" name="approval_token" type="text" autocomplete="off" value="">
<label for="idempotency_key">Idempotency key</label>
<input id="idempotency_key" name="idempotency_key" type="text" value="">
<label for="amount">Amount (Rs)</label>
<input id="amount" name="amount" type="number" value="{fee}" min="0">
<button type="submit" style="margin-top:16px">Pay and file</button>
</form>
</div>"""
    return layout("Pay", body)


def confirmation_page(app: Any, authority_row: Any, transfer_row: Any, display_deadline: str) -> HTMLResponse:
    transfer = ""
    if app["transfer_to_code"]:
        transfer = f"""<div class="notice">Transferred under Section 6(3) to
<strong>{e(transfer_row['name'] if transfer_row else app['transfer_to_code'])}</strong>
on {e(app['transfer_at'])}. The five-day transfer clock applies.
<a href="/transfer/{e(app['app_id'])}">View transfer record</a>.</div>"""
    body = f"""
<div class="card">
<h2>Application acknowledged</h2>
<div class="notice">This acknowledgement is served from the mock portal's own database.</div>
<div class="meta">
<div><span>Registration number</span><strong class="regno">{e(app['reg_no'])}</strong></div>
<div><span>Response deadline</span><strong style="font-size:20px">{e(display_deadline)}</strong></div>
</div>
{transfer}
<h3>Application details</h3>
<table>
<tr><th>Applicant</th><td>{e(app['applicant_name'])}</td></tr>
<tr><th>Public authority</th><td>{e(authority_row['name'] if authority_row else app['authority_code'])}</td></tr>
<tr><th>PIO</th><td>{e(authority_row['pio'] if authority_row else '')}</td></tr>
<tr><th>Track</th><td>{'Life or liberty (48 hours)' if app['urgent'] else 'Standard (30 days)'}</td></tr>
<tr><th>Fee paid</th><td>Rs {fee_for(app)}</td></tr>
<tr><th>Filed (mock date)</th><td>{e(app['filed_at'])}</td></tr>
</table>
<h3>Information sought</h3>
<pre style="white-space:pre-wrap;font:inherit;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:14px">{e(app['information_sought'])}</pre>
</div>"""
    return layout("Acknowledgement", body)


def fee_for(app: Any) -> int:
    return 0 if app["bpl"] else 10


def transfer_page(app: Any, from_row: Any, to_row: Any) -> HTMLResponse:
    body = f"""
<div class="card">
<h2>Section 6(3) transfer record</h2>
<p class="lede">Application <code>{e(app['app_id'])}</code>, registration <strong>{e(app['reg_no'] or '—')}</strong>.</p>
<table>
<tr><th>Received by</th><td>{e(from_row['name'] if from_row else app['authority_code'])}<br>
<small>{e(from_row['code'] if from_row else app['authority_code'])} &middot; {e(from_row['pio'] if from_row else '')}</small></td></tr>
<tr><th>Transferred to</th><td>{e(to_row['name'] if to_row else app['transfer_to_code'])}<br>
<small>{e(to_row['code'] if to_row else (app['transfer_to_code'] or ''))} &middot; {e(to_row['pio'] if to_row else '')}</small></td></tr>
<tr><th>Transferred on</th><td>{e(app['transfer_at'])}</td></tr>
<tr><th>Rule</th><td>Section 6(3): transfer within five days to the authority holding the records.</td></tr>
</table>
<p class="lede">The mock portal now tracks the application at the transferee PIO. The agent's tracker
reads this page to follow the transfer.</p>
</div>"""
    return layout("Transfer", body)


def grievance_page() -> HTMLResponse:
    body = """
<div class="card">
<h2>Grievance portal (separate service)</h2>
<div class="warnbox">This is where requests for <strong>action</strong> belong. An RTI application asks
for records that already exist; it cannot order a repair, a transfer or disciplinary action.</div>
<p>Complaints about a service, a delay, or staff conduct are registered here and forwarded to the
department's grievance officer.</p>
<form method="post" action="/grievance">
<label for="g_name">Name</label><input id="g_name" name="name" type="text">
<label for="g_body">What should be fixed?</label><textarea id="g_body" name="body"></textarea>
<button type="submit" style="margin-top:14px">Register grievance</button>
</form>
<p class="lede">This mock does not process the complaint; it confirms the redirect target the agent
should hand to the citizen.</p>
</div>"""
    return layout("Grievance", body)


def grievance_received(name: str) -> HTMLResponse:
    body = f"""
<div class="card"><h2>Grievance registered (mock)</h2>
<div class="notice">No complaint number is issued in this simulation. The redirect itself is the
evidence: action requests leave the RTI pipeline here.</div>
<p>Complainant: {e(name or 'anonymous')}</p></div>"""
    return layout("Grievance received", body)


def admin_login(error: str = "") -> HTMLResponse:
    alert = f"<div class='alert' role='alert'>{e(error)}</div>" if error else ""
    body = f"""
<div class="card">
<h2>Demo console</h2><div class="warnbox"><strong>Operator-only surface.</strong> The demo console drives the mock clock,
completes the mock CAPTCHA and flips failure switches. It is not citizen-facing.</div>
{alert}
<form method="post" action="/admin/login">
<label for="admin_token">Demo operator token</label>
<input id="admin_token" name="admin_token" type="password" autocomplete="off" required>
<button type="submit">Open console</button>
</form>
</div>"""
    return layout("Demo console sign-in", body)


def clock_page(now: str, applications: list[Any], timeout_next: bool, skew: int) -> HTMLResponse:
    rows = "".join(
        f"""<tr><td><code>{e(a['app_id'])}</code><br><small>{e(a['reg_no'] or 'not filed')}</small></td>
<td>{e(a['status'])}<br><small>{e(a['deadline'] or '')}</small></td>
<td>
<form class="inline" method="post" action="/admin/captcha/{e(a['app_id'])}/complete">
<button class="secondary" type="submit">Complete CAPTCHA</button></form>
<form class="inline" method="post" action="/admin/reply/{e(a['app_id'])}">
<input type="hidden" name="evasive" value="1">
<button class="secondary" type="submit">Add evasive reply</button></form>
<form class="inline" method="post" action="/admin/confirmation/{e(a['app_id'])}/restore">
<button class="secondary" type="submit">Restore confirmation page</button></form>
</td></tr>"""
        for a in applications
    ) or "<tr><td colspan='3'>No applications yet.</td></tr>"
    body = f"""
<div class="card">
<h2>Demo console</h2>
<p class="lede">Operator-only controls. The agent never calls these; they simulate a human solving a
CAPTCHA, a portal reply, or a clock passing.</p>
<div class="notice">Mock date/time: <strong>{e(now)}</strong></div>
<form method="post" action="/admin/clock">
<label for="days">Advance the mock clock (days)</label>
<div class="row"><input id="days" name="days" type="number" value="31" min="1">
<div style="align-self:end"><button type="submit">Advance</button></div></div>
</form>
</div>
<div class="card">
<h3>Failure-mode switches</h3>
<p class="lede">These apply to the next filing only, then reset.</p>
<form method="post" action="/admin/simulate">
<label style="display:inline;font-weight:400"><input type="checkbox" name="timeout_next_filing" value="1" {'checked' if timeout_next else ''}>
Timeout after payment, before the confirmation page renders</label><br>
<label for="skew">Show a deadline that is wrong by (days)</label>
<input id="skew" name="skew_next_deadline_days" type="number" value="{skew}" min="0" max="10">
<button type="submit" style="margin-top:12px">Apply to next filing</button>
</form>
</div>
<div class="card"><h3>Applications</h3>
<table><thead><tr><th>Application</th><th>Status</th><th>Operator actions</th></tr></thead>
<tbody>{rows}</tbody></table></div>
"""
    return layout("Demo console", body)


def unavailable_page(message: str) -> HTMLResponse:
    body = f"""<div class="card"><h2>Page temporarily unavailable</h2>
<div class="alert">{e(message)}</div>
<p>The application may still exist. The verifier must record this as pending, not as success.</p></div>"""
    return layout("Unavailable", body, status=503)
