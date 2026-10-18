"use strict";

const FIELD_LABELS = {
  business_name: "Business name",
  requested_effective_date: "Requested effective date",
  garaging_state: "Garaging state",
  operations_description: "Operations description",
  declared_vehicle_count: "Declared vehicle count",
  operating_radius_miles: "Operating radius (miles)",
  hazardous_materials: "Hazardous materials",
  loss_runs_present: "Loss runs present",
  schedule_vehicle_count: "Schedule vehicle count (derived from CSV)",
};

// All applicant data lives here and is wiped before every new packet.
let state = null;
let selectedFiles = [];
let runToken = 0; // ignores late responses from a previous packet

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
};
const badge = (status, label) => el("span", { class: `badge ${status}`, text: label || status });

function setStatus(kind, message, spinner = false) {
  const box = $("status");
  box.hidden = !kind;
  box.className = `status ${kind || ""}`;
  box.textContent = "";
  if (spinner) box.append(el("span", { class: "spinner" }));
  box.append(message || "");
}

async function api(path, options) {
  const resp = await fetch(path, options);
  let body = null;
  try { body = await resp.json(); } catch { /* non-JSON error page */ }
  if (!resp.ok) throw new Error((body && body.detail) || `Request failed (HTTP ${resp.status})`);
  return body;
}

/* ---------- intake ---------- */

function renderFileList() {
  const list = $("file-list");
  list.textContent = "";
  selectedFiles.forEach((f, i) => {
    const remove = el("button", { class: "small-btn", text: "remove" });
    remove.onclick = () => { selectedFiles.splice(i, 1); renderFileList(); };
    list.append(el("li", {}, `${f.name} (${Math.ceil(f.size / 1024)} KB) `, remove));
  });
}

function addFiles(fileList) {
  for (const f of fileList) {
    if (!selectedFiles.some((s) => s.name === f.name && s.size === f.size)) selectedFiles.push(f);
  }
  renderFileList();
}

function resetResults() {
  state = null;
  runToken += 1;
  $("results").hidden = true;
  for (const id of ["overall", "fields", "summary", "narrative-status", "extraction-log", "rerun-note", "copy-note"]) $(id).textContent = "";
  $("rules-table").querySelector("tbody").textContent = "";
  $("inventory-table").querySelector("tbody").textContent = "";
  $("email-subject").value = "";
  $("email-body").value = "";
  $("rerun-btn").disabled = true;
  $("email-to").value = "";
  $("send-confirm").hidden = true;
  $("send-note").textContent = "";
  updateSendButton();
}

function clearAll() {
  resetResults();
  selectedFiles = [];
  renderFileList();
  $("file-input").value = "";
  $("pasted-email").value = "";
  setStatus(null);
}

async function processPacket() {
  const pasted = $("pasted-email").value;
  if (!selectedFiles.length && !pasted.trim()) {
    setStatus("error", "Add the packet's files or paste the broker email first.");
    return;
  }
  resetResults(); // never show the previous applicant's data
  const token = runToken;
  const form = new FormData();
  selectedFiles.forEach((f) => form.append("files", f, f.name));
  form.append("pasted_email", pasted);

  const started = Date.now();
  const timer = setInterval(() => setStatus("processing",
    `Reading documents and extracting fields with the local model… ${Math.round((Date.now() - started) / 1000)}s`, true), 1000);
  setStatus("processing", "Reading documents and extracting fields with the local model…", true);
  $("process-btn").disabled = true;
  try {
    const packet = await api("/api/process", { method: "POST", body: form });
    if (token !== runToken) return;
    state = { packet, overrides: {}, evaluation: packet.evaluation, narrativeStale: false, sent: false };
    $("email-to").value = packet.broker_address || "";
    const failed = packet.extraction_log.filter((l) => !l.ok);
    const suspicious = packet.extraction_log.filter((l) => l.suspicious_lines && l.suspicious_lines.length);
    if (suspicious.length) {
      setStatus("warning", `Warning: ${suspicious.map((l) => `${l.source} contains text addressed to an AI ("${l.suspicious_lines[0]}")`).join("; ")}. It was treated as data; values taken from such lines are not used.`);
    } else if (failed.length) {
      setStatus("warning", `Processed with model errors: ${failed.map((l) => `${l.source}: ${l.error}`).join("; ")}. Deterministic results are still shown.`);
    } else {
      setStatus("success", `Packet processed in ${Math.round((Date.now() - started) / 1000)}s · ${packet.inventory.length} file(s) · model ${packet.model}`);
    }
    renderAll();
    generateNarrative();
  } catch (err) {
    if (token === runToken) setStatus("error", `Processing failed: ${err.message}`);
  } finally {
    clearInterval(timer);
    $("process-btn").disabled = false;
  }
}

/* ---------- rendering ---------- */

function renderAll() {
  $("results").hidden = false;
  renderOverall();
  renderRules();
  renderFields();
  renderInventory();
}

function renderOverall() {
  const { packet, evaluation, overrides } = state;
  const box = $("overall");
  box.textContent = "";
  const nOv = Object.keys(overrides).length;
  box.append(
    badge(evaluation.overall_status, evaluation.overall_status.replaceAll("_", " ")),
    el("div", {},
      el("div", { class: "field-name", text: packet.fields.business_name.display !== "—" ? packet.fields.business_name.display : "Applicant name not determined" }),
      el("div", { class: "muted small", text: `Packet ${packet.packet_id} · processed ${new Date(packet.processed_at).toLocaleString()} · ${nOv} human override(s)` }),
      el("div", { class: "muted small", text: "Readiness for human review only — not an approval, decline, quote or binding decision." })),
  );
  box.firstChild.classList.add("big");
}

function renderRules() {
  const tbody = $("rules-table").querySelector("tbody");
  tbody.textContent = "";
  for (const r of state.evaluation.rules) {
    const inputs = el("div");
    for (const i of r.inputs) {
      inputs.append(el("span", { class: `chip ${i.overridden ? "OVERRIDE" : ""}`,
        text: `${i.field}: ${i.state === "KNOWN" ? i.display : i.state}${i.overridden ? " (override)" : ""}` }));
    }
    tbody.append(el("tr", {},
      el("td", {}, el("strong", { text: r.id }), ` ${r.name}`),
      el("td", {}, badge(r.status)),
      el("td", { text: r.reason }),
      el("td", {}, inputs)));
  }
}

function evidenceList(field) {
  const ul = el("ul", { class: "evidence" });
  for (const e of field.evidence || []) {
    const li = el("li", { class: e.excerpt_verified === false ? "unverified" : "" },
      el("span", { class: "src", text: e.source_file }), ` · ${e.locator}`);
    if (e.status === "not_provided") li.append(" · explicitly not provided");
    if (e.status === "stated") li.append(` · value: ${e.display ?? e.raw_value}`);
    if (e.excerpt) li.append(el("br"), el("q", { text: e.excerpt }));
    if (e.observation) li.append(el("br"), el("span", { class: "muted", text: e.observation }));
    if (e.invalid_reason) li.append(el("br"), el("span", { class: "error-text", text: e.invalid_reason }));
    if (e.grounding_note) li.append(el("br"), el("span", { class: e.excerpt_verified ? "muted" : "error-text", text: e.grounding_note }));
    ul.append(li);
  }
  for (const s of field.email_statements || []) {
    ul.append(el("li", {}, el("span", { class: "src", text: s.source_file }), ` · ${s.locator} · statement (not an attachment)`,
      el("br"), el("q", { text: s.excerpt })));
  }
  if (!ul.children.length) {
    ul.append(el("li", { class: "muted", text: `No value found. Sources checked: ${(field.inspected_sources || []).join(", ") || "none"}` }));
  }
  return ul;
}

function renderFields() {
  const container = $("fields");
  container.textContent = "";
  const eff = state.evaluation.effective_fields;
  for (const [name, label] of Object.entries(FIELD_LABELS)) {
    const original = state.packet.fields[name];
    const f = eff[name];
    const head = el("div", { class: "field-head" },
      el("div", {}, el("span", { class: "field-name", text: label }), " ", el("span", { class: "field-key", text: name })),
      el("div", {}, f.overridden ? badge("OVERRIDE", "HUMAN OVERRIDE") : null, " ", badge(original.state)));
    const box = el("div", { class: "field" }, head,
      el("div", { class: "field-value", text: f.display }),
      el("div", { class: "muted small", text: original.reason || "" }));

    if (original.alternatives && original.alternatives.length) {
      const alts = el("div", { class: "alts" }, el("strong", { text: "Conflicting values (unresolved): " }));
      original.alternatives.forEach((a, i) => alts.append(i ? " vs " : "", el("strong", { text: a.display }), ` [${a.sources.join(", ")}]`));
      box.append(alts);
    }
    if (f.overridden) {
      box.append(el("div", { class: "override-box" },
        `Human override: ${f.override.display}${f.override.note ? ` — “${f.override.note}”` : ""} (${new Date(f.override.at).toLocaleTimeString()}). `,
        `Original extracted: ${original.display} [${original.state}]. Original evidence retained below.`));
    }
    box.append(evidenceList(original));

    const actions = el("div", { class: "actions" });
    const ovBtn = el("button", { class: "small-btn", text: f.overridden ? "Edit override" : "Override" });
    ovBtn.onclick = () => openOverrideForm(box, name, actions);
    actions.append(ovBtn);
    if (f.overridden || state.overrides[name]) {
      const rm = el("button", { class: "small-btn", text: "Remove override" });
      rm.onclick = () => { delete state.overrides[name]; markPending(); renderFields(); };
      actions.append(rm);
    }
    if (state.overrides[name] && !f.overridden) actions.append(el("span", { class: "muted small", text: `Pending override: ${state.overrides[name].value} — click “Rerun checks”` }));
    if (!state.overrides[name] && f.overridden) actions.append(el("span", { class: "muted small", text: "Override removal pending — click “Rerun checks”" }));
    box.append(actions);
    container.append(box);
  }
}

function openOverrideForm(box, name, actions) {
  const form = $("override-tpl").content.firstElementChild.cloneNode(true);
  const current = state.overrides[name];
  if (current) { form.querySelector(".ov-value").value = current.value; form.querySelector(".ov-note").value = current.note || ""; }
  form.querySelector(".ov-cancel").onclick = () => form.remove();
  form.querySelector(".ov-apply").onclick = () => {
    const value = form.querySelector(".ov-value").value.trim();
    if (!value) return;
    state.overrides[name] = { value, note: form.querySelector(".ov-note").value.trim(), at: new Date().toISOString() };
    markPending();
    renderFields();
  };
  actions.after(form);
  form.querySelector(".ov-value").focus();
}

function markPending() {
  $("rerun-btn").disabled = false;
  $("rerun-note").textContent = "Overrides changed — rerun checks to update results.";
}

async function rerunChecks() {
  const token = runToken;
  $("rerun-btn").disabled = true;
  try {
    const evaluation = await api("/api/evaluate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields: state.packet.fields, overrides: state.overrides }),
    });
    if (token !== runToken) return;
    state.evaluation = evaluation;
    state.narrativeStale = true;
    renderAll();
    $("rerun-note").textContent = `Checks rerun at ${new Date().toLocaleTimeString()} with ${Object.keys(state.overrides).length} human override(s).`;
    $("narrative-status").textContent = "Results changed since the summary was generated — click “Regenerate summary & draft”.";
  } catch (err) {
    $("rerun-btn").disabled = false;
    $("rerun-note").textContent = "";
    setStatus("error", `Rerun failed: ${err.message}`);
  }
}

function renderInventory() {
  const tbody = $("inventory-table").querySelector("tbody");
  tbody.textContent = "";
  for (const i of state.packet.inventory) {
    tbody.append(el("tr", {},
      el("td", { text: i.filename }), el("td", { text: i.kind }), el("td", { text: i.detected_type || "—" }),
      el("td", { text: i.type_basis || "" }), el("td", { text: i.readable ? "yes" : `no — ${i.error || ""}` })));
  }
  const log = $("extraction-log");
  log.textContent = "";
  for (const l of state.packet.extraction_log) {
    log.append(el("li", { text: l.ok ? `${l.source}: ok (${l.document_type}, ${l.elapsed_seconds ?? "?"}s)` : `${l.source}: FAILED — ${l.error}` }));
  }
}

/* ---------- narrative ---------- */

async function generateNarrative() {
  if (!state) return;
  const token = runToken;
  const btn = $("narrative-btn");
  btn.disabled = true;
  const status = $("narrative-status");
  status.textContent = "";
  status.append(el("span", { class: "spinner" }), "Generating summary and draft with the local model…");
  $("summary").textContent = "";
  try {
    const n = await api("/api/narrative", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields: state.packet.fields, overrides: appliedOverrides(), inventory: state.packet.inventory }),
    });
    if (token !== runToken) return;
    state.narrativeStale = false;
    if (n.ok) {
      status.textContent = `AI-generated by ${n.model} (${n.elapsed_seconds ?? "?"}s). Verify against the evidence above.`;
      $("summary").textContent = n.summary;
      $("email-subject").value = n.email_subject;
      $("email-body").value = n.email_body;
    } else {
      status.textContent = "";
      status.append(el("span", { class: "error-text", text: `Model call failed: ${n.error}. No AI summary is shown. ` }),
        "The draft below is a deterministic template (not AI-generated) built from the rule results.");
      $("email-subject").value = "Additional information needed for commercial auto submission";
      $("email-body").value = n.fallback_email_body;
    }
  } catch (err) {
    if (token === runToken) status.textContent = `Summary request failed: ${err.message}`;
  } finally {
    if (token === runToken) btn.disabled = false;
  }
}

// The narrative must reflect the checks as last rerun, not unapplied edits.
function appliedOverrides() {
  const out = {};
  for (const [name, f] of Object.entries(state.evaluation.effective_fields)) {
    if (f.overridden) out[name] = { value: String(f.override.value), note: f.override.note, at: f.override.at };
  }
  return out;
}

/* ---------- wiring ---------- */

async function checkModel() {
  const pill = $("model-pill");
  try {
    const h = await api("/api/health");
    pill.className = `pill ${h.reachable && h.model_available ? "ok" : "bad"}`;
    pill.textContent = !h.reachable ? `Ollama not reachable (${h.model})`
      : h.model_available ? `Model ready: ${h.model}` : `Model not pulled: ${h.model}`;
  } catch {
    pill.className = "pill bad";
    pill.textContent = "Model status unavailable";
  }
}

$("file-input").addEventListener("change", (e) => addFiles(e.target.files));
const dz = $("dropzone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("drag"); addFiles(e.dataTransfer.files); });
$("process-btn").onclick = processPacket;
$("clear-btn").onclick = clearAll;
$("rerun-btn").onclick = rerunChecks;
$("narrative-btn").onclick = generateNarrative;
$("copy-btn").onclick = async () => {
  try {
    await navigator.clipboard.writeText(`Subject: ${$("email-subject").value}\n\n${$("email-body").value}`);
    $("copy-note").textContent = "Copied to clipboard.";
  } catch { $("copy-note").textContent = "Copy failed — select the text manually."; }
};
checkModel();

/* ---------- optional send connectors (server decides whether sending is enabled) ---------- */

let mailStatus = null;
let sendProvider = null;

async function refreshMail() {
  try { mailStatus = await api("/api/mail/status"); } catch { mailStatus = null; }
  const enabled = !!(mailStatus && mailStatus.send_enabled);
  $("send-disabled").hidden = enabled;
  $("send-enabled").hidden = !enabled;
  if (!enabled) return;

  const box = $("providers");
  box.textContent = "";
  const connected = Object.entries(mailStatus.providers).filter(([, p]) => p.connected);
  if (!connected.some(([k]) => k === sendProvider)) sendProvider = connected.length ? connected[0][0] : null;
  for (const [key, p] of Object.entries(mailStatus.providers)) {
    const item = el("div", { class: "provider" });
    if (!p.configured) {
      item.append(el("span", { class: "muted", text: `${p.label}: not configured` }));
    } else if (p.connected) {
      const radio = el("input", { type: "radio", name: "send-provider", value: key });
      radio.checked = key === sendProvider;
      radio.onchange = () => { sendProvider = key; updateSendButton(); };
      const dc = el("button", { class: "small-btn", text: "Disconnect" });
      dc.onclick = async () => { await api(`/auth/${key}/disconnect`, { method: "POST" }); refreshMail(); };
      item.append(radio, `${p.label}: ${p.account}`, dc);
    } else {
      const btn = el("button", { class: "small-btn", text: `Connect ${p.label}` });
      btn.onclick = () => window.open(`/auth/${key}/start`, "mail-auth", "width=520,height=680");
      item.append(btn);
    }
    box.append(item);
  }
  $("sent-log").textContent = "";
  for (const r of mailStatus.sent_log || []) {
    $("sent-log").append(el("li", { text: `Sent via ${r.provider} from ${r.from} to ${r.to.join(", ")} — “${r.subject}” at ${r.sent_at}` }));
  }
  updateSendButton();
}

function updateSendButton() {
  const btn = $("send-btn");
  if (!btn) return;
  btn.disabled = !(state && sendProvider && !state.sent && mailStatus && mailStatus.send_enabled);
}

window.addEventListener("message", (e) => {
  if (e.origin !== window.location.origin || !e.data || e.data.type !== "mail-auth") return;
  $("send-note").textContent = e.data.message;
  refreshMail();
});

$("send-btn").onclick = () => {
  const to = $("email-to").value.trim();
  const subject = $("email-subject").value.trim();
  const body = $("email-body").value.trim();
  if (!to || !subject || !body) { $("send-note").textContent = "Recipient, subject and body are required."; return; }
  const p = mailStatus.providers[sendProvider];
  const box = $("send-confirm");
  box.textContent = "";
  box.append(
    el("strong", { text: "Confirm: this will send a real email." }),
    el("div", { text: `From: ${p.account} (${p.label})` }),
    el("div", { text: `To: ${to}` }),
    el("div", { text: `Subject: ${subject}` }),
    el("pre", { text: body }));
  if (state.narrativeStale) box.append(el("div", { class: "error-text", text: "Note: checks changed since this draft was generated. Regenerate or edit it before sending." }));
  const confirm = el("button", { class: "primary", text: "Confirm & send" });
  const cancel = el("button", { text: "Cancel" });
  cancel.onclick = () => { box.hidden = true; };
  confirm.onclick = () => sendDraft(to, subject, body, confirm, cancel);
  box.append(el("div", { class: "actions" }, confirm, cancel));
  box.hidden = false;
};

async function sendDraft(to, subject, body, confirmBtn, cancelBtn) {
  const token = runToken;
  confirmBtn.disabled = cancelBtn.disabled = true;
  $("send-note").textContent = "Sending…";
  try {
    const record = await api("/api/mail/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: sendProvider, to, subject, body, confirmed: true, packet_id: state.packet.packet_id }),
    });
    if (token !== runToken) return;
    state.sent = true;
    $("send-confirm").hidden = true;
    $("send-note").textContent = `Sent via ${record.provider} to ${record.to.join(", ")}.`;
    refreshMail();
  } catch (err) {
    $("send-note").textContent = `Send failed: ${err.message}`;
    confirmBtn.disabled = cancelBtn.disabled = false;
  }
}

refreshMail();
