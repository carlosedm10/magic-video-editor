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

/* ---------- stale-project recovery ----------
   A project can vanish out from under an open tab (deleted in another tab,
   or -- the bug this guards against -- a stale persisted selection from a
   previous session whose project.json is simply gone). Every project-scoped
   endpoint 404s at that point; without this, a browser left on that project
   just polls /queue every 2s forever against a dead id. selectProject()
   remembers the current pid in localStorage so a reload can restore it;
   the very same key is what gets cleared here once we've confirmed the
   project is actually gone. */

const LAST_PROJECT_KEY = "mve_last_project_id";

function getPersistedProjectId() {
  try { return localStorage.getItem(LAST_PROJECT_KEY); } catch (_e) { return null; }
}

function setPersistedProjectId(pid) {
  try {
    if (pid) localStorage.setItem(LAST_PROJECT_KEY, pid);
    else localStorage.removeItem(LAST_PROJECT_KEY);
  } catch (_e) { /* private browsing / storage disabled -- non-fatal */ }
}

function showToast(msg) {
  const el = document.createElement("div");
  el.className = "mve-toast";
  el.textContent = msg;
  Object.assign(el.style, {
    position: "fixed", bottom: "24px", left: "50%", transform: "translateX(-50%)",
    background: "var(--panel, #222)", color: "var(--text, #fff)",
    padding: "10px 18px", borderRadius: "8px", fontSize: "13px",
    boxShadow: "0 4px 16px rgba(0,0,0,.3)", zIndex: 9999,
    opacity: "0", transition: "opacity .2s ease",
  });
  document.body.appendChild(el);
  requestAnimationFrame(() => { el.style.opacity = "1"; });
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

// Guards against handling the same project's disappearance twice (e.g. a
// refreshProject() 404 and a pollQueue() 404 landing back to back).
let _handlingProjectGone = false;

function handleProjectGone(pid) {
  if (_handlingProjectGone || state.pid !== pid) return;
  _handlingProjectGone = true;
  console.warn(`Project ${pid} no longer exists -- returning to Home.`);
  if (getPersistedProjectId() === pid) setPersistedProjectId(null);
  state.watching.clear();
  goHome();
  showToast("Project no longer exists");
  _handlingProjectGone = false;
}
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- Lucide icons (spec v5.5) ----------
   ui/vendor/lucide.min.js is loaded before this file (see index.html) and
   exposes window.lucide. This is the ONE shared helper every render path in
   the app calls right after injecting HTML containing <i data-lucide="…">
   tags, so freshly-added icons actually get swapped for the inline SVGs.
   Guarded: never throw if lucide didn't load for some reason (e.g. a build
   that drops the vendor file) — the app must keep working, just with bare
   (invisible, no layout break) <i> tags instead of icons. */
window.refreshIcons = () => {
  try {
    window.lucide?.createIcons({ attrs: { width: 16, height: 16, "stroke-width": 1.75 } });
  } catch (e) {
    console.error("refreshIcons failed", e);
  }
};
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
// Mirrors magic_video_editor/api/pipeline.py's STAGES/STAGE_LABELS — keep in lockstep.
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
  state._queuePoll404s = 0;
  setPersistedProjectId(pid);
  await refreshProject();
  hideHome();
  $("#project-view").hidden = false;
  updateTopbarForProject();
  loadProjects();
  ensureQueuePolling();
}

/* ---------- routing: Projects Home vs. the editor (spec v5.2 + PRINCIPLE)
   window.HomeView is owned by a sibling agent building ui/home.js in
   parallel and exposes {mount(container), unmount()}; every touch point
   here is guarded so this file never throws regardless of load order or
   whether that build has landed yet. ---------- */

function showHome() {
  $("#project-view").hidden = true;
  const el = $("#home-view");
  el.hidden = false;
  if (typeof window.HomeView !== "undefined" && window.HomeView?.mount) {
    try { window.HomeView.mount(el); }
    catch (e) { console.error("HomeView failed to mount", e); }
  } else if (!el.dataset.fallback) {
    el.dataset.fallback = "1";
    el.innerHTML = `<div class="empty"><div class="empty-inner">
      <div class="empty-brand">✦ Magic Video Editor</div>
      <div class="dim">Create or select a project to start.</div>
    </div></div>`;
  }
}

function hideHome() {
  const el = $("#home-view");
  if (typeof window.HomeView !== "undefined" && window.HomeView?.unmount) {
    try { window.HomeView.unmount(); }
    catch (e) { console.error("HomeView failed to unmount", e); }
  }
  el.hidden = true;
}

function goHome() {
  state.pid = null;
  state.project = null;
  state.tab = null;
  state.queue = [];
  state._queuePoll404s = 0;
  state.runAllJob = null;
  state.runAllItem = null;
  closeDrawer();
  closeSettings();
  closeExportDialog();
  closeActivityPopover();
  document.title = "Magic Video Editor";
  updateTopbarForProject();
  showHome();
  loadProjects();
}

async function refreshProject() {
  if (!state.pid) return;
  const prevId = state.project?.id;
  const pid = state.pid;
  let project;
  try {
    project = await api(`/projects/${pid}`);
  } catch (e) {
    if (e.status === 404) { handleProjectGone(pid); return; }
    throw e;
  }
  state.project = project;
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
  refreshIcons();
}

function updateTopbarForProject() {
  const has = !!state.pid;
  const runBtn = $("#run-all-btn");
  const rail = $("#icon-rail");
  const stageBar = $("#stage-bar");
  const exportBtn = $("#export-btn");
  if (runBtn) runBtn.hidden = !has && !state.runAllJob;
  if (rail) rail.style.visibility = has ? "visible" : "hidden";
  if (stageBar) stageBar.style.visibility = has ? "visible" : "hidden";
  if (exportBtn) exportBtn.hidden = !has;
  if (!has) updateActivityChip();
}

/* ---------- pipeline status chip (spec v7 §7.2) ----------
   Replaces the old 8 always-visible stage pills with ONE compact chip
   ("Pipeline ✓" done / "Pipeline N/8" in progress / garnet error) that opens
   an anchored popover listing every stage with its status and a per-stage
   re-run button (same runStage() the old pills called). Self-contained:
   injects its own <style> + popover DOM into #stage-bar (still the anchor
   element from ui/index.html) the first time renderStageBar() runs, the
   same "chrome injection" pattern ui/editor/player.js and
   ui/editor/timeline.js already use for pieces index.html/style.css don't
   know about.

   SYNC-FIX root cause (field bug: strip showed all-green/near-100% while
   the chip popover correctly showed "4 Takes ERROR" + stages 5-8 pending):
   the chip/popover and the run-all strip used to read TWO DIFFERENT stage
   objects. The chip read state.project.stages -- the persisted project
   snapshot, which is only refreshed by refreshProject(), and refreshProject()
   during a run-all only fires once the whole item stops running (pollQueue's
   wasRunning->!nowRunning check) or incidentally from an unrelated action
   elsewhere (e.g. ui/tabs/takes.js's own refreshProject() call, mediabin
   uploads, watchJob() finishing a different job). So mid-run, state.project
   .stages could be showing this project's PREVIOUS run's final stage
   statuses (all done) for the entire duration, or the correct fresh ones,
   depending entirely on which unrelated refresh happened to land last --
   nondeterministic. The strip, meanwhile, read job.stages off the CURRENT
   run's live job (state.jobs[runAllItem.job_id], refreshed every 2s poll
   tick) -- a different object, on a different cadence, that could easily
   disagree with whatever state.project.stages happened to be holding.
   _derivePipelineStages() below is now the ONE place that decides, per
   poll tick, what each stage's status/progress is; both renderStageBar
   (chip + popover) and renderRunAllPanel (strip) call it and render off
   the identical returned array -- there is no second path back to reading
   state.project.stages directly while anything is running. */

let _pipelineRows = [];

function _derivePipelineStages() {
  const persisted = state.project?.stages || {};
  const raItem = state.runAllItem;
  const raJob = raItem?.job_id ? state.jobs[raItem.job_id] : null;
  const raLive = !!raItem && (raItem.status === "running" || raItem.status === "pending") && raJob?.stages;
  return STAGES.map(([key]) => {
    if (raLive && raJob.stages[key]) {
      const st = raJob.stages[key];
      return { key, status: st.status, progress: st.progress || 0, detail: st.detail || "" };
    }
    // A lone per-stage re-run (runStage(), not part of a run-all) has no
    // job.stages map of its own -- its queue item's own live status IS the
    // truth for that one key.
    const solo = state.queue.some((i) => i.status === "running" && i.kind === `stage:${key}`);
    if (solo) return { key, status: "running", progress: 0, detail: "" };
    const st = persisted[key];
    return { key, status: st?.status || "pending", progress: st?.status === "done" ? 1 : 0, detail: st?.detail || "" };
  });
}

function _ensurePipelineChip() {
  let chip = document.getElementById("pipeline-chip");
  if (chip) return chip;
  if (!document.getElementById("pipeline-chip-styles")) {
    const style = document.createElement("style");
    style.id = "pipeline-chip-styles";
    style.textContent = `
      .pipeline-chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px;
        border-radius: 999px; border: 1px solid var(--border); background: var(--panel2);
        color: var(--dim); font-size: 12px; cursor: pointer; white-space: nowrap; line-height: 1; }
      .pipeline-chip:hover { border-color: var(--accent); color: var(--text); }
      .pipeline-chip.ok { border-color: var(--accent2); color: var(--accent2); }
      .pipeline-chip.running { border-color: var(--accent); color: var(--accent-hover); }
      .pipeline-chip.error { border-color: var(--accent-hover); color: var(--accent-hover); }
      .pipeline-chip i { width: 14px; height: 14px; }
      /* position: fixed (not absolute-inside-#stage-bar) + appended to
         document.body, exactly like .activity-popover (ui/exportux.css) --
         #stage-bar has overflow-x: auto for its own horizontal-scroll
         reasons, which per the CSS spec forces overflow-y: auto too the
         instant overflow-x isn't "visible", turning the bar into a clipping
         ancestor that silently swallowed an absolutely-positioned popover
         child (it was in the DOM, fully wired, just invisible). Top/left are
         set inline from the chip's live getBoundingClientRect() each time it
         opens (see togglePipelinePopover) since, unlike the always-top-right
         Activity popover, this chip's position shifts with the stage bar's
         content. */
      .pipeline-popover { position: fixed; width: 260px;
        background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 8px;
        backdrop-filter: blur(16px); box-shadow: 0 8px 24px rgba(0,0,0,.4); z-index: 55; font-size: 12px;
        max-height: min(70vh, 480px); overflow-y: auto; }
      .pipeline-popover-row { display: flex; align-items: center; gap: 8px; padding: 5px 4px; border-radius: 8px; }
      .pipeline-popover-row:hover { background: var(--panel2); }
      .pipeline-popover-label { flex: 1; color: var(--text); }
      .pipeline-popover-status { font-size: 10px; text-transform: uppercase; letter-spacing: .03em; }
      .pipeline-popover-status.done { color: var(--accent2); }
      .pipeline-popover-status.running { color: var(--accent-hover); }
      .pipeline-popover-status.error { color: var(--accent-hover); }
      .pipeline-popover-status.pending { color: var(--dim); }
      .pipeline-popover-rerun { background: none; border: 1px solid var(--border); border-radius: 6px;
        color: var(--dim); cursor: pointer; padding: 2px 5px; display: inline-flex; }
      .pipeline-popover-rerun:hover { border-color: var(--accent); color: var(--text); }
    `;
    document.head.appendChild(style);
  }
  const bar = document.getElementById("stage-bar");
  if (!bar) return null;
  bar.style.position = "relative";
  chip = document.createElement("button");
  chip.id = "pipeline-chip";
  chip.className = "pipeline-chip";
  chip.innerHTML = '<i data-lucide="workflow"></i><span class="pipeline-chip-label">Pipeline</span>';
  chip.onclick = (e) => { e.stopPropagation(); togglePipelinePopover(); };
  bar.appendChild(chip);
  // The popover itself lives on document.body now (see togglePipelinePopover),
  // not inside `bar` -- so "outside click" must check both, or the very
  // click on a re-run button inside the popover would count as "outside"
  // and close/remove it before that button's own onclick gets a chance to
  // fire (pointerdown precedes click).
  document.addEventListener("pointerdown", (e) => {
    if (!_pipelinePopoverOpen()) return;
    const pop = document.getElementById("pipeline-popover");
    if (bar.contains(e.target) && !chip.contains(e.target)) return; // clicks elsewhere in the stage bar chrome don't count
    if (chip.contains(e.target) || pop?.contains(e.target)) return;
    togglePipelinePopover(false);
  });
  return chip;
}

function _pipelinePopoverOpen() {
  return !!document.getElementById("pipeline-popover");
}

// Fixed-position, body-anchored popover (see the .pipeline-popover comment
// above for why) -- top/left computed from the chip's live position each
// time it opens so it tracks the chip wherever the stage bar laid it out,
// clamped so it never runs off the right edge of the window.
function togglePipelinePopover(force) {
  const open = force != null ? force : !_pipelinePopoverOpen();
  const chip = document.getElementById("pipeline-chip");
  if (!chip) return;
  if (open) {
    let pop = document.getElementById("pipeline-popover");
    if (!pop) {
      pop = document.createElement("div");
      pop.id = "pipeline-popover";
      pop.className = "pipeline-popover";
      document.body.appendChild(pop);
    }
    const rect = chip.getBoundingClientRect();
    const width = 260;
    const left = Math.min(rect.left, window.innerWidth - width - 12);
    pop.style.top = `${Math.round(rect.bottom + 6)}px`;
    pop.style.left = `${Math.round(Math.max(8, left))}px`;
    renderPipelinePopover();
  } else {
    document.getElementById("pipeline-popover")?.remove();
  }
}

function renderPipelinePopover() {
  const pop = document.getElementById("pipeline-popover");
  if (!pop) return;
  pop.innerHTML = _pipelineRows.map((r) => `
    <div class="pipeline-popover-row">
      <span class="pipeline-popover-label">${esc(r.label)}</span>
      <span class="pipeline-popover-status ${r.status}" title="${esc(r.detail)}">${r.status}</span>
      <button class="pipeline-popover-rerun" data-stage="${r.key}" title="Re-run this stage"><i data-lucide="rotate-cw"></i></button>
    </div>`).join("");
  pop.querySelectorAll("[data-stage]").forEach((btn) => btn.onclick = () => runStage(btn.dataset.stage));
  refreshIcons();
}

// ONE source of truth for per-stage status (root-cause fix, see the long
// comment on _derivePipelineStages below): renderStageBar (chip + popover)
// and renderRunAllPanel (the progress strip) both call this, once per poll
// tick, and render off the exact same returned array -- neither one is
// allowed to independently read state.project.stages vs a job object again.
const _STAGE_SHORT_LABELS = Object.fromEntries(STAGES);

function renderStageBar() {
  const chip = _ensurePipelineChip();
  const rows = _derivePipelineStages();
  let doneCount = 0;
  let runningCount = 0;
  let errorLabel = null;
  _pipelineRows = rows.map(({ key, status, progress, detail }) => {
    const label = _STAGE_SHORT_LABELS[key] || key;
    if (status === "done") doneCount++;
    else if (status === "running") runningCount++;
    else if (status === "error" && !errorLabel) errorLabel = label;
    return { key, label, status, progress, detail };
  });
  if (!chip) return;
  const total = STAGES.length;
  let cls;
  let text;
  if (errorLabel) { cls = "error"; text = "Pipeline !"; chip.title = `Error in ${errorLabel} — click for details`; }
  else if (doneCount === total) { cls = "ok"; text = "Pipeline ✓"; chip.title = "All stages done — click for details"; }
  else { cls = runningCount > 0 ? "running" : ""; text = `Pipeline ${doneCount}/${total}`; chip.title = "Click for stage details and re-run"; }
  chip.className = `pipeline-chip ${cls}`;
  const label = chip.querySelector(".pipeline-chip-label");
  if (label) label.textContent = text;
  if (_pipelinePopoverOpen()) renderPipelinePopover();
  refreshIcons();
}

async function runStage(stage) {
  try {
    await api(`/projects/${state.pid}/queue`, { method: "POST", body: { kind: `stage:${stage}` } });
    await pollQueue();
    openActivityPopover();
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
  const running = !!item && (item.status === "running" || item.status === "pending");
  if (btn) {
    // Resource safety: while a pipeline run is queued/running for this
    // project, the primary button becomes Stop (garnet, same .primary
    // styling) and calls the cancel endpoint instead of starting a second
    // run. This button (and the panel below) lives OUTSIDE the editor grid
    // so it persists no matter which view/drawer is open (spec v3 bug fix).
    btn.innerHTML = running
      ? '<i data-lucide="square"></i> Stop'
      : '<i data-lucide="sparkles"></i> Run pipeline';
    btn.onclick = running ? stopRunAll : runAll;
    btn.hidden = !state.pid;
    refreshIcons();
  }
  if (!panel) return;
  if (!running) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  // Same _pipelineRows the chip/popover just rendered from this tick
  // (renderStageBar() always runs before renderRunAllPanel() -- see
  // pollQueue()/watchJob() below) -- one source of truth, per the
  // _derivePipelineStages comment above. A stage's progress freezes the
  // instant it errors (the backend stops mutating job.stages for any stage
  // past the one that raised), so the bar simply stops advancing there too.
  panel.hidden = false;
  panel.innerHTML = _pipelineRows.map(({ key, status, progress, detail }) => {
    const pct = Math.round((progress || 0) * 100);
    const errored = status === "error";
    return `<div class="run-all-item">
      <div class="run-all-row">
        <span class="run-all-label">${esc(STAGE_LABELS[key] || key)}</span>
        <div class="run-all-bar"><div class="run-all-fill ${status}" style="width:${pct}%"></div></div>
        <span class="run-all-pct">${errored ? "!" : `${pct}%`}</span>
      </div>
      ${errored && detail ? `<span class="run-all-error" title="${esc(detail)}">${esc(detail)}</span>` : ""}
    </div>`;
  }).join("");
}

// Surfaces a toast ("<stage>: <first line of error>") the one time a
// run-all transitions from running/pending to errored, using the exact same
// job.stages the strip/chip just rendered from (_pipelineRows) to name the
// stage that actually failed -- never re-derived from a second source, and
// never re-fired on subsequent polls for the same finished item.
function _maybeToastPipelineFailure(prevItem) {
  if (!prevItem || state.runAllItem?.id === prevItem.id) return;
  const finished = state.queue.find((i) => i.id === prevItem.id);
  if (!finished || finished.status !== "error") return;
  const toasted = (state._toastedRunAllErrorIds ??= new Set());
  if (toasted.has(finished.id)) return;
  toasted.add(finished.id);
  const failedRow = _pipelineRows.find((r) => r.status === "error");
  const stageLabel = failedRow ? (STAGE_LABELS[failedRow.key] || failedRow.key) : "Pipeline";
  const detail = failedRow?.detail || finished.error || "";
  showToast(`${stageLabel} failed${detail ? `: ${detail.split("\n")[0]}` : ""}`);
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
  const pid = state.pid;
  try {
    const { queue } = await api(`/projects/${pid}/queue`);
    state.queue = queue;
    state._queuePoll404s = 0;
  } catch (e) {
    // A single 404 could be a transient blip (e.g. mid-delete race) -- only
    // treat the project as gone once 3 polls in a row against the SAME pid
    // 404, so we don't yank the user out of a project on one flaky response.
    // Any other error (network hiccup, 5xx) just keeps the last known queue
    // rather than flashing the badge/panel empty.
    if (e.status === 404) {
      state._queuePoll404s = (state._queuePoll404s || 0) + 1;
      if (state._queuePoll404s >= 3) handleProjectGone(pid);
    }
    return;
  }
  const prevRunAllItem = state.runAllItem;
  state.runAllItem = state.queue.find(
    (i) => i.kind === "run-all" && (i.status === "running" || i.status === "pending")) || null;
  if (state.runAllItem?.job_id) {
    try { state.jobs[state.runAllItem.job_id] = await api(`/jobs/${state.runAllItem.job_id}`); }
    catch (_e) { /* job may have just been cancelled/gc'd -- ignore */ }
  }
  updateActivityChip();
  renderStageBar();
  renderRunAllPanel();
  _maybeToastPipelineFailure(prevRunAllItem);
  if (window.ActivityPopover?.isOpen()) window.ActivityPopover.render();
  if (state.project && (!state.runAllItem || state.runAllItem.status !== "running")) {
    // A run-all (or any stage) that just finished may have changed
    // stages/reels/etc -- pick that up without waiting for a manual action.
    const wasRunning = state._queueHadRunning;
    const nowRunning = state.queue.some((i) => i.status === "running");
    if (wasRunning && !nowRunning) refreshProject();
    state._queueHadRunning = nowRunning;
  }
}

/* ---------- Activity chip (spec v5.4 §2) ----------
   Ambient replacement for the old Queue badge: invisible while idle, shows
   a spinner + the running item's label + its % while any queue item is
   running/pending. Click opens the Background Tasks popover implemented in
   ui/tabs/jobs.js (window.ActivityPopover). */

function updateActivityChip() {
  const chip = $("#activity-chip");
  if (!chip) return;
  if (!state.pid) { chip.hidden = true; return; }
  const running = state.queue.find((i) => i.status === "running");
  const pendingCount = state.queue.filter((i) => i.status === "pending").length;
  const busy = !!running || pendingCount > 0;
  chip.hidden = !busy;
  if (!busy) return;
  const label = running
    ? (typeof queueKindLabel === "function" ? queueKindLabel(running.kind) : running.kind)
    : `${pendingCount} queued`;
  const job = running?.job_id ? state.jobs[running.job_id] : null;
  const pct = running ? Math.round(((job?.progress ?? running.progress) || 0) * 100) : null;
  $("#activity-chip-label").textContent = label;
  $("#activity-chip-pct").textContent = pct === null ? "" : `${pct}%`;
}

function watchJob(jid) {
  if (state.watching.has(jid)) return;
  state.watching.add(jid);
  const poll = async () => {
    const job = await api(`/jobs/${jid}`);
    state.jobs[jid] = job;
    if (window.ActivityPopover?.isOpen()) window.ActivityPopover.render();
    renderStageBar();
    renderRunAllPanel();
    if (job.status === "running") setTimeout(poll, 1200);
    else { state.watching.delete(jid); await refreshProject(); }
  };
  poll();
}

/* ---------- Export dialog (spec v5.4 §1) ----------
   Small modal replacing the render buttons that used to live in the Queue
   view: pick Final video / All reels / a specific reel, see the export
   destination (settings.export_dir, click opens it in Finder), one primary
   CTA enqueues the corresponding queue item(s) and closes. */

async function openExportDialog() {
  if (!state.pid) return;
  const reels = state.project?.reels || [];
  const sel = $("#export-reel-select");
  sel.innerHTML = reels.length
    ? reels.map((r) => `<option value="${r.id}">${esc(r.title || `Reel #${r.rank}`)}</option>`).join("")
    : `<option value="">No reels yet</option>`;
  $(`input[name="export-what"][value="final"]`).checked = true;
  try {
    const settings = await api("/settings");
    state.settings = settings;
    $("#export-dest-path").textContent = settings.export_dir;
  } catch (_e) {
    $("#export-dest-path").textContent = state.settings?.export_dir || "";
  }
  $("#export-overlay").hidden = false;
  refreshIcons();
}

function closeExportDialog() {
  const el = $("#export-overlay");
  if (el) el.hidden = true;
}

async function runExport() {
  const what = document.querySelector('input[name="export-what"]:checked')?.value || "final";
  try {
    if (what === "final") {
      await api(`/projects/${state.pid}/queue`, { method: "POST", body: { kind: "final_render" } });
    } else if (what === "reels") {
      const reels = state.project?.reels || [];
      if (!reels.length) { alert("No reels to export yet."); return; }
      for (const r of reels) {
        await api(`/projects/${state.pid}/queue`,
          { method: "POST", body: { kind: `reel_render:${r.id}`, payload: { reel_id: r.id } } });
      }
    } else {
      const rid = $("#export-reel-select").value;
      if (!rid) { alert("No reel selected."); return; }
      await api(`/projects/${state.pid}/queue`,
        { method: "POST", body: { kind: `reel_render:${rid}`, payload: { reel_id: rid } } });
    }
    await pollQueue();
    closeExportDialog();
  } catch (e) {
    alert(e.message);
  }
}

/* ---------- secondary-view drawer (Takes / Reels / Settings) ---------- */

function setTab(tab) {
  // Back-compat: ui/tabs/reels.js (not owned by this task) still calls
  // setTab("jobs") after enqueuing a reel render, from before the Queue
  // drawer tab was replaced by the Activity chip/popover (spec v5.4). There
  // is no "jobs" drawer pane anymore -- redirect to the popover instead of
  // opening an empty drawer.
  if (tab === "jobs") { window.ActivityPopover?.open(); return; }
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
  refreshIcons();
}

/* ---------- keyboard: Esc closes whichever overlay is open, innermost
   first (editor shortcuts live in ui/editor/timeline.js, which owns the
   timeline/player) ---------- */

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#export-overlay").hidden) closeExportDialog();
  else if (window.ActivityPopover?.isOpen()) window.ActivityPopover.close();
  else if (!$("#settings-overlay").hidden) closeSettings();
  else if (state.tab) closeDrawer();
  // spec v7 §7.1: Esc also backs out of the player's Source mode (media bin
  // clip preview) when no other overlay/drawer is in front of it.
  else if (window.EditorUI?.player?.sourceClipId) window.EditorUI.player.exitSourceMode();
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
  $("#home-btn").onclick = goHome;

  $("#export-btn").onclick = openExportDialog;
  $("#export-close").onclick = closeExportDialog;
  $("#export-cta").onclick = runExport;
  $("#export-overlay").addEventListener("click", (e) => {
    if (e.target.id === "export-overlay") closeExportDialog();
  });
  $("#export-dest").onclick = async () => {
    const path = state.settings?.export_dir || $("#export-dest-path").textContent;
    if (!path) return;
    try { await api("/open-folder", { method: "POST", body: { path } }); }
    catch (e) { alert(e.message); }
  };
  $("#activity-chip").onclick = () => window.ActivityPopover?.toggle();

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
  const savedPid = getPersistedProjectId();
  if (savedPid && list.some((p) => p.id === savedPid)) {
    await selectProject(savedPid);
  } else {
    // Stale persisted selection whose project.json is already gone (the
    // exact bug this guards against) -- drop it silently rather than
    // leaving it around to be picked up (and 404 against) next boot.
    if (savedPid) setPersistedProjectId(null);
    if (list.length === 1) await selectProject(list[0].id);
    else showHome();
  }
  refreshIcons();

  initUpdateBanner();
}
boot();

/* ---------- Auto-update banner (spec v6 "Auto-update via GitHub Releases")
   -- ADDITIVE ONLY, does not touch anything above. GET /api/update is
   populated by a non-blocking background check the backend fires at
   startup (magic_video_editor/updater.py), so it may not be `checked` yet
   the instant boot() runs -- poll a few times, a few seconds apart, then
   give up quietly. Dismissing hides the banner for the rest of this
   session (sessionStorage) without asking the backend to forget the
   update; reloading the app shows it again until it's actually installed. */

let _updateStatus = null;

async function initUpdateBanner() {
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      _updateStatus = await api("/update");
    } catch (_e) {
      return; // best-effort chrome -- a network hiccup here must never surface
    }
    if (_updateStatus.checked) break;
    await new Promise((r) => setTimeout(r, 3000));
  }
  renderUpdateBanner();

  $("#update-banner-cta").onclick = installUpdate;
  $("#update-banner-dismiss").onclick = () => {
    sessionStorage.setItem("mve_update_dismissed", _updateStatus?.latest_version || "1");
    $("#update-banner").hidden = true;
  };
}

function renderUpdateBanner() {
  const banner = $("#update-banner");
  if (!banner || !_updateStatus?.available) return;
  if (sessionStorage.getItem("mve_update_dismissed") === (_updateStatus.latest_version || "1")) return;
  $("#update-banner-text").textContent =
    `Nueva versión ${_updateStatus.latest_version} — Actualizar ahora`;
  banner.hidden = false;
  refreshIcons();
}

async function installUpdate() {
  if (!confirm(
    `Se descargará e instalará Magic Video Editor ${_updateStatus.latest_version}. ` +
    "La app se cerrará y se reabrirá automáticamente. ¿Continuar?"
  )) return;

  const cta = $("#update-banner-cta");
  const progress = $("#update-banner-progress");
  cta.disabled = true;
  progress.hidden = false;
  progress.textContent = "0%";

  let job;
  try {
    job = await api("/update/install", { method: "POST" });
  } catch (e) {
    cta.disabled = false;
    progress.hidden = true;
    alert(e.message); // e.g. dev-mode "git pull instead" -- see updater.py
    return;
  }

  const poll = async () => {
    let j;
    try { j = await api(`/jobs/${job.job_id}`); }
    catch (_e) { return; } // the app is likely exiting to relaunch -- expected
    progress.textContent = `${Math.round((j.progress || 0) * 100)}%`;
    if (j.status === "running") setTimeout(poll, 800);
    else if (j.status === "error") {
      cta.disabled = false;
      progress.hidden = true;
      alert(`Update failed: ${j.error}`);
    }
    // status "done": the process is about to os._exit() itself and the
    // update helper relaunches a fresh instance -- nothing left to do here.
  };
  poll();
}
