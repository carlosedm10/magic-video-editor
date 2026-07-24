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
  await refreshProject();
  $("#empty-state").hidden = true;
  $("#project-view").hidden = false;
  updateTopbarForProject();
  loadProjects();
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
  } catch (e) {
    if (e.status === 409 && e.detail?.job) {
      // Already running (repeated click / two tabs) -- don't error, just
      // start tracking the job that's actually in flight (spec: resource safety).
      watchJob(e.detail.job);
      setTab("jobs");
    } else {
      alert(e.message);
    }
  }
}

async function runAll() {
  if (state.runAllJob && state.jobs[state.runAllJob]?.status === "running") return;
  try {
    const { job } = await api(`/projects/${state.pid}/run-all`, { method: "POST" });
    state.runAllJob = job;
    watchJob(job);
  } catch (e) {
    if (e.status === 409 && e.detail?.job) {
      state.runAllJob = e.detail.job;
      watchJob(e.detail.job);
    } else {
      alert(e.message);
    }
  }
}

async function stopRunAll() {
  const jid = state.runAllJob;
  if (!jid) return;
  try {
    await api(`/jobs/${jid}/cancel`, { method: "POST" });
  } catch (e) { alert(e.message); }
}

function renderRunAllPanel() {
  const panel = $("#run-all-panel");
  const btn = $("#run-all-btn");
  const job = state.runAllJob ? state.jobs[state.runAllJob] : null;
  const running = !!job && job.status === "running";
  if (btn) {
    // Resource safety: while a pipeline job is running for this project,
    // the primary button becomes Stop (garnet, same .primary styling) and
    // calls the cancel endpoint instead of starting a second run. This
    // button (and the panel below) lives OUTSIDE the editor grid so it
    // persists no matter which view/drawer is open (spec v3 bug fix).
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
  if (e.key === "Escape" && state.tab) closeDrawer();
});

/* ---------- boot ---------- */

async function boot() {
  document.querySelectorAll("[data-tab]").forEach((el) => el.onclick = () => setTab(el.dataset.tab));
  $("#drawer-close").onclick = closeDrawer;
  $("#drawer-overlay").addEventListener("click", (e) => {
    if (e.target.id === "drawer-overlay") closeDrawer();
  });

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
