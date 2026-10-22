/* RTI Pilot dashboard. Vanilla JS: no build step, no external requests. */

const $ = (sel) => document.querySelector(sel);
const state = {
  runId: null,
  runToken: null,
  operatorToken: sessionStorage.getItem("rti_operator_token") || "",
  detail: null,
  portalUrl: "http://127.0.0.1:8001",
  busy: false,
  poll: null,
};

const ACTIVE_STATUSES = new Set(["intake", "awaiting_input", "awaiting_approval", "awaiting_captcha", "filing"]);
// Mirrors RTI_REVIEW_SLA_HOURS; shown in the queue summary only.
const REVIEW_SLA_HOURS = 48;

const STATUS_LABELS = {
  intake: "Working",
  awaiting_input: "Needs your answer",
  awaiting_approval: "Approval needed",
  awaiting_captcha: "Human handoff",
  filing: "Filing",
  filed: "Filed",
  verified: "Filed and verified",
  filed_unverified: "Filed, verification pending",
  review_queue: "In review queue",
  redirected: "Redirected to grievance",
  declined: "Declined",
  failed: "Needs attention",
  tracking: "Tracking",
  appeal_ready: "First appeal ready",
  appeal_filed: "First appeal filed",
  second_appeal_ready: "Second appeal ready",
  complaint_ready: "Complaint ready",
  closed: "Closed",
};

const STATUS_TONE = {
  verified: "ok",
  tracking: "ok",
  filed: "info",
  awaiting_approval: "warn",
  awaiting_input: "warn",
  awaiting_captcha: "warn",
  review_queue: "warn",
  appeal_ready: "warn",
  appeal_filed: "info",
  second_appeal_ready: "warn",
  complaint_ready: "warn",
  filed_unverified: "warn",
  failed: "bad",
  declined: "bad",
  redirected: "info",
};

// Appeal-stage runs all approve through the same endpoint; only the wording and the
// draft kind differ. Keeping the mapping in one place stops a new stage from
// silently rendering without its approve button.
const APPEAL_STATUSES = new Set(["appeal_ready", "second_appeal_ready", "complaint_ready"]);
const APPEAL_KINDS = {
  appeal_ready: "appeal_first",
  second_appeal_ready: "appeal_second",
  complaint_ready: "complaint",
};
const APPEAL_COPY = {
  appeal_ready: { card: "Appeal awaiting approval", draft: "First appeal draft", result: "Section 19 first appeal" },
  second_appeal_ready: { card: "Second appeal awaiting approval", draft: "Second appeal draft", result: "Section 19(3) second appeal" },
  complaint_ready: { card: "Complaint awaiting approval", draft: "Complaint draft", result: "Section 18 complaint" },
};

function appealCopy(status) {
  return APPEAL_COPY[status] || { card: "Appeal awaiting approval", draft: "Appeal draft", result: "Appeal" };
}

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const pct = (value) => `${Math.round(Number(value || 0) * 100)}%`;

function fmtDate(iso, withTime = false) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

function toast(message, isError = false) {
  const el = document.createElement("div");
  el.className = `toast${isError ? " error" : ""}`;
  el.setAttribute("role", isError ? "alert" : "status");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isError ? 7000 : 4000);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  // The operator token opens the queue, tracker and test views. The run token is
  // the citizen's own credential and is only meaningful for the run it was issued for.
  if (state.operatorToken) headers["X-Operator-Token"] = state.operatorToken;
  if (state.runToken) headers["X-Run-Token"] = state.runToken;
  const resp = await fetch(path, { headers, ...options });
  let body = null;
  try { body = await resp.json(); } catch (e) { body = null; }
  if (!resp.ok) {
    if (resp.status === 401 || resp.status === 403) {
      throw new Error("Operator access is required for this view. Use the Operator access button.");
    }
    throw new Error((body && (body.detail || body.message)) || `${resp.status} ${resp.statusText}`);
  }
  return body;
}

/* ----------------------------------------------------------------- voice */

// The browser's own recognizer: no audio leaves through this app, and there is no
// dependency or key. Locale follows the language picker, so a Telugu request is
// transcribed as Telugu rather than translated.
const VOICE_LOCALES = {
  en: "en-IN", te: "te-IN", hi: "hi-IN", mr: "mr-IN", kn: "kn-IN", ta: "ta-IN",
};
const VOICE_ERRORS = {
  "not-allowed": "Microphone access was blocked. Allow it in the browser settings, or type instead.",
  "service-not-allowed": "The browser's speech service is not available. Type instead.",
  "audio-capture": "No microphone was found. Type instead.",
  "no-speech": "Nothing was heard. Try again, or type instead.",
  network: "The browser's speech service could not be reached. Type instead.",
  aborted: "",
};
const voice = { recognition: null, listening: false, supported: false, heard: false, lastError: "" };

function voiceNote(message) {
  const note = $("#voice-note");
  if (note) note.textContent = message;
}

function voiceLocale() {
  return VOICE_LOCALES[$("#language").value] || "en-IN";
}

function setListening(listening) {
  voice.listening = listening;
  const btn = $("#voice-btn");
  if (!btn) return;
  btn.setAttribute("aria-pressed", String(listening));
  btn.setAttribute("aria-label", listening ? "Stop voice input" : "Start voice input");
  btn.classList.toggle("listening", listening);
}

function stopVoice() {
  if (!voice.recognition) return;
  try { voice.recognition.stop(); } catch (e) { /* already stopped */ }
  setListening(false);
}

function setupVoice() {
  const btn = $("#voice-btn");
  if (!btn) return;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    btn.disabled = true;
    btn.setAttribute("aria-label", "Voice input is not available in this browser");
    voiceNote("Voice input is not available in this browser. Type instead.");
    return;
  }
  voice.supported = true;
  const recognition = new Recognition();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    voice.heard = false;
    voice.lastError = "";
    setListening(true);
    voiceNote("Listening… speak your request, then press Send.");
  };
  recognition.onresult = (event) => {
    const transcript = Array.from(event.results || [])
      .map((result) => (result[0] && result[0].transcript ? result[0].transcript : ""))
      .join(" ")
      .trim();
    if (!transcript) return;
    voice.heard = true;
    const input = $("#chat-input");
    const current = input.value.trim();
    input.value = current ? `${current} ${transcript}` : transcript;
    input.focus();
    voiceNote("Added to your message. Review it, then press Send.");
  };
  recognition.onerror = (event) => {
    voice.lastError = VOICE_ERRORS[event.error] ?? "Voice input stopped. Type instead.";
    if (voice.lastError) voiceNote(voice.lastError);
  };
  recognition.onend = () => {
    setListening(false);
    // Keep a result or an error on screen; clear the transient prompt otherwise.
    if (!voice.heard && !voice.lastError) voiceNote("");
  };

  voice.recognition = recognition;
  btn.addEventListener("click", () => {
    if (voice.listening) { stopVoice(); voiceNote(""); return; }
    try {
      recognition.lang = voiceLocale();
      voiceNote("Starting the microphone…");
      recognition.start();
    } catch (e) {
      setListening(false);
      voiceNote("The microphone could not start. Try again, or type instead.");
    }
  });
  $("#language").addEventListener("change", () => {
    if (voice.listening) stopVoice();
  });
}

/* ------------------------------------------------------------------ chat */

function renderMessages() {
  const box = $("#messages");
  const d = state.detail;
  if (!d) return;
  const pending = d.run.pending_question;
  box.innerHTML = d.messages.map((m) => {
    const who = m.role === "user" ? "You" : "RTI Pilot";
    const cls = m.role === "user" ? "user" : (pending && m.text === pending ? "assistant question" : "assistant");
    return `<div class="msg ${cls}"><span class="who">${who}</span>${esc(m.text)}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}

function setBusy(busy, label = "Working…") {
  state.busy = busy;
  const btn = $("#send-btn");
  btn.disabled = busy;
  btn.innerHTML = busy ? `<span class="spinner"></span> ${esc(label)}` : "Send";
}

async function sendMessage(text, language) {
  if (!text.trim() || state.busy) return;
  setBusy(true, "Reading your request…");
  try {
    let detail;
    const awaitingAnswer = state.detail && state.detail.run.status === "awaiting_input";
    if (awaitingAnswer) {
      detail = await api(`/api/runs/${state.runId}/answer`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
    } else {
      detail = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ text, language: language || null }),
      });
    }
    state.detail = detail;
    state.runId = detail.run.run_id;
    // Returned once, at creation. Kept in memory only: it is the client's claim on this run.
    if (detail.run_token) state.runToken = detail.run_token;
    $("#chat-input").value = "";
    renderAll();
    schedulePoll();
  } catch (err) {
    toast(err.message || "The request could not be processed.", true);
  } finally {
    setBusy(false);
  }
}

async function runAction(action) {
  if (!state.runId || state.busy) return;
  const labels = {
    approve: "Filing…",
    decline: "Closing…",
    captcha: "Paying…",
    track: "Checking the portal…",
    "approve-appeal": "Approving…",
  };
  setBusy(true, labels[action] || "Working…");
  try {
    let detail;
    if (action === "approve") {
      detail = await api(`/api/runs/${state.runId}/approve`, { method: "POST", body: "{}" });
    } else if (action === "decline") {
      detail = await api(`/api/runs/${state.runId}/decline`, {
        method: "POST",
        body: JSON.stringify({ reason: "Declined by the citizen in the dashboard." }),
      });
    } else if (action === "captcha") {
      detail = await api(`/api/runs/${state.runId}/captcha-complete`, { method: "POST", body: "{}" });
    } else if (action === "track") {
      detail = await api(`/api/runs/${state.runId}/track`, { method: "POST", body: "{}" });
    } else if (action === "approve-appeal") {
      detail = await api(`/api/runs/${state.runId}/approve-appeal`, { method: "POST", body: "{}" });
    }
    state.detail = detail;
    renderAll();
  } catch (err) {
    toast(err.message || "That action failed.", true);
  } finally {
    setBusy(false);
  }
}

/* -------------------------------------------------------------- timeline */

function routingFor(detail, code) {
  return detail.decisions.find(
    (x) => x.agent === "routing" && x.output && x.output.authority === code
  );
}

function renderTimeline() {
  const d = state.detail;
  if (!d) return;
  const status = d.run.status;
  const chip = $("#status-chip");
  chip.textContent = STATUS_LABELS[status] || status;
  chip.className = `pill ${STATUS_TONE[status] || "neutral"}`;

  const uncertain = d.run.missing_evidence && status === "awaiting_input";
  const steps = d.decisions.map((x) => {
    const low = x.label.toLowerCase().includes("uncertain") || x.agent === "routing" && x.label === "UNCERTAIN";
    const amber = low || (uncertain && x.agent === "routing");
    const routingEvidence = x.agent === "routing" && x.output
      ? d.evidence.find((e) => e.source_id === x.output.source_id)
      : null;
    const output = x.output || {};
    let dest = "";
    if (x.agent === "routing" && output.authority_name) {
      dest = `<div class="dest"><strong>${esc(output.authority_name)}</strong>
        ${esc(output.pio || "")}<br>
        <span class="hint">Registry ${esc(output.source_id || "")} · ${esc(output.level || "")}</span></div>`;
    }
    const meta = [];
    if (x.rule_ref) meta.push(`<span>Rule <b>${esc(x.rule_ref)}</b></span>`);
    if (output.source_id) meta.push(`<span>Source <b>${esc(output.source_id)}</b></span>`);
    if (routingEvidence) meta.push(`<span>Retrieved <b>${esc(fmtDate(routingEvidence.retrieved_at, true))}</b></span>`);
    if (x.agent === "verifier" && output.observed_reg_no) {
      meta.push(`<span>Observed <b>${esc(output.observed_reg_no)}</b></span>`);
    }
    return `<article class="step agent-${esc(x.agent)} ${amber ? "amber" : ""}">
      <div class="step-head">
        <span class="agent">${esc(x.agent.replace(/_/g, " "))}</span>
        <span class="conf ${x.confidence < 0.7 ? "low" : ""}">${pct(x.confidence)}</span>
      </div>
      <div class="step-label">${esc(x.label)}</div>
      <p class="step-reason">${esc(x.reason)}</p>
      ${dest}
      ${meta.length ? `<div class="step-meta">${meta.join("")}</div>` : ""}
    </article>`;
  });
  $("#timeline").innerHTML = steps.join("") || `<p class="empty-state small">No steps yet.</p>`;

  const drawer = $("#evidence-drawer");
  drawer.hidden = d.evidence.length === 0;
  $("#evidence-summary").textContent = `Evidence ledger (${d.evidence.length})`;
  $("#evidence-list").innerHTML = d.evidence.map((e) => `
    <div class="evidence-item">
      <code>${esc(e.source_id)}</code> · ${esc(e.kind)} · ${esc(e.source)}
      ${e.source_url ? ` · <a href="${esc(e.source_url)}" target="_blank" rel="noopener">source</a>` : ""}
      <br>${esc(e.extracted_value)}
      <br><span class="hint">retrieved ${esc(fmtDate(e.retrieved_at, true))}</span>
    </div>`).join("");
}

/* ----------------------------------------------------------------- cards */

function latestDraft(detail, kind) {
  return (detail.drafts || []).filter((d) => d.kind === kind).slice(-1)[0];
}

function renderApproval() {
  const d = state.detail;
  const box = $("#approval-card");
  if (!d) { box.innerHTML = ""; return; }
  const approval = d.approval;
  const status = d.run.status;
  if (!approval) { box.innerHTML = ""; return; }

  const appealKind = APPEAL_KINDS[status];
  const appeal = appealKind ? appealCopy(status) : null;
  const draft = appealKind ? latestDraft(d, appealKind) : latestDraft(d, "application");
  const routing = routingFor(d, approval.destination);
  const destination = routing && routing.output
    ? `${routing.output.authority_name}<br><span class="hint">${esc(routing.output.pio || "")} · ${esc(approval.destination)}</span>`
    : esc(approval.destination);

  if (approval.status === "pending") {
    const actions = status === "awaiting_approval"
      ? `<div class="card-actions">
           <button class="primary" data-action="approve" type="button">Approve and file</button>
           <button class="danger" data-action="decline" type="button">Decline</button>
         </div>`
      : "";
    box.innerHTML = `<section class="card approval">
      <h3>${appeal ? appeal.card : "Approval card"}</h3>
      <p class="hint">Nothing is submitted or paid until you approve this exact draft, destination and fee.</p>
      <h4>Destination</h4>
      <p>${destination}</p>
      <h4>${appeal ? appeal.draft : "Draft application"}</h4>
      <div class="draft">${esc(draft ? draft.text : "Draft unavailable")}</div>
      ${draft && draft.rewrite_notes.length ? `<h4>What changed and why</h4>
        <ul class="notes">${draft.rewrite_notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      <div class="fee-row"><span>Application fee</span><span class="fee">₹${esc(approval.fee_inr)}</span></div>
      ${actions}
    </section>`;
    return;
  }

  if (approval.status === "approved") {
    box.innerHTML = `<section class="card approval">
      <h3>Approved</h3>
      <p><span class="pill ok">Approved ${esc(fmtDate(approval.approved_at, true))}</span></p>
      <p class="hint">A signed approval token is stored with the run. The portal accepts payment only with that token and an idempotency key.</p>
      ${status === "awaiting_captcha" ? `
        <div class="fee-row" style="display:block">
          <strong>Human verification required</strong>
          <p class="hint" style="margin:6px 0 10px">The portal is holding the application at a mock CAPTCHA. The agent does not solve challenges. In the demo, the operator completes it here.</p>
          <button class="primary" data-action="captcha" type="button">Demo operator: complete CAPTCHA</button>
        </div>` : ""}
    </section>`;
    return;
  }

  box.innerHTML = `<section class="card">
    <h3>Approval declined</h3>
    <p>Nothing was filed and no fee was paid.</p>
    ${approval.declined_reason ? `<p class="hint">Reason: ${esc(approval.declined_reason)}</p>` : ""}
  </section>`;
}

function renderResult() {
  const d = state.detail;
  const box = $("#result-card");
  if (!d) { box.innerHTML = ""; return; }
  const run = d.run;
  const verification = d.verification;
  const tracker = d.tracker;
  const appealKind = APPEAL_KINDS[run.status];
  const appeal = appealKind ? latestDraft(d, appealKind) : latestDraft(d, "appeal_first");
  const appealLabel = appealKind ? appealCopy(run.status).result : "Section 19 first appeal";
  if (!run.reg_no && !verification && !tracker && !appeal) { box.innerHTML = ""; return; }

  const tone = verification && verification.verification_status === "verified" ? "result"
    : verification ? "result pending" : "result pending";
  const deadline = (verification && verification.observed_deadline) || (tracker && tracker.due_date);
  const matchBadge = verification
    ? (verification.verification_status === "verified"
        ? `<span class="pill ok">Verifier: matches intent</span>`
        : verification.verification_status === "pending"
          ? `<span class="pill warn">Verifier: pending</span>`
          : `<span class="pill bad">Verifier: mismatch</span>`)
    : `<span class="pill warn">Not verified yet</span>`;

  const comparison = verification ? `
    <table class="kv">
      <tr><th>Intended registration</th><td>${esc(verification.expected_reg_no || "—")}</td></tr>
      <tr><th>Observed on portal</th><td>${esc(verification.observed_reg_no || "unreadable")}</td></tr>
      <tr><th>Intended deadline</th><td>${esc(fmtDate(verification.expected_deadline))}</td></tr>
      <tr><th>Observed deadline</th><td>${esc(fmtDate(verification.observed_deadline))}</td></tr>
    </table>
    ${verification.note ? `<p class="hint" style="margin-top:8px">${esc(verification.note)}</p>` : ""}` : "";

  const trackerBlock = tracker ? `
    <h4>Tracker</h4>
    <table class="kv">
      <tr><th>Reply state</th><td>${esc((tracker.reply_state || "").replace(/_/g, " "))}</td></tr>
      <tr><th>Due date</th><td>${esc(fmtDate(tracker.due_date))}</td></tr>
      ${tracker.transferred_to ? `<tr><th>Transferred to</th><td>${esc(tracker.transferred_to)} (Section 6(3))</td></tr>` : ""}
    </table>` : "";

  box.innerHTML = `<section class="card ${tone}">
    <h3>${run.reg_no ? "Filed result" : "Result"}</h3>
    ${run.reg_no ? `<div class="regno">${esc(run.reg_no)}</div>` : ""}
    ${deadline ? `<p>Response due <span class="big-date">${esc(fmtDate(deadline))}</span></p>` : ""}
    <p style="margin-top:8px">${matchBadge}</p>
    ${comparison}
    ${trackerBlock}
    ${appeal ? `<h4>${esc(appealLabel)}</h4>
      <div class="draft" style="max-height:150px">${esc(appeal.text)}</div>
      ${APPEAL_STATUSES.has(run.status) ? `<div class="card-actions"><button class="primary" data-action="approve-appeal" type="button">Approve ${run.status === "complaint_ready" ? "complaint" : "appeal"}</button></div>` : ""}` : ""}
    ${run.portal_app_id && ["verified", "filed_unverified", "tracking", "appeal_filed", "appeal_ready", "second_appeal_ready", "complaint_ready"].includes(run.status)
      ? `<div class="card-actions"><button class="ghost" data-action="track" type="button">Re-check the portal</button></div>` : ""}
  </section>`;
}

function renderMeta() {
  const d = state.detail;
  const box = $("#meta-card");
  if (!d) {
    box.innerHTML = `<section class="card">
      <h3>How it works</h3>
      <ol class="notes" style="font-size:13px">
        <li>Intake extracts the request and asks only for what is missing.</li>
        <li>Triage separates RTI from grievance and screens Section 8 exemptions.</li>
        <li>Routing picks the public authority and cites the registry entry.</li>
        <li>Drafting rewrites opinion questions into record requests.</li>
        <li>You approve the destination, draft and fee.</li>
        <li>An independent verifier re-opens the portal and checks the result.</li>
      </ol>
      <p class="hint">Mock portal. No real filing, payment or personal data.</p>
    </section>`;
    return;
  }
  const fields = d.fields || {};
  const rows = [
    ["Run", d.run.run_id],
    ["Language", { en: "English", te: "Telugu", hi: "Hindi", mr: "Marathi", kn: "Kannada", ta: "Tamil" }[d.run.language] || d.run.language],
    ["State", fields.state ? fields.state.value : "—"],
    ["Department", fields.department ? fields.department.value.replace(/_/g, " ") : "—"],
    ["Fee", fields.fee_inr ? `₹${fields.fee_inr.value}` : "—"],
    ["Urgency", fields.urgency ? fields.urgency.value : "—"],
    ["BPL", fields.bpl && fields.bpl.value === "true" ? "Yes" : "No"],
  ];
  box.innerHTML = `<section class="card">
    <h3>Run facts</h3>
    <table class="kv">${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table>
  </section>`;
}

function renderAll() {
  renderMessages();
  renderTimeline();
  renderApproval();
  renderResult();
  renderMeta();
}

/* --------------------------------------------------------------- polling */

function schedulePoll() {
  clearInterval(state.poll);
  const status = state.detail && state.detail.run.status;
  if (!ACTIVE_STATUSES.has(status)) return;
  state.poll = setInterval(async () => {
    if (!state.runId || state.busy) return;
    try {
      const detail = await api(`/api/runs/${state.runId}`);
      if (detail.run.status !== state.detail.run.status || detail.messages.length !== state.detail.messages.length) {
        state.detail = detail;
        renderAll();
      }
      if (!ACTIVE_STATUSES.has(detail.run.status)) clearInterval(state.poll);
    } catch (e) { /* keep the last good view */ }
  }, 4000);
}

/* ---------------------------------------------------------------- views */

async function loadReview() {
  const list = $("#review-list");
  const summary = $("#review-summary");
  list.innerHTML = `<p class="empty-state">Loading the queue…</p>`;
  try {
    const { reviews: rows, counts } = await api("/api/reviews?status=open");
    const count = $("#review-count");
    count.textContent = counts.open;
    count.hidden = counts.open === 0;
    summary.textContent = counts.open
      ? `${counts.open} open · ${counts.unassigned} unclaimed · ${counts.overdue} past the ${REVIEW_SLA_HOURS}h target`
      : "Nothing is waiting for review.";
    list.innerHTML = rows.length ? rows.map((r) => `
      <article class="queue-card"${r.overdue ? ' style="border-left-color:var(--bad)"' : ""}>
        <div class="meta">
          <span class="pill ${r.overdue ? "bad" : "warn"}">${esc(r.source === "rule" ? "8(1)(j) rule" : "classifier")}</span>
          <span>${esc(r.exemption ? r.exemption.rule_ref : "")}</span>
          <span>${esc(fmtDate(r.created_at, true))}</span>
          <span class="hint">${r.overdue ? "Overdue" : "Due"} ${esc(fmtDate(r.due_at, true))}</span>
        </div>
        <p class="input">${esc(r.raw_input)}</p>
        <p class="hint">${esc(r.reason)}</p>
        <p class="hint">${r.assigned_to
          ? `Claimed by <strong>${esc(r.assigned_to)}</strong> ${esc(fmtDate(r.assigned_at, true))}`
          : "Unclaimed"}</p>
        <div class="card-actions">
          ${r.assigned_to ? "" : `<button class="ghost" data-claim-review="${esc(r.run_id)}" type="button">Claim</button>`}
          <button class="ghost" data-decide-review="${esc(r.run_id)}" data-decision="release" type="button">Release to filing</button>
          <button class="ghost" data-decide-review="${esc(r.run_id)}" data-decision="uphold" type="button">Uphold exemption</button>
          <button class="ghost" data-open-run="${esc(r.run_id)}" type="button">Open reasoning trail</button>
        </div>
      </article>`).join("")
      : `<p class="empty-state">Nothing is waiting for review.</p>`;
  } catch (err) {
    summary.textContent = "";
    list.innerHTML = `<p class="empty-state">Could not load the queue: ${esc(err.message)}</p>`;
  }
}

async function loadTracker() {
  const list = $("#tracker-list");
  list.innerHTML = `<p class="empty-state">Reading the portal clock…</p>`;
  try {
    const [rows, clock] = await Promise.all([api("/api/tracker"), api("/api/portal-clock")]);
    $("#portal-clock").textContent = clock.mock_now ? `Mock clock ${fmtDate(clock.mock_now, true)}` : "Mock clock unavailable";
    list.innerHTML = rows.length ? rows.map((r) => `
      <article class="queue-card" style="border-left-color:${r.reply_state === "no_reply" ? "var(--amber-line)" : "var(--accent)"}">
        <div class="track-row">
          <div>
            <strong>${esc(r.reg_no || "not filed")}</strong>
            <p class="hint">${esc(r.raw_input)}</p>
          </div>
          <div>
            <span class="pill ${r.reply_state === "no_reply" ? "warn" : "info"}">${esc((r.reply_state || "").replace(/_/g, " "))}</span>
            <p class="hint">Due ${esc(fmtDate(r.due_date))}</p>
          </div>
          <div>
            <p class="hint">${r.transferred_to ? `Transferred to ${esc(r.transferred_to)}` : "No transfer"}</p>
            <p class="hint">${r.appeal_draft_id ? "First appeal drafted" : "No appeal yet"}</p>
          </div>
          <div class="card-actions" style="margin:0">
            <button class="ghost" data-track-run="${esc(r.run_id)}" type="button">Check now</button>
            <button class="ghost" data-open-run="${esc(r.run_id)}" type="button">Open</button>
          </div>
        </div>
        ${(r.timeline || []).length ? `<ol class="mini-timeline">${r.timeline.map((e) => `
          <li class="tl-${esc(e.kind)}"><span class="tl-label">${esc(e.label)}</span>
          <span class="tl-date">${esc(fmtDate(e.at, true))}</span></li>`).join("")}</ol>` : ""}
      </article>`).join("")
      : `<p class="empty-state">No filed requests are being tracked yet.</p>`;
  } catch (err) {
    list.innerHTML = `<p class="empty-state">Could not load the tracker: ${esc(err.message)}</p>`;
  }
}

async function loadTests() {
  const list = $("#tests-list");
  list.innerHTML = `<p class="empty-state">Loading results…</p>`;
  try {
    const rows = await api("/api/tests");
    const passed = rows.filter((r) => r.passed).length;
    $("#tests-summary").innerHTML = rows.length
      ? `<span class="pill ok">${passed} passed</span>
         <span class="pill ${passed === rows.length ? "ok" : "bad"}">${rows.length - passed} failed</span>
         <span class="pill neutral">${rows.length} cases</span>`
      : "";
    list.innerHTML = rows.length ? rows.map((r) => `
      <article class="test-card ${r.passed ? "pass" : "fail"}">
        <div class="test-head" data-toggle-test="${esc(r.test_id)}">
          <span class="id">${esc(r.test_id)}</span>
          <span class="name">${esc(r.name)}</span>
          <span class="pill ${r.passed ? "ok" : "bad"}">${r.passed ? "PASS" : "FAIL"}</span>
        </div>
        <div class="test-body" id="test-body-${esc(r.test_id)}" hidden>
          <div><div class="label">Input</div><div class="value">${esc(r.input)}</div></div>
          <div><div class="label">Expected</div><div class="value">${esc(r.expected)}</div></div>
          <div><div class="label">Actual</div><div class="value actual">${esc(r.actual)}</div></div>
          ${r.notes ? `<div><div class="label">Notes</div><div class="value">${esc(r.notes)}</div></div>` : ""}
          <div class="card-actions" style="margin-top:4px">
            ${r.run_id ? `<button class="ghost" data-open-run="${esc(r.run_id)}" type="button">Open run</button>` : ""}
          </div>
        </div>
      </article>`).join("")
      : `<p class="empty-state">No results stored yet. Run <code>python -m scripts.run_eval</code> with both services up.</p>`;
  } catch (err) {
    list.innerHTML = `<p class="empty-state">Could not load results: ${esc(err.message)}</p>`;
  }
}

function switchView(view) {
  document.querySelectorAll(".view").forEach((el) => el.classList.add("hidden"));
  const target = document.getElementById(`view-${view}`);
  if (target) target.classList.remove("hidden");
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  if (view === "review") loadReview();
  if (view === "tracker") loadTracker();
  if (view === "tests") loadTests();
}

async function openRun(runId) {
  try {
    const detail = await api(`/api/runs/${runId}`);
    state.runId = runId;
    state.runToken = null;
    state.detail = detail;
    renderAll();
    schedulePoll();
    switchView("assistant");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------------------------------------------ init */

function bind() {
  // Tabs follow the ARIA tabs pattern: one tab stop for the group, arrows move
  // between tabs, Home and End jump to either end.
  const tabs = Array.from(document.querySelectorAll(".tab"));
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1, Home: 0, End: 0 }[event.key];
      if (step === undefined) return;
      event.preventDefault();
      let next = index + step;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      next = (next + tabs.length) % tabs.length;
      tabs[next].focus();
      switchView(tabs[next].dataset.view);
    });
  });

  $("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    stopVoice();
    const text = $("#chat-input").value.trim();
    sendMessage(text, $("#language").value);
  });

  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });

  $("#operator-access").addEventListener("click", () => {
    const current = state.operatorToken ? "connected" : "not set";
    const token = window.prompt(
      `Operator token (${current}). The review queue, tracker and test results need it.`,
      ""
    );
    if (token === null) return;
    state.operatorToken = token.trim();
    if (state.operatorToken) sessionStorage.setItem("rti_operator_token", state.operatorToken);
    else sessionStorage.removeItem("rti_operator_token");
    toast(state.operatorToken ? "Operator access saved for this tab." : "Operator access cleared.");
    loadReview();
  });

  $("#new-run").addEventListener("click", () => {
    state.runId = null;
    state.runToken = null;
    state.detail = null;
    clearInterval(state.poll);
    $("#messages").innerHTML = `<div class="empty-state">
      <strong>Tell me what you need to know.</strong>
      <p>For example: “How much did the Public Works Department spend on repairing the road in Ward 12, Pune, during 2025-26?”</p>
      <p class="hint">English, తెలుగు, हिन्दी, मराठी, ಕನ್ನಡ and தமிழ் are understood. Nothing is filed without your approval.</p>
    </div>`;
    renderTimeline();
    $("#timeline").innerHTML = `<p class="empty-state small">Each agent step appears here with its decision, confidence, reason, rule and source.</p>`;
    $("#approval-card").innerHTML = "";
    $("#result-card").innerHTML = "";
    renderMeta();
    $("#status-chip").textContent = "No run yet";
    $("#status-chip").className = "pill neutral";
    $("#chat-input").focus();
  });

  document.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-action]");
    if (action) { runAction(action.dataset.action); return; }

    const open = event.target.closest("[data-open-run]");
    if (open) { openRun(open.dataset.openRun); return; }

    const track = event.target.closest("[data-track-run]");
    if (track) {
      try {
        await api(`/api/runs/${track.dataset.trackRun}/track`, { method: "POST", body: "{}" });
        toast("Tracker refreshed from the portal.");
        loadTracker();
      } catch (err) { toast(err.message, true); }
      return;
    }

    const claim = event.target.closest("[data-claim-review]");
    if (claim) {
      try {
        await api(`/api/reviews/${claim.dataset.claimReview}/claim`, { method: "POST", body: "{}" });
        toast("Review claimed. It is yours until you decide it.");
        loadReview();
      } catch (err) { toast(err.message, true); }
      return;
    }

    const decide = event.target.closest("[data-decide-review]");
    if (decide) {
      const releasing = decide.dataset.decision === "release";
      const note = window.prompt(
        releasing
          ? "Why is this safe to file? The reason is stored with your name."
          : "Short reason for upholding the exemption (optional).",
        ""
      );
      if (note === null) return;
      try {
        await api(`/api/reviews/${decide.dataset.decideReview}/decide`, {
          method: "POST",
          body: JSON.stringify({ decision: decide.dataset.decision, note }),
        });
        toast(releasing ? "Released to filing." : "Exemption upheld; the run is closed.");
        loadReview();
        if (state.runId === decide.dataset.decideReview) {
          state.detail = await api(`/api/runs/${state.runId}`);
          renderAll();
          schedulePoll();
        }
      } catch (err) { toast(err.message, true); }
      return;
    }

    const toggle = event.target.closest("[data-toggle-test]");
    if (toggle) {
      const body = document.getElementById(`test-body-${toggle.dataset.toggleTest}`);
      if (body) body.hidden = !body.hidden;
      return;
    }

    const refresh = event.target.closest("[data-refresh]");
    if (refresh) {
      if (refresh.dataset.refresh === "review") loadReview();
      if (refresh.dataset.refresh === "tracker") loadTracker();
      if (refresh.dataset.refresh === "tests") loadTests();
    }
  });
}

async function init() {
  bind();
  setupVoice();
  try {
    const cfg = await api("/api/config");
    state.portalUrl = (cfg.portal_url || state.portalUrl).replace(/\/$/, "");
  } catch (e) { /* keep the default */ }
  $("#portal-link").href = state.portalUrl;
  $("#clock-link").href = `${state.portalUrl}/admin/clock`;
  loadReview();
  renderMeta();
  $("#chat-input").focus();
}

init();
