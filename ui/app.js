/* CutRoom UI — vanilla JS, no build step. Polls jobs, renders per-tab views. */

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

let state = { pid: null, project: null, tab: "files", jobs: {}, watching: new Set() };

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
  renderTab();
}

/* ---------- stage bar ---------- */

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

function watchJob(jid) {
  if (state.watching.has(jid)) return;
  state.watching.add(jid);
  const poll = async () => {
    const job = await api(`/jobs/${jid}`);
    state.jobs[jid] = job;
    if (state.tab === "jobs") renderTab();
    renderStageBar();
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
  ({ files: renderFiles, takes: renderTakes, edit: renderEdit,
     reels: renderReels, jobs: renderJobs }[state.tab])();
}

/* ---------- Files tab ---------- */

function renderFiles() {
  const p = state.project;
  const clips = p.clips.map((c) => `
    <div class="card row">
      <div class="grow">
        <div>${esc(c.filename)}
          ${c.is_main ? '<span class="pill main">main camera</span>' : ""}
          <span class="pill ${c.role}">${c.role}</span>
        </div>
        <div class="dim">${c.info ? `${c.info.duration.toFixed(0)}s · ${c.info.width}x${c.info.height} @ ${c.info.fps}fps` : "not ingested yet"}
          ${c.language ? ` · lang: ${c.language}` : ""}</div>
      </div>
      ${c.role === "camera" && !c.is_main ?
        `<button class="btn small" data-main="${c.id}">Set main</button>` : ""}
      <button class="btn small" data-role="${c.id}">${c.role === "camera" ? "→ audio" : "→ camera"}</button>
      <button class="btn small danger" data-del="${c.id}">Remove</button>
    </div>`).join("");

  $("#tab-files").innerHTML = `
    <div class="hint">Add your raw footage (and optionally a separate audio recording).
      Mark which camera is the main one, then run the pipeline stages in order (top right).</div>
    <div class="row" style="margin-bottom:14px">
      <button class="btn primary" id="add-files">＋ Add files…</button>
      <input type="text" id="path-input" class="grow" placeholder="…or paste absolute file paths, comma separated, and press Enter">
    </div>
    ${clips || '<div class="dim">No files yet.</div>'}`;

  $("#add-files").onclick = async () => {
    let paths = [];
    if (window.pywebview?.api?.pick_files) paths = await window.pywebview.api.pick_files();
    else { $("#path-input").focus(); return; }
    if (paths.length) await addPaths(paths);
  };
  $("#path-input").onkeydown = async (e) => {
    if (e.key === "Enter") await addPaths(e.target.value.split(",").map((s) => s.trim()).filter(Boolean));
  };
  document.querySelectorAll("[data-main]").forEach((el) => el.onclick = async () => {
    await api(`/projects/${p.id}/clips/${el.dataset.main}`, { method: "POST", body: { is_main: true } });
    refreshProject();
  });
  document.querySelectorAll("[data-role]").forEach((el) => el.onclick = async () => {
    const clip = p.clips.find((c) => c.id === el.dataset.role);
    await api(`/projects/${p.id}/clips/${clip.id}`, {
      method: "POST", body: { role: clip.role === "camera" ? "audio" : "camera" } });
    refreshProject();
  });
  document.querySelectorAll("[data-del]").forEach((el) => el.onclick = async () => {
    await api(`/projects/${p.id}/clips/${el.dataset.del}`, { method: "DELETE" });
    refreshProject();
  });
}

async function addPaths(paths) {
  await api(`/projects/${state.pid}/clips`, { method: "POST", body: { paths } });
  await refreshProject();
}

/* ---------- Takes tab ---------- */

function renderTakes() {
  const p = state.project;
  if (!p.sentences?.length) {
    $("#tab-takes").innerHTML = '<div class="dim">Run Transcribe + Takes first.</div>';
    return;
  }
  const byClip = {};
  p.sentences.forEach((s) => (byClip[s.clip_id] ??= []).push(s));
  $("#tab-takes").innerHTML = `
    <div class="hint">Every detected sentence. Grayed = cut (repeated take, fragment, or excluded by you).
      Amber border = part of a repeated-take group. Click a sentence to toggle keep/cut.</div>` +
    Object.entries(byClip).map(([cid, sents]) => {
      const clip = p.clips.find((c) => c.id === cid);
      return `<div class="card">
        <div class="row" style="margin-bottom:8px"><b>${esc(clip?.filename || cid)}</b>
          <span class="dim">${sents.filter((s) => s.kept).length}/${sents.length} kept</span></div>
        ${sents.map((s) => `
          <div class="sentence ${s.kept ? "" : "cut"} ${s.dup_group ? "dup" : ""}" data-sid="${s.id}">
            <span class="t">${fmtT(s.start)}–${fmtT(s.end)}</span>
            <span class="grow">${esc(s.text)}
              <div class="why">${esc(s.reason || s.why || "")} ${s.score != null ? `· score ${s.score}` : ""}</div>
            </span>
          </div>`).join("")}
      </div>`;
    }).join("");
  document.querySelectorAll(".sentence").forEach((el) => el.onclick = async () => {
    const s = p.sentences.find((x) => x.id === el.dataset.sid);
    await api(`/projects/${p.id}/sentences/${s.id}`, { method: "POST", body: { kept: !s.kept } });
    refreshProject();
  });
}

/* ---------- Edit tab ---------- */

function renderEdit() {
  const p = state.project;
  const order = p.clip_order?.length ? p.clip_order :
    p.clips.filter((c) => c.role === "camera").map((c) => c.id);
  const edl = p.edl_preview || [];
  const total = edl.reduce((acc, s) => acc + (s.end - s.start), 0);
  const renders = (p.renders || []).slice().reverse();

  $("#tab-edit").innerHTML = `
    <div class="hint">Clip order (${esc(p.order_notes || "not ordered yet")}). Drag to reorder, then re-run Render.</div>
    <div id="order-list">${order.map((cid) => {
      const c = p.clips.find((x) => x.id === cid);
      return `<div class="card row order-item" draggable="true" data-cid="${cid}">
        <span>⠿</span><b>${esc(c?.filename || cid)}</b></div>`;
    }).join("")}</div>
    <div class="card">
      <b>Edit decision list</b> <span class="dim">${edl.length} segments · ${fmtT(total)} total</span>
      ${edl.map((s) => {
        const c = p.clips.find((x) => x.id === s.clip_id);
        return `<div class="sentence"><span class="t">${fmtT(s.start)}–${fmtT(s.end)}</span>
          <span class="grow"><span class="dim">${esc(c?.filename)}</span> ${esc(s.text.slice(0, 140))}…</span></div>`;
      }).join("") || '<div class="dim">Empty — run Takes first.</div>'}
    </div>
    ${renders.map((r) => `<div class="card">
      <b>Main cut ${r.at}</b> <span class="dim">${r.duration}s · ${r.segments} segments</span>
      <video controls preload="metadata" src="/api/projects/${p.id}/media/file?path=${encodeURIComponent(r.path)}"></video>
    </div>`).join("")}`;

  // drag & drop ordering
  let dragEl = null;
  document.querySelectorAll(".order-item").forEach((el) => {
    el.ondragstart = () => (dragEl = el);
    el.ondragover = (e) => { e.preventDefault();
      const list = $("#order-list");
      const after = [...list.children].find((c) =>
        c !== dragEl && e.clientY < c.getBoundingClientRect().top + c.offsetHeight / 2);
      list.insertBefore(dragEl, after || null);
    };
    el.ondragend = async () => {
      const newOrder = [...document.querySelectorAll(".order-item")].map((c) => c.dataset.cid);
      await api(`/projects/${p.id}/order`, { method: "POST", body: { clip_order: newOrder } });
      refreshProject();
    };
  });
}

/* ---------- Reels tab ---------- */

function renderReels() {
  const p = state.project;
  if (!p.reels?.length) {
    $("#tab-reels").innerHTML = '<div class="dim">Run the Reels stage to get ~20 scored suggestions.</div>';
    return;
  }
  $("#tab-reels").innerHTML = `<div class="reel-grid">` + p.reels.map((r) => `
    <div class="card">
      <div><span class="score">${r.score}</span> · #${r.rank} <b>${esc(r.title)}</b></div>
      <div class="dim">${r.duration}s · hook ${r.hook} · standalone ${r.self_contained} · payoff ${r.payoff}</div>
      <div class="dim" style="margin:6px 0">${esc(r.text.slice(0, 160))}…</div>
      ${r.path
        ? `<video controls preload="metadata" src="/api/projects/${p.id}/media/file?path=${encodeURIComponent(r.path)}"></video>`
        : `<button class="btn primary small" data-reel="${r.id}">Render 9:16</button>`}
    </div>`).join("") + `</div>`;
  document.querySelectorAll("[data-reel]").forEach((el) => el.onclick = async () => {
    const { job } = await api(`/projects/${p.id}/reels/${el.dataset.reel}/render`, { method: "POST" });
    watchJob(job);
    setTab("jobs");
  });
}

/* ---------- Jobs tab ---------- */

function renderJobs() {
  const jobs = Object.values(state.jobs).sort((a, b) => b.started_at - a.started_at);
  $("#tab-jobs").innerHTML = jobs.length ? jobs.map((j) => `
    <div class="card">
      <div class="row"><b>${esc(j.name)}</b>
        <span class="pill">${j.status}</span>
        <span class="dim">${Math.round(j.progress * 100)}%</span></div>
      ${j.error ? `<div class="dim" style="color:var(--danger)">${esc(j.error)}</div>` : ""}
      <div class="log">${j.log.map(esc).join("\n")}</div>
    </div>`).join("") : '<div class="dim">No activity yet this session.</div>';
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
  const h = await api("/health");
  $("#health").innerHTML = `
    ffmpeg <span class="${h.ffmpeg ? "ok" : "bad"}">${h.ffmpeg ? "✓" : "missing"}</span> ·
    ollama <span class="${h.ollama ? "ok" : "bad"}">${h.ollama ? "✓" : "down"}</span><br>
    <span title="${esc(h.whisper)}">${esc(h.model)}</span>`;
  await loadProjects();
}
boot();
