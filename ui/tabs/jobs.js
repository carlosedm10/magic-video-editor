/* Queue view (spec v4 §2-3) — replaces the old Activity tab. Renders the
   per-project job queue (state.queue, kept fresh by core.js's global 2s
   poll — see ensureQueuePolling/pollQueue) as pending / running / recent
   done-or-error, plus the live per-job log feed below. Header actions
   enqueue a preview render, a final render, or a render of every reel.
   Pending items get a per-item cancel button and are drag-to-reorderable
   (drop posts the new order to /queue/reorder). */

const QUEUE_KIND_LABELS = {
  "run-all": "Run pipeline",
  preview_render: "Render preview",
  final_render: "Final render",
  thumbs: "Thumbnails",
};

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

function renderJobs() {
  const pane = $("#tab-jobs");
  if (!pane) return;
  const queue = state.queue || [];
  const pending = queue.filter((i) => i.status === "pending");
  const running = queue.filter((i) => i.status === "running");
  const recent = queue
    .filter((i) => i.status === "done" || i.status === "error" || i.status === "cancelled")
    .slice(-10)
    .reverse();

  pane.innerHTML = `
    <div class="row queue-actions">
      <button class="btn small" id="q-preview">▶ Render preview</button>
      <button class="btn small" id="q-final">⬇ Final render</button>
      <button class="btn small" id="q-reels">📱 Render all reels</button>
    </div>

    <div class="card">
      <b>Running</b>
      ${running.length ? running.map((i) => {
        const job = i.job_id ? state.jobs[i.job_id] : null;
        const pct = Math.round(((job?.progress ?? i.progress) || 0) * 100);
        return `<div class="q-row">
          <span class="q-name">${esc(queueKindLabel(i.kind))}</span>
          <div class="run-all-bar"><div class="run-all-fill" style="width:${pct}%"></div></div>
          <span class="run-all-pct">${pct}%</span>
        </div>`;
      }).join("") : '<div class="dim">Nothing running.</div>'}
    </div>

    <div class="card">
      <div class="row"><b>Pending</b><span class="dim">drag to reorder</span></div>
      <div id="q-pending">
        ${pending.length ? pending.map((i) => `
          <div class="q-row q-pending-row" draggable="true" data-id="${i.id}">
            <span class="q-drag">⠿</span>
            <span class="q-name">${esc(queueKindLabel(i.kind))}</span>
            <span class="grow"></span>
            <button class="icon-btn danger" data-cancel="${i.id}" title="Cancel">✕</button>
          </div>`).join("") : '<div class="dim">Nothing queued.</div>'}
      </div>
    </div>

    <div class="card">
      <b>Recent</b>
      ${recent.length ? recent.map((i) => `
        <div class="q-row">
          <span class="q-name">${esc(queueKindLabel(i.kind))}</span>
          <span class="pill ${i.status}">${i.status}</span>
          ${i.error ? `<span class="dim" style="color:var(--danger)">${esc(i.error)}</span>` : ""}
        </div>`).join("") : '<div class="dim">Nothing finished yet this session.</div>'}
    </div>

    <div class="card">
      <b>Job log</b>
      ${renderJobLogFeed()}
    </div>`;

  $("#q-preview").onclick = () => enqueueKind("preview_render");
  $("#q-final").onclick = () => enqueueKind("final_render");
  $("#q-reels").onclick = () => {
    (state.project?.reels || []).forEach((r) =>
      enqueueKind(`reel_render:${r.id}`, { reel_id: r.id }));
  };
  pane.querySelectorAll("[data-cancel]").forEach((el) => el.onclick = async () => {
    try {
      await api(`/projects/${state.pid}/queue/${el.dataset.cancel}`, { method: "DELETE" });
      await pollQueue();
      renderJobs();
    } catch (e) { alert(e.message); }
  });
  wireQueueDrag(pane.querySelector("#q-pending"));

  // This view's own 2s cadence (renderJobs is re-invoked by core.js's
  // global poll every time the Queue tab is the active one): fetch fresh
  // job detail for every running item so its log/progress stay live here,
  // beyond the run-all job core.js already tracks for the top progress strip.
  running.forEach(async (i) => {
    if (!i.job_id) return;
    try { state.jobs[i.job_id] = await api(`/jobs/${i.job_id}`); }
    catch (_e) { /* transient -- next poll retries */ }
  });
}

async function enqueueKind(kind, payload = {}) {
  try {
    await api(`/projects/${state.pid}/queue`, { method: "POST", body: { kind, payload } });
    await pollQueue();
    renderJobs();
  } catch (e) { alert(e.message); }
}

function wireQueueDrag(list) {
  if (!list) return;
  list.querySelectorAll(".q-pending-row").forEach((row) => {
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
    const ids = [...list.querySelectorAll(".q-pending-row")].map((r) => r.dataset.id);
    try {
      await api(`/projects/${state.pid}/queue/reorder`, { method: "POST", body: { ids } });
      await pollQueue();
    } catch (err) { alert(err.message); }
  });
}

function renderJobLogFeed() {
  const jobs = Object.values(state.jobs).sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  return jobs.length ? jobs.map((j) => `
    <div class="card" style="margin-top:8px">
      <div class="row"><b>${esc(j.name)}</b>
        <span class="pill">${j.status}</span>
        <span class="dim">${Math.round((j.progress || 0) * 100)}%</span></div>
      ${j.error ? `<div class="dim" style="color:var(--danger)">${esc(j.error)}</div>` : ""}
      <div class="log">${(j.log || []).map(esc).join("\n")}</div>
    </div>`).join("") : '<div class="dim">No job activity yet this session.</div>';
}

window.TABS.jobs = renderJobs;
