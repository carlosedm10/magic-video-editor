/* Projects Home (spec v5.2) — the landing view shown when no project is
   open. Owned entirely by this module: exposes window.HomeView =
   {mount(container), unmount()}. Routing (deciding WHEN to show this view,
   wiring a "home" button, calling mount/unmount) belongs to the integrator
   (core.js/index.html) — this file never touches either.

   Vanilla JS, no build step. Relies only on globals declared in core.js
   (api, esc, fmtT, selectProject) which — since core.js is a classic
   script, not a module — are visible here as long as core.js loads first.
   Falls back gracefully if any of them are somehow missing. */

(function () {
  const FILTERS = [
    ["all", "All"],
    ["todo", "To do"],
    ["in_progress", "In progress"],
    ["done", "Done"],
    ["uploaded", "Uploaded"],
  ];

  // Automatic processing_level values (cutroom/store.py processing_level)
  // -> display badge text + a css modifier class.
  const LEVEL_LABEL = {
    por_empezar: "Por empezar",
    en_proceso: "En proceso",
    finalizado: "Finalizado",
  };
  const LEVEL_CLASS = {
    por_empezar: "level-todo",
    en_proceso: "level-progress",
    finalizado: "level-done",
  };

  let root = null; // container passed to mount()
  let filter = "all";
  let projects = []; // enriched summaries (see loadProjects)
  let refreshTimer = null;
  let renaming = null; // project id currently being inline-renamed

  function fmtDuration(totalSeconds) {
    const s = Math.max(0, Math.round(totalSeconds || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m > 0) return `${m}m ${String(sec).padStart(2, "0")}s`;
    return `${sec}s`;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch (_e) {
      return iso;
    }
  }

  function icon(name, size) {
    return `<i data-lucide="${name}" style="width:${size || 16}px;height:${size || 16}px"></i>`;
  }

  function runLucide() {
    // core.js's window.refreshIcons is the one shared helper every render
    // path in the app calls (spec v5.5) -- fall back to calling
    // window.lucide directly in the (should-never-happen) case core.js
    // hasn't defined it yet, so this view never depends on load order.
    if (typeof window.refreshIcons === "function") { window.refreshIcons(); return; }
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      try { window.lucide.createIcons({ attrs: { width: 16, height: 16 } }); }
      catch (_e) { /* best-effort only */ }
    }
  }

  async function loadProjects() {
    const summaries = await api("/projects");
    // Summaries (store.list_projects) lack clip details -- fetch each full
    // project in parallel to get the poster clip + total footage duration.
    // Local project counts are small; this is a one-shot cost per mount /
    // refresh, not a per-frame one.
    const full = await Promise.all(
      summaries.map((p) => api(`/projects/${p.id}`).catch(() => null))
    );
    return summaries.map((p, i) => {
      const detail = full[i];
      const clips = detail?.clips || [];
      const totalDuration = clips.reduce((sum, c) => sum + (c.info?.duration || 0), 0);
      const posterClip = clips.find((c) => c.thumbs?.strip) || null;
      return { ...p, clips_list: clips, totalDuration, posterClipId: posterClip?.id || null };
    });
  }

  async function hydratePoster(card, pid, cid) {
    try {
      const meta = await api(`/projects/${pid}/thumbs/${cid}/meta`);
      const posterEl = card.querySelector(".home-poster");
      if (!posterEl || !meta?.count) return;
      // The filmstrip is a single-row sprite of `count` frames, each
      // frame_w x frame_h. Cropping to just the FIRST frame via CSS: give
      // the element the frame's aspect ratio and stretch the background so
      // its total width becomes count*100% of the element -- frame 0 then
      // exactly fills the element at background-position 0 0.
      posterEl.style.backgroundImage = `url(/api/projects/${pid}/thumbs/${cid}/strip)`;
      posterEl.style.backgroundSize = `${meta.count * 100}% 100%`;
      posterEl.style.backgroundPosition = "0 0";
      posterEl.style.backgroundRepeat = "no-repeat";
      posterEl.classList.add("has-poster");
    } catch (_e) {
      // No thumbs yet (not generated / clip has no video track) -- keep the
      // gradient placeholder, nothing to do.
    }
  }

  function cardHtml(p) {
    const level = p.processing_level || "por_empezar";
    const status = p.workflow_status || "todo";
    const isRenaming = renaming === p.id;
    return `
    <div class="home-card" data-pid="${p.id}">
      <div class="home-poster" data-pid="${p.id}">
        ${p.posterClipId ? "" : `<div class="home-poster-empty">${icon("film", 22)}</div>`}
        <span class="pill home-level ${LEVEL_CLASS[level] || ""}">${esc(LEVEL_LABEL[level] || level)}</span>
      </div>
      <div class="home-card-body">
        <div class="home-name-row">
          ${isRenaming
            ? `<input type="text" class="home-name-input" value="${esc(p.name)}" data-pid="${p.id}">`
            : `<b class="home-name" data-pid="${p.id}" title="Double-click to rename">${esc(p.name)}</b>`}
        </div>
        <div class="home-meta row dim">
          <span title="Clips">${icon("film", 13)} ${p.clips}</span>
          <span title="Total footage">${icon("clock", 13)} ${fmtDuration(p.totalDuration)}</span>
          <span title="Created">${icon("calendar", 13)} ${fmtDate(p.created_at)}</span>
        </div>
        <div class="home-card-footer row">
          <select class="home-status" data-pid="${p.id}" title="Workflow status">
            <option value="todo" ${status === "todo" ? "selected" : ""}>To do</option>
            <option value="in_progress" ${status === "in_progress" ? "selected" : ""}>In progress</option>
            <option value="done" ${status === "done" ? "selected" : ""}>Done</option>
            <option value="uploaded" ${status === "uploaded" ? "selected" : ""}>Uploaded</option>
          </select>
          <span class="grow"></span>
          <button class="icon-btn danger home-delete" data-pid="${p.id}" title="Delete project">${icon("trash-2", 15)}</button>
        </div>
      </div>
    </div>`;
  }

  function newProjectCardHtml() {
    return `
    <div class="home-card home-new" id="home-new-project">
      <div class="home-new-inner">
        <div class="home-new-icon">${icon("plus", 26)}</div>
        <div class="home-new-label">New project</div>
      </div>
    </div>`;
  }

  function render() {
    if (!root) return;
    const filtered = filter === "all" ? projects : projects.filter((p) => (p.workflow_status || "todo") === filter);
    root.innerHTML = `
      <div class="home-inner">
        <div class="home-head">
          <div class="home-title">✦ Projects</div>
          <div class="home-sub dim">Everything you're editing, in one place.</div>
        </div>
        <div class="home-filters row">
          ${FILTERS.map(([key, label]) => `<button class="tab home-filter ${filter === key ? "active" : ""}" data-filter="${key}">${esc(label)}</button>`).join("")}
        </div>
        <div class="home-grid">
          ${newProjectCardHtml()}
          ${filtered.map(cardHtml).join("")}
        </div>
        ${filtered.length === 0 ? `<div class="dim home-empty">No projects in this filter yet.</div>` : ""}
      </div>`;
    wireEvents();
    runLucide();
    // Kick off poster hydration for every card with a poster clip, after
    // the grid is in the DOM (async, non-blocking, best-effort).
    filtered.forEach((p) => {
      if (!p.posterClipId) return;
      const card = root.querySelector(`.home-card[data-pid="${p.id}"]`);
      if (card) hydratePoster(card, p.id, p.posterClipId);
    });
  }

  function wireEvents() {
    root.querySelectorAll(".home-filter").forEach((el) => {
      el.onclick = () => { filter = el.dataset.filter; render(); };
    });

    const newBtn = root.querySelector("#home-new-project");
    if (newBtn) newBtn.onclick = onNewProject;

    root.querySelectorAll(".home-card:not(.home-new)").forEach((card) => {
      card.addEventListener("click", (e) => {
        // Ignore clicks on interactive children -- only bare card area opens.
        if (e.target.closest(".home-name, .home-name-input, .home-status, .home-delete")) return;
        openProject(card.dataset.pid);
      });
    });

    root.querySelectorAll(".home-name").forEach((el) => {
      el.ondblclick = (e) => {
        e.stopPropagation();
        renaming = el.dataset.pid;
        render();
        const input = root.querySelector(".home-name-input");
        if (input) { input.focus(); input.select(); }
      };
    });

    root.querySelectorAll(".home-name-input").forEach((el) => {
      el.onclick = (e) => e.stopPropagation();
      el.onkeydown = async (e) => {
        if (e.key === "Enter") { el.blur(); }
        else if (e.key === "Escape") { renaming = null; render(); }
      };
      el.onblur = () => commitRename(el.dataset.pid, el.value);
    });

    root.querySelectorAll(".home-status").forEach((el) => {
      el.onclick = (e) => e.stopPropagation();
      el.onchange = () => setWorkflowStatus(el.dataset.pid, el.value);
    });

    root.querySelectorAll(".home-delete").forEach((el) => {
      el.onclick = (e) => { e.stopPropagation(); onDelete(el.dataset.pid); };
    });
  }

  async function commitRename(pid, value) {
    renaming = null;
    const name = (value || "").trim();
    const project = projects.find((p) => p.id === pid);
    if (!name || !project || name === project.name) { render(); return; }
    try {
      await api(`/projects/${pid}`, { method: "PATCH", body: { name } });
      project.name = name;
    } catch (e) {
      alert(e.message);
    }
    render();
  }

  async function setWorkflowStatus(pid, value) {
    const project = projects.find((p) => p.id === pid);
    const prev = project?.workflow_status;
    if (project) project.workflow_status = value; // optimistic
    render();
    try {
      await api(`/projects/${pid}`, { method: "PATCH", body: { workflow_status: value } });
    } catch (e) {
      if (project) project.workflow_status = prev;
      alert(e.message);
      render();
    }
  }

  async function onDelete(pid) {
    const project = projects.find((p) => p.id === pid);
    const ok = confirm(
      `Delete "${project?.name || "this project"}"?\n\n` +
      "This deletes transcripts and renders, not your original footage."
    );
    if (!ok) return;
    try {
      await api(`/projects/${pid}`, { method: "DELETE" });
      projects = projects.filter((p) => p.id !== pid);
      render();
    } catch (e) {
      alert(e.message);
    }
  }

  async function onNewProject() {
    const name = prompt("Project name:");
    if (!name) return;
    try {
      const p = await api("/projects", { method: "POST", body: { name } });
      if (typeof window.selectProject === "function") await window.selectProject(p.id);
    } catch (e) {
      alert(e.message);
    }
  }

  function openProject(pid) {
    if (typeof window.selectProject === "function") window.selectProject(pid);
  }

  async function refresh() {
    if (renaming) return; // don't clobber an in-progress inline rename
    try {
      projects = await loadProjects();
    } catch (e) {
      console.error("HomeView failed to load projects", e);
      if (root) root.innerHTML = `<div class="dim home-empty">Could not load projects — see console.</div>`;
      return;
    }
    render();
  }

  function mount(container) {
    root = container;
    // Self-contained positioning/scroll (own CSS class) regardless of
    // whatever bare container element the integrator hands us.
    root.classList.add("home-view");
    filter = "all";
    renaming = null;
    root.innerHTML = `<div class="dim home-empty">Loading projects…</div>`;
    refresh();
    // Light periodic refresh while the home view is open -- picks up
    // processing_level flips (queue busy -> done) without a manual reload.
    // Cleared on unmount; a plain project list fetch is cheap.
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(refresh, 8000);
  }

  function unmount() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    if (root) { root.innerHTML = ""; root.classList.remove("home-view"); }
    root = null;
    projects = [];
    renaming = null;
  }

  window.HomeView = { mount, unmount };
})();
