/* Magic Video Editor UI kernel — api helper, shared state, project switcher,
   stage pills, run-pipeline button + progress panel (persists across every
   view while a job runs), drawer dispatch (Takes/Reels/Settings/Activity),
   job polling.

   Vanilla JS, no build step, no modules: everything declared at the top
   level of a classic <script> is visible to scripts loaded after it in the
   same page, so this is the one place `$`, `api`, `esc`, `fmtT`, `state`,
   `refreshProject`, `watchJob`, `setTab` etc. are defined. Secondary-view
   renderers register themselves onto window.TABS (see ui/tabs/*.js); the
   main editor surface (media bin / player / timeline / inspector) lives
   under ui/editor/*.js and registers onto window.EditorUI, whose
   onProjectSelected/onProjectRefreshed hooks are called from here. */

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    // detail may be a plain string OR a {message, job} object (e.g. the
    // 409-busy responses from run/{stage} and run-all) -- keep both the
    // human message and the raw detail so callers can branch on it.
    const msg = typeof body.detail === "string" ? body.detail
      : body.detail?.message || res.statusText;
    const err = new Error(msg);
    err.status = res.status;
    err.detail = body.detail;
    throw err;
  }
  return res.json();
};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtT = (t) => {
  t = Math.max(0, t || 0);
  return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
};

const STAGES = [
  ["ingest", "1 Ingest"], ["sync", "2 Sync"], ["transcribe", "3 Transcribe"],
  ["takes", "4 Takes"], ["order", "5 Order"], ["review", "6 Review"],
  ["render", "7 Render"], ["reels", "8 Reels"],
];
// Friendly labels for the run-all progress panel (spec: Pipeline orchestration UX).
// Mirrors cutroom/api/pipeline.py's STAGES/STAGE_LABELS — keep in lockstep.
const STAGE_LABELS = {
  ingest: "Reading files", sync: "Syncing cameras", transcribe: "Transcribing",
  takes: "Analyzing takes", order: "Ordering the story", review: "Checking for suggestions",
  render: "Editing the video", reels: "Making shorts",
};

window.TABS = window.TABS || {};

let state = {
  pid: null, project: null, tab: null, jobs: {}, watching: new Set(),
  runAllJob: null,
  // Job queue (spec v4 §2): state.queue mirrors GET /projects/{pid}/queue,
  // refreshed by the global 2s poll below (queuePoll()) so the top-bar badge
  // and the stage pills stay live no matter which view is open; the Queue
  // tab (ui/tabs/jobs.js) reads this same array instead of polling itself.
  queue: [], queuePolling: false,
};

/* ---------- projects ---------- */

async function loadProjects() {
  const list = await api("/projects");
  const sel = $("#project-select");
  if (sel) {
    const cur = state.pid;
    sel.innerHTML = `<option value="">Select project…</option>` + list.map((p) =>
      `<option value="${p.id}" ${p.id === cur ? "selected" : ""}>${esc(p.name)} (${p.clips})</option>`
    ).join("");
  }
  return list;
}

async function selectProject(pid) {
  state.pid = pid;
  state.runAllJob = null;
  state.queue = [];
  await refreshProject();
  $("#empty-state").hidden = true;
  $("#project-view").hidden = false;
  updateTopbarForProject();
  loadProjects();
  ensureQueuePolling();
}

async function refreshProject() {
  if (!state.pid) return;
  const prevId = state.project?.id;
  state.project = await api(`/projects/${state.pid}`);
  $("#p-name") && ($("#p-name").textContent = state.project.name);
  document.title = `${state.project.name} — Magic Video Editor`;
  renderStageBar();
  renderRunAllPanel();
  const isNewProject = prevId !== state.project.id;
  try {
    if (window.EditorUI) {
      if (isNewProject) await window.EditorUI.onProjectSelected(state.project);
      else window.EditorUI.onProjectRefreshed(state.project);
    }
  } catch (e) {
    console.error("EditorUI failed to render — the rest of the app keeps working", e);
  }
  if (state.tab) renderTab();
}

function updateTopbarForProject() {
  const has = !!state.pid;
  const runBtn = $("#run-all-btn");
  const rail = $("#icon-rail");
  const stageBar = $("#stage-bar");
  if (runBtn) runBtn.hidden = !has && !state.runAllJob;
  if (rail) rail.style.visibility = has ? "visible" : "hidden";
  if (stageBar) stageBar.style.visibility = has ? "visible" : "hidden";
}

/* ---------- stage bar + run pipeline ---------- */

function renderStageBar() {
  const stages = state.project.stages || {};
  $("#stage-bar").innerHTML = STAGES.map(([key, label]) => {
    const st = stages[key];
    // Queue-driven (spec v4 §2): a stage is "running" when its queue item
    // (kind "stage:<key>") is currently the one the worker picked up, not
    // via the old jobs.start() naming convention (queue jobs are all named
    // "queue:<kind>:<pid>" now -- see cutroom/queue.py _run_item).
    const running = state.queue.some((i) => i.status === "running" && i.kind === `stage:${key}`);
    const cls = running ? "running" : st ? st.status : "";
    const title = st?.detail || "";
    return `<button class="stage ${cls}" data-stage="${key}" title="${esc(title)}">${label}</button>`;
  }).join("");
  document.querySelectorAll(".stage").forEach((el) =>
    el.onclick = () => runStage(el.dataset.stage));
}

async function runStage(stage) {
  try {
    await api(`/projects/${state.pid}/queue`, { method: "POST", body: { kind: `stage:${stage}` } });
    await pollQueue();
    setTab("jobs");
  } catch (e) {
    alert(e.message);
  }
}

async function runAll() {
  try {
    await api(`/projects/${state.pid}/queue`, { method: "POST", body: { kind: "run-all" } });
    await pollQueue();
  } catch (e) {
    alert(e.message);
  }
}

async function stopRunAll() {
  const item = state.runAllItem;
  if (!item) return;
  try {
    await api(`/projects/${state.pid}/queue/${item.id}`, { method: "DELETE" });
    await pollQueue();
  } catch (e) { alert(e.message); }
}

function renderRunAllPanel() {
  const panel = $("#run-all-panel");
  const btn = $("#run-all-btn");
  const item = state.runAllItem;
  const job = item?.job_id ? state.jobs[item.job_id] : null;
  const running = !!item && (item.status === "running" || item.status === "pending");
  if (btn) {
    // Resource safety: while a pipeline run is queued/running for this
    // project, the primary button becomes Stop (garnet, same .primary
    // styling) and calls the cancel endpoint instead of starting a second
    // run. This button (and the panel below) lives OUTSIDE the editor grid
    // so it persists no matter which view/drawer is open (spec v3 bug fix).
    btn.textContent = running ? "■ Stop" : "✦ Run pipeline";
    btn.onclick = running ? stopRunAll : runAll;
    btn.hidden = !state.pid;
  }
  if (!panel) return;
  if (!running) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const stages = job?.stages || {};
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

/* ---------- job queue polling (spec v4 §2) ----------
   One global 2s poll drives: the top-bar queue badge (pending count), the
   stage pills' running state, the run-all progress panel (via the running
   run-all item's job_id), and -- when the Queue view is open -- its render.
   ui/tabs/jobs.js reads state.queue directly; it does not poll on its own. */

function ensureQueuePolling() {
  if (state.queuePolling) return;
  state.queuePolling = true;
  pollQueue();
  setInterval(pollQueue, 2000);
}

async function pollQueue() {
  if (!state.pid) return;
  try {
    const { queue } = await api(`/projects/${state.pid}/queue`);
    state.queue = queue;
  } catch (_e) {
    // Transient network hiccup -- keep the last known queue rather than
    // flashing the badge/panel empty.
    return;
  }
  state.runAllItem = state.queue.find(
    (i) => i.kind === "run-all" && (i.status === "running" || i.status === "pending")) || null;
  if (state.runAllItem?.job_id) {
    try { state.jobs[state.runAllItem.job_id] = await api(`/jobs/${state.runAllItem.job_id}`); }
    catch (_e) { /* job may have just been cancelled/gc'd -- ignore */ }
  }
  updateQueueBadge();
  renderStageBar();
  renderRunAllPanel();
  if (state.tab === "jobs") renderTab();
  if (state.project && (!state.runAllItem || state.runAllItem.status !== "running")) {
    // A run-all (or any stage) that just finished may have changed
    // stages/reels/etc -- pick that up without waiting for a manual action.
    const wasRunning = state._queueHadRunning;
    const nowRunning = state.queue.some((i) => i.status === "running");
    if (wasRunning && !nowRunning) refreshProject();
    state._queueHadRunning = nowRunning;
  }
}

function updateQueueBadge() {
  const badge = $("#queue-badge");
  if (!badge) return;
  const pending = state.queue.filter((i) => i.status === "pending").length;
  const running = state.queue.filter((i) => i.status === "running").length;
  badge.hidden = !state.pid;
  $("#queue-badge-count").textContent = pending + running;
  badge.classList.toggle("active", running > 0);
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

/* ---------- secondary-view drawer (Takes / Reels / Settings / Activity) ---------- */

function setTab(tab) {
  state.tab = tab;
  $("#drawer-overlay").hidden = false;
  document.querySelectorAll("[data-tab]").forEach((el) =>
    el.classList.toggle("active", el.dataset.tab === tab));
  document.querySelectorAll(".tabpane").forEach((el) =>
    el.hidden = el.id !== `tab-${tab}`);
  renderTab();
}

function closeDrawer() {
  state.tab = null;
  $("#drawer-overlay").hidden = true;
  document.querySelectorAll("[data-tab]").forEach((el) => el.classList.remove("active"));
}

/* ---------- Settings: full-viewport overlay page, not a drawer tab
   (spec v4 §5) -- opened only from the gear pinned to the bottom of the
   media bin. Reuses the same #tab-settings container id (and window.TABS.settings
   renderer) that used to live inside the drawer, so ui/tabs/settings.js
   needs no changes to keep mounting into it. ---------- */

function openSettings() {
  state.tab = "settings";
  $("#settings-overlay").hidden = false;
  renderTab();
}

function closeSettings() {
  state.tab = null;
  $("#settings-overlay").hidden = true;
}

function renderTab() {
  if (!state.project || !state.tab) return;
  const fn = window.TABS[state.tab];
  if (!fn) return;
  try {
    fn();
  } catch (e) {
    console.error(`Failed to render ${state.tab}`, e);
    const el = $(`#tab-${state.tab}`);
    if (el) el.innerHTML = '<div class="dim">This view failed to render — see console.</div>';
  }
}

/* ---------- keyboard: Esc closes the drawer (editor shortcuts live in
   ui/editor/timeline.js, which owns the timeline/player) ---------- */

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#settings-overlay").hidden) closeSettings();
  else if (state.tab) closeDrawer();
});

/* ---------- boot ---------- */

async function boot() {
  document.querySelectorAll("[data-tab]").forEach((el) => el.onclick = () => setTab(el.dataset.tab));
  $("#drawer-close").onclick = closeDrawer;
  $("#drawer-overlay").addEventListener("click", (e) => {
    if (e.target.id === "drawer-overlay") closeDrawer();
  });
  $("#settings-gear").onclick = openSettings;
  $("#settings-close").onclick = closeSettings;
  $("#queue-badge").onclick = () => setTab("jobs");

  $("#new-project").onclick = async () => {
    const name = prompt("Project name:");
    if (!name) return;
    const p = await api("/projects", { method: "POST", body: { name } });
    await selectProject(p.id);
  };
  $("#project-select").onchange = (e) => {
    if (e.target.value) selectProject(e.target.value);
  };

  const runAllBtn = $("#run-all-btn");
  if (runAllBtn) runAllBtn.onclick = runAll;

  try {
    const h = await api("/health");
    $("#health").innerHTML = `
      ffmpeg <span class="${h.ffmpeg ? "ok" : "bad"}">${h.ffmpeg ? "✓" : "missing"}</span> ·
      ollama <span class="${h.ollama ? "ok" : "bad"}">${h.ollama ? "✓" : "down"}</span><br>
      <span title="${esc(h.whisper)}">${esc(h.model)}</span>`;
  } catch (_e) {
    // Health is best-effort UI chrome; never let it block boot.
  }

  updateTopbarForProject();
  const list = await loadProjects();
  if (list.length === 1) await selectProject(list[0].id);
}
boot();
