/* Left-column media bin: clips grouped by camera_group with group headers,
   main badge / Set main, per-clip duration, Add files / Add folder (pywebview
   pick_files/pick_folder with a paste-path fallback). Replaces the old Files
   tab (docs/PLATFORM-SPEC.md v3, "Ingestion model: camera groups"). The
   static controls (buttons, paste-path input) are wired once; render() only
   rebuilds the clip list itself, so typing in the paste-path box or button
   state never gets clobbered by an unrelated refreshProject() tick.

   Clips are HTML5-draggable (spec v4 §4 "Media bin: clips draggable INTO
   the timeline") — ui/editor/timeline.js is the drop target and inserts a
   full-clip segment at the drop index via Editor.insertClip(). The MIME
   type "application/x-mve-clip" (clip id as payload) is this app's own,
   private contract between the two files. */

window.EditorUI = window.EditorUI || {};

window.EditorUI.mediabin = {
  _wired: false,

  render(project) {
    if (!this._wired) this._wireStatic();
    const list = document.getElementById("media-bin-list");
    if (!list || !project) return;

    const groups = {};
    for (const c of project.clips || []) {
      const g = c.camera_group || "main";
      (groups[g] = groups[g] || []).push(c);
    }
    const names = Object.keys(groups).sort((a, b) => {
      const aMain = groups[a].some((c) => c.is_main);
      const bMain = groups[b].some((c) => c.is_main);
      if (aMain !== bMain) return aMain ? -1 : 1;
      return a.localeCompare(b);
    });

    list.innerHTML = names.map((g) => {
      const clips = groups[g];
      const isMain = clips.some((c) => c.is_main);
      return `
        <div class="bin-group">
          <div class="bin-group-head row">
            <b>${esc(g)}</b>
            ${isMain ? '<span class="pill main">main</span>' :
              `<button class="btn small" data-main-group="${esc(g)}">Set main</button>`}
            <span class="grow"></span>
            <span class="dim">${clips.length}</span>
          </div>
          ${clips.map((c) => {
            const draggable = c.info?.duration > 0;
            return `
            <div class="bin-clip row" data-clip="${c.id}" ${draggable ? 'draggable="true"' : ""}
              title="${draggable ? "Drag onto the timeline to append this clip" : ""}">
              <span class="bin-clip-name grow" title="${esc(c.filename)}">${esc(c.filename)}</span>
              <span class="dim mono">${c.info ? fmtT(c.info.duration) : "…"}</span>
              ${this._proxyTag(c)}
              <button class="icon-btn" data-role="${c.id}"
                title="${c.role === "camera" ? "Switch to audio-only" : "Switch to camera"}">
                ${c.role === "camera" ? "🎥" : "🎙"}</button>
              <button class="icon-btn danger" data-del="${c.id}" title="Remove">✕</button>
            </div>`;
          }).join("")}
        </div>`;
    }).join("") ||
      '<div class="dim" style="padding:8px 4px">No clips yet — add files or a folder above.'
      + ' A folder becomes one camera group.</div>';

    list.querySelectorAll(".bin-clip[draggable]").forEach((el) => {
      el.addEventListener("dragstart", (e) => {
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("application/x-mve-clip", el.dataset.clip);
        e.dataTransfer.setData("text/plain", el.dataset.clip);
      });
    });

    list.querySelectorAll("[data-main-group]").forEach((el) => el.onclick = async () => {
      await api(`/projects/${project.id}/groups/${encodeURIComponent(el.dataset.mainGroup)}/main`, { method: "POST" });
      refreshProject();
    });
    list.querySelectorAll("[data-role]").forEach((el) => el.onclick = async () => {
      const clip = project.clips.find((c) => c.id === el.dataset.role);
      if (!clip) return;
      await api(`/projects/${project.id}/clips/${clip.id}`, {
        method: "POST", body: { role: clip.role === "camera" ? "audio" : "camera" },
      });
      refreshProject();
    });
    list.querySelectorAll("[data-del]").forEach((el) => el.onclick = async () => {
      if (!confirm("Remove this clip from the project?")) return;
      await api(`/projects/${project.id}/clips/${el.dataset.del}`, { method: "DELETE" });
      refreshProject();
    });
  },

  _proxyTag(c) {
    if (!c.info || !c.info.has_video) return "";
    if (!("proxy" in c)) return '<span class="dim" title="Generating preview proxy…">⏳</span>';
    if (c.proxy) return '<span class="pill dim" title="H.264 preview proxy ready">proxy</span>';
    return "";
  },

  _wireStatic() {
    this._wired = true;
    const addFiles = document.getElementById("add-files");
    const addFolder = document.getElementById("add-folder");
    const pathInput = document.getElementById("path-input");

    if (addFiles) addFiles.onclick = async () => {
      let paths = [];
      if (window.pywebview?.api?.pick_files) paths = await window.pywebview.api.pick_files();
      else { pathInput?.focus(); return; }
      if (paths.length) await this._addPaths(paths);
    };
    if (addFolder) addFolder.onclick = async () => {
      let paths = [];
      if (window.pywebview?.api?.pick_folder) paths = await window.pywebview.api.pick_folder();
      else {
        if (pathInput) pathInput.placeholder = "Paste an absolute FOLDER path and press Enter";
        pathInput?.focus();
        return;
      }
      if (paths.length) await this._addPaths(paths);
    };
    if (pathInput) pathInput.onkeydown = async (e) => {
      if (e.key !== "Enter") return;
      const paths = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!paths.length) return;
      await this._addPaths(paths);
      e.target.value = "";
    };
  },

  async _addPaths(paths) {
    if (!state.pid || !paths.length) return;
    await api(`/projects/${state.pid}/clips`, { method: "POST", body: { paths } });
    await refreshProject();
  },
};
