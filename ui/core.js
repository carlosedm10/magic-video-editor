/* CutRoom UI kernel — api helper, shared state, project list, stage pills,
   run-pipeline button + progress panel, tab dispatch, job polling.

   Vanilla JS, no build step, no modules: everything declared at the top
   level of a classic <script> is visible to scripts loaded after it in the
   same page, so this is the one place `$`, `api`, `esc`, `fmtT`, `state`,
   `refreshProject`, `watchJob`, `setTab` etc. are defined. Tab renderers
   register themselves onto window.TABS (see ui/tabs/*.js); panels expose
   themselves the same way under their own window.<Name>Panel (ui/panels/*.js). */

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
  return res.json();
};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtT = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

const STAGES = [
  ["ingest", "1 Ingest"], ["sync", "2 Sync"], ["transcribe", "3 Transcribe"],
  ["takes", "4 Takes"], ["order", "5 Order"], ["render", "6 Render"], ["reels", "7 Reels"],
];
// Friendly labels for the run-all progress panel (spec: Pipeline orchestration UX).
const STAGE_LABELS = {
  ingest: "Reading files", sync: "Syncing cameras", transcribe: "Transcribing",
  takes: "Analyzing takes", order: "Ordering the story", render: "Editing the video",
  reels: "Making shorts",
};

window.TABS = window.TABS || {};

let state = {
  pid: null, project: null, tab: "files", jobs: {}, watching: new Set(),
  runAllJob: null,
};

/* ---------- projects ---------- */

async function loadProjects() {
  const list = await api("/projects");
  $("#project-list").innerHTML = list.map((p) => `
    <div class="proj ${p.id === state.pid ? "active" : ""}" data-id="${p.id}">
      ${esc(p.name)}<small class="dim">${p.clips} clips</small>
    </div>`).join("");
  document.querySelectorAll(".proj").forEach((el) =>
    el.onclick = () => selectProject(el.dataset.id));
}

async function selectProject(pid) {
  state.pid = pid;
  state.runAllJob = null;
  await refreshProject();
  $("#empty-state").hidden = true;
  $("#project-view").hidden = false;
  loadProjects();
}

async function refreshProject() {
  if (!state.pid) return;
  state.project = await api(`/projects/${state.pid}`);
  $("#p-name").textContent = state.project.name;
  renderStageBar();
  renderRunAllPanel();
  renderTab();
}

/* ---------- stage bar + run pipeline ---------- */

function renderStageBar() {
  const stages = state.project.stages || {};
  $("#stage-bar").innerHTML = STAGES.map(([key, label]) => {
    const st = stages[key];
    const running = Object.values(state.jobs).some(
      (j) => j.status === "running" && j.name.startsWith(`${key}:`));
    const cls = running ? "running" : st ? st.status : "";
    const title = st?.detail || "";
    return `<button class="stage ${cls}" data-stage="${key}" title="${esc(title)}">${label}</button>`;
  }).join("");
  document.querySelectorAll(".stage").forEach((el) =>
    el.onclick = () => runStage(el.dataset.stage));
}

async function runStage(stage) {
  try {
    const { job } = await api(`/projects/${state.pid}/run/${stage}`, { method: "POST" });
    watchJob(job);
    setTab("jobs");
  } catch (e) { alert(e.message); }
}

async function runAll() {
  if (state.runAllJob && state.jobs[state.runAllJob]?.status === "running") return;
  try {
    const { job } = await api(`/projects/${state.pid}/run-all`, { method: "POST" });
    state.runAllJob = job;
    watchJob(job);
  } catch (e) { alert(e.message); }
}

function renderRunAllPanel() {
  const panel = $("#run-all-panel");
  if (!panel) return;
  const job = state.runAllJob ? state.jobs[state.runAllJob] : null;
  if (!job || job.status !== "running") {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const stages = job.stages || {};
  panel.hidden = false;
  panel.innerHTML = STAGES.map(([key]) => {
    const st = stages[key] || { status: "pending", progress: 0 };
    const pct = Math.round((st.progress || 0) * 100);
    return `<div class="run-all-row">
      <span class="run-all-label">${esc(STAGE_LABELS[key] || key)}</span>
      <div class="run-all-bar"><div class="run-all-fill ${st.status}" style="width:${pct}%"></div></div>
      <span class="run-all-pct">${st.status === "error" ? "!" : `${pct}%`}</span>
    </div>`;
  }).join("");
}

function watchJob(jid) {
  if (state.watching.has(jid)) return;
  state.watching.add(jid);
  const poll = async () => {
    const job = await api(`/jobs/${jid}`);
    state.jobs[jid] = job;
    if (state.tab === "jobs") renderTab();
    renderStageBar();
    renderRunAllPanel();
    if (job.status === "running") setTimeout(poll, 1200);
    else { state.watching.delete(jid); await refreshProject(); }
  };
  poll();
}

/* ---------- tabs ---------- */

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((el) =>
    el.classList.toggle("active", el.dataset.tab === tab));
  document.querySelectorAll(".tabpane").forEach((el) =>
    el.hidden = el.id !== `tab-${tab}`);
  renderTab();
}

function renderTab() {
  if (!state.project) return;
  const fn = window.TABS[state.tab];
  if (fn) fn();
}

/* ---------- boot ---------- */

async function boot() {
  document.querySelectorAll(".tab").forEach((el) => el.onclick = () => setTab(el.dataset.tab));
  $("#new-project").onclick = async () => {
    const name = prompt("Project name:");
    if (!name) return;
    const p = await api("/projects", { method: "POST", body: { name } });
    await selectProject(p.id);
  };
  const runAllBtn = $("#run-all-btn");
  if (runAllBtn) runAllBtn.onclick = runAll;
  const h = await api("/health");
  $("#health").innerHTML = `
    ffmpeg <span class="${h.ffmpeg ? "ok" : "bad"}">${h.ffmpeg ? "✓" : "missing"}</span> ·
    ollama <span class="${h.ollama ? "ok" : "bad"}">${h.ollama ? "✓" : "down"}</span><br>
    <span title="${esc(h.whisper)}">${esc(h.model)}</span>`;
  await loadProjects();
}
boot();
