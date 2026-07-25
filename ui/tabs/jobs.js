/* Background Tasks popover (spec v5.4 §2-3) — replaces the old Queue
   drawer tab. Anchored under the top-bar Activity chip (#activity-chip,
   wired in core.js): running item with progress + cancel, pending list
   (drag-to-reorder / remove), last 3 finished (subtle). Each row has a
   "details" disclosure expanding a capped-height monospace log fetched
   from GET /jobs/{id} — the raw log wall is never the default view.

   This file no longer registers into window.TABS (the Queue drawer tab is
   gone; Takes/Reels still use that mechanism). It exposes
   window.ActivityPopover = { open, close, toggle, isOpen, render } and
   core.js's pollQueue()/watchJob() call render() whenever the popover is
   open, since state.queue/state.jobs are refreshed there. */

const QUEUE_KIND_LABELS = {
  "run-all": "Run pipeline",
  preview_render: "Render preview",
  final_render: "Final render",
  thumbs: "Thumbnails",
  proxies: "Proxies",
};

const _activityExpanded = new Set(); // queue item ids whose log disclosure is open

function queueKindLabel(kind) {
  if (QUEUE_KIND_LABELS[kind]) return QUEUE_KIND_LABELS[kind];
  if (kind.startsWith("stage:")) return STAGE_LABELS[kind.slice(6)] || kind;
  if (kind.startsWith("reel_render:")) {
    const rid = kind.slice("reel_render:".length);
    const reel = state.project?.reels?.find((r) => r.id === rid);
    return reel ? `Reel: ${reel.title || "#" + reel.rank}` : `Reel render (${rid})`;
  }
  return kind;
}

let _activityOpen = false;

function isActivityPopoverOpen() {
  return _activityOpen;
}

function openActivityPopover() {
  _activityOpen = true;
  const pop = $("#activity-popover");
  pop.hidden = false;
  renderActivityPopover();
  document.addEventListener("mousedown", _activityOutsideClick, true);
}

function closeActivityPopover() {
  _activityOpen = false;
  const pop = $("#activity-popover");
  if (pop) pop.hidden = true;
  document.removeEventListener("mousedown", _activityOutsideClick, true);
}

function toggleActivityPopover() {
  if (_activityOpen) closeActivityPopover(); else openActivityPopover();
}

function _activityOutsideClick(e) {
  const pop = $("#activity-popover");
  const chip = $("#activity-chip");
  if (!pop || pop.hidden) return;
  if (pop.contains(e.target) || chip?.contains(e.target)) return;
  closeActivityPopover();
}

function _logDisclosure(item, job) {
  const open = _activityExpanded.has(item.id);
  const log = job?.log || [];
  return `
    <button class="btn small activity-details-toggle" data-details="${item.id}">
      ${open ? '<i data-lucide="chevron-down"></i> Hide details' : '<i data-lucide="chevron-right"></i> Details'}
    </button>
    ${open ? `<div class="log activity-log">${log.length ? log.map(esc).join("\n") : "No log output yet."}</div>` : ""}`;
}

function _activityRow(item, { pending = false, finished = false } = {}) {
  const job = item.job_id ? state.jobs[item.job_id] : null;
  const pct = Math.round(((job?.progress ?? item.progress) || 0) * 100);
  return `
    <div class="activity-row ${pending ? "activity-row-pending" : ""}"
         ${pending ? `draggable="true" data-id="${item.id}"` : ""}>
      <div class="row">
        ${pending ? '<span class="q-drag"><i data-lucide="grip-vertical"></i></span>' : ""}
        <span class="q-name">${esc(queueKindLabel(item.kind))}</span>
        ${finished ? `<span class="pill ${item.status}">${item.status}</span>` : ""}
        <span class="grow"></span>
        ${!finished ? `<button class="icon-btn danger" data-cancel="${item.id}" title="${pending ? "Remove" : "Cancel"}"><i data-lucide="x"></i></button>` : ""}
      </div>
      ${!pending ? `<div class="run-all-bar"><div class="run-all-fill" style="width:${pct}%"></div></div>` : ""}
      ${item.error ? `<div class="dim" style="color:var(--danger)">${esc(item.error)}</div>` : ""}
      ${!pending ? _logDisclosure(item, job) : ""}
    </div>`;
}

function renderActivityPopover() {
  const pop = $("#activity-popover");
  if (!pop || pop.hidden) return;
  const queue = state.queue || [];
  const running = queue.filter((i) => i.status === "running");
  const pending = queue.filter((i) => i.status === "pending");
  const recent = queue
    .filter((i) => i.status === "done" || i.status === "error" || i.status === "cancelled")
    .slice(-3)
    .reverse();

  pop.innerHTML = `
    <div class="activity-popover-inner">
      <div class="activity-section">
        <b class="activity-heading">Running</b>
        ${running.length ? running.map((i) => _activityRow(i)).join("")
          : '<div class="dim">Nothing running.</div>'}
      </div>

      <div class="activity-section">
        <div class="row"><b class="activity-heading">Pending</b>
          ${pending.length ? '<span class="dim">drag to reorder</span>' : ""}</div>
        <div id="activity-pending">
          ${pending.length ? pending.map((i) => _activityRow(i, { pending: true })).join("")
            : '<div class="dim">Nothing queued.</div>'}
        </div>
      </div>

      <div class="activity-section">
        <b class="activity-heading">Recent</b>
        ${recent.length ? recent.map((i) => _activityRow(i, { finished: true })).join("")
          : '<div class="dim">Nothing finished yet this session.</div>'}
      </div>
    </div>`;

  pop.querySelectorAll("[data-cancel]").forEach((el) => el.onclick = async () => {
    try {
      await api(`/projects/${state.pid}/queue/${el.dataset.cancel}`, { method: "DELETE" });
      await pollQueue();
    } catch (e) { alert(e.message); }
  });
  pop.querySelectorAll("[data-details]").forEach((el) => el.onclick = () => {
    const id = el.dataset.details;
    if (_activityExpanded.has(id)) _activityExpanded.delete(id); else _activityExpanded.add(id);
    renderActivityPopover();
  });
  _wireActivityDrag(pop.querySelector("#activity-pending"));

  // Fetch fresh job detail for every visible running item (and any
  // finished item whose log disclosure is open) so progress/log stay live.
  [...running, ...recent].forEach(async (i) => {
    if (!i.job_id) return;
    if (i.status !== "running" && !_activityExpanded.has(i.id) && state.jobs[i.job_id]) return;
    try { state.jobs[i.job_id] = await api(`/jobs/${i.job_id}`); }
    catch (_e) { /* transient -- next poll retries */ }
  });
  refreshIcons();
}

function _wireActivityDrag(list) {
  if (!list) return;
  list.querySelectorAll(".activity-row-pending").forEach((row) => {
    row.addEventListener("dragstart", () => row.classList.add("dragging"));
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    row.addEventListener("dragover", (e) => {
      e.preventDefault();
      const dragging = list.querySelector(".dragging");
      if (!dragging || dragging === row) return;
      const rect = row.getBoundingClientRect();
      const before = e.clientY - rect.top < rect.height / 2;
      list.insertBefore(dragging, before ? row : row.nextSibling);
    });
  });
  list.addEventListener("drop", async (e) => {
    e.preventDefault();
    const ids = [...list.querySelectorAll(".activity-row-pending")].map((r) => r.dataset.id);
    try {
      await api(`/projects/${state.pid}/queue/reorder`, { method: "POST", body: { ids } });
      await pollQueue();
    } catch (err) { alert(err.message); }
  });
}

window.ActivityPopover = {
  open: openActivityPopover,
  close: closeActivityPopover,
  toggle: toggleActivityPopover,
  isOpen: isActivityPopoverOpen,
  render: renderActivityPopover,
};
