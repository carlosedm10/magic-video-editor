/* Left-column media bin: clips grouped by camera_group with group headers,
   main badge / Set main, per-clip duration, Add files / Add folder.

   Import UX (docs/PLATFORM-SPEC.md v5.3 + the "app-first" PRINCIPLE):
   - pywebview native dialogs (pick_files/pick_folder, hardlink import via
     POST /clips) stay the PRIMARY path whenever window.pywebview.api exists.
   - Browser-mode fallback: hidden <input type=file multiple> / <input
     webkitdirectory> feed POST /api/projects/{pid}/upload (multipart,
     streamed to disk server-side), with a per-file progress bar rendered
     in the bin via XMLHttpRequest upload.onprogress.
   - Drag & drop from Finder onto the whole bin: dragover shows a dashed
     "Drop clips or a camera folder" overlay; dropped entries are traversed
     via webkitGetAsEntry so a folder's files carry their folder name as the
     first path segment in the uploaded filename -- the backend
     (magic_video_editor/api/projects.py clips_upload) already infers camera_group from
     that segment, exactly like the webkitdirectory picker does, so both
     paths share one upload helper.
   - The paste-path input is no longer primary UI: it starts hidden behind a
     tiny "add by path…" disclosure link for power users/devs.

   The static controls (buttons, hidden inputs, disclosure link) are wired
   once; render() only rebuilds the clip list itself, so in-flight uploads
   or an open path box never get clobbered by an unrelated refreshProject()
   tick. Upload progress lives in its own #media-bin-uploads container
   (created here, not in index.html) so it renders independently of the
   clip-list rebuild.

   Clips are HTML5-draggable (spec v4 §4 "Media bin: clips draggable INTO
   the timeline") — ui/editor/timeline.js is the drop target and inserts a
   full-clip segment at the drop index via Editor.insertClip(). The MIME
   type "application/x-mve-clip" (clip id as payload) is this app's own,
   private contract between the two files. */

window.EditorUI = window.EditorUI || {};

window.EditorUI.mediabin = {
  _wired: false,
  _uploads: new Map(),
  _dragDepth: 0,

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
                <i data-lucide="${c.role === "camera" ? "video" : "mic"}"></i></button>
              <button class="icon-btn danger" data-del="${c.id}" title="Remove"><i data-lucide="x"></i></button>
            </div>`;
          }).join("")}
        </div>`;
    }).join("") ||
      '<div class="dim" style="padding:8px 4px">No clips yet — add files or a folder above,'
      + ' or drop them here. A folder becomes one camera group.</div>';

    list.querySelectorAll(".bin-clip[draggable]").forEach((el) => {
      el.addEventListener("dragstart", (e) => {
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("application/x-mve-clip", el.dataset.clip);
        e.dataTransfer.setData("text/plain", el.dataset.clip);
      });
    });

    // Source mode (spec v7 §7.1): click OR double-click a clip -> plays its
    // preview proxy standalone in the main player (ui/editor/player.js owns
    // the actual mode switch/chrome). Ignores clicks on the row's own
    // buttons (role toggle, remove, set-main) so those keep working
    // unchanged. A dblclick fires a click first (browsers always do), so
    // enterSourceMode() is idempotent about re-entering the same clip.
    list.querySelectorAll(".bin-clip[data-clip]").forEach((el) => {
      const enter = (e) => {
        if (e.target.closest("button")) return;
        window.EditorUI?.player?.enterSourceMode?.(el.dataset.clip);
      };
      el.addEventListener("click", enter);
      el.addEventListener("dblclick", enter);
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
    refreshIcons();
  },

  _proxyTag(c) {
    if (!c.info || !c.info.has_video) return "";
    if (!("proxy" in c)) return '<span class="dim" title="Generating preview proxy…"><i data-lucide="loader-2" class="lucide-spin"></i></span>';
    if (c.proxy) return '<span class="pill dim" title="H.264 preview proxy ready">proxy</span>';
    return "";
  },

  _wireStatic() {
    this._wired = true;
    this._injectStyle();

    const bin = document.getElementById("media-bin");
    const addFiles = document.getElementById("add-files");
    const addFolder = document.getElementById("add-folder");
    const pathInput = document.getElementById("path-input");
    const list = document.getElementById("media-bin-list");

    // Transient upload-progress rows live in their own container so a
    // render() clip-list rebuild never wipes an in-flight upload's bar.
    const uploads = document.createElement("div");
    uploads.id = "media-bin-uploads";
    uploads.className = "mvebin-uploads";
    uploads.hidden = true;
    if (list?.parentElement) list.parentElement.insertBefore(uploads, list);

    // Hidden native-style inputs feeding the upload endpoint (browser-mode
    // fallback only — pywebview's real dialogs are tried first below).
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.multiple = true;
    fileInput.accept = "video/*,audio/*";
    fileInput.hidden = true;
    document.body.appendChild(fileInput);

    const folderInput = document.createElement("input");
    folderInput.type = "file";
    folderInput.multiple = true;
    folderInput.webkitdirectory = true;
    folderInput.directory = true;
    folderInput.hidden = true;
    document.body.appendChild(folderInput);

    fileInput.onchange = async () => {
      const files = Array.from(fileInput.files || []);
      fileInput.value = "";
      await Promise.all(files.map((f) => this._uploadOne(f, f.name)));
    };
    folderInput.onchange = async () => {
      const files = Array.from(folderInput.files || []);
      folderInput.value = "";
      await Promise.all(files.map((f) => this._uploadOne(f, f.webkitRelativePath || f.name)));
    };

    if (addFiles) addFiles.onclick = async () => {
      if (window.pywebview?.api?.pick_files) {
        const paths = await window.pywebview.api.pick_files();
        if (paths.length) await this._addPaths(paths);
      } else {
        fileInput.click();
      }
    };
    if (addFolder) addFolder.onclick = async () => {
      if (window.pywebview?.api?.pick_folder) {
        const paths = await window.pywebview.api.pick_folder();
        if (paths.length) await this._addPaths(paths);
      } else {
        folderInput.click();
      }
    };

    // "add by path…" disclosure — dev/power-user escape hatch, not primary UI.
    if (pathInput) {
      pathInput.hidden = true;
      pathInput.placeholder = "Paste absolute file/folder paths, comma separated, Enter";
      const link = document.createElement("a");
      link.href = "#";
      link.className = "mvebin-path-link dim";
      link.textContent = "add by path…";
      link.onclick = (e) => {
        e.preventDefault();
        pathInput.hidden = !pathInput.hidden;
        if (!pathInput.hidden) pathInput.focus();
      };
      pathInput.parentElement?.insertBefore(link, pathInput);

      pathInput.onkeydown = async (e) => {
        if (e.key !== "Enter") return;
        const paths = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
        if (!paths.length) return;
        await this._addPaths(paths);
        e.target.value = "";
      };
    }

    // Drag & drop from Finder onto the whole bin (spec v5.3 §1).
    if (bin) {
      if (getComputedStyle(bin).position === "static") bin.style.position = "relative";
      const overlay = document.createElement("div");
      overlay.className = "mvebin-dropzone";
      overlay.hidden = true;
      overlay.innerHTML = '<div class="mvebin-dropzone-inner">Drop clips or a camera folder</div>';
      bin.appendChild(overlay);

      const showOverlay = () => { overlay.hidden = false; };
      const hideOverlay = () => { overlay.hidden = true; };

      bin.addEventListener("dragenter", (e) => {
        if (!this._hasFiles(e)) return;
        e.preventDefault();
        this._dragDepth++;
        showOverlay();
      });
      bin.addEventListener("dragover", (e) => {
        if (!this._hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      });
      bin.addEventListener("dragleave", () => {
        this._dragDepth = Math.max(0, this._dragDepth - 1);
        if (!this._dragDepth) hideOverlay();
      });
      bin.addEventListener("drop", async (e) => {
        if (!this._hasFiles(e)) return;
        e.preventDefault();
        this._dragDepth = 0;
        hideOverlay();
        await this._handleDrop(e.dataTransfer);
      });
    }
  },

  _hasFiles(e) {
    return Array.from(e.dataTransfer?.types || []).includes("Files");
  },

  async _handleDrop(dt) {
    if (!state.pid) return;
    const items = dt.items ? Array.from(dt.items) : [];
    const entries = items
      .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
      .filter(Boolean);

    let pairs = [];
    if (entries.length) {
      const nested = await Promise.all(entries.map((e) => this._entryToFiles(e)));
      pairs = nested.flat();
    } else {
      // Fallback for browsers without the entries API — folder structure is
      // lost, files land in the default "main" group.
      pairs = Array.from(dt.files || []).map((f) => [f, f.name]);
    }
    if (!pairs.length) return;
    await Promise.all(pairs.map(([f, relPath]) => this._uploadOne(f, relPath)));
  },

  // Recursively resolves a DataTransferItem's FileSystemEntry into
  // [File, relativePath] pairs. entry.fullPath already carries the folder
  // name as its first segment (e.g. "/IMG_9232/clip.mp4"), which is exactly
  // the convention the upload endpoint uses to infer camera_group — so both
  // the drag&drop path and the webkitdirectory picker share one contract.
  async _entryToFiles(entry) {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      return [[file, entry.fullPath.replace(/^\//, "")]];
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const children = [];
      for (;;) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        children.push(...batch);
      }
      const nested = await Promise.all(children.map((c) => this._entryToFiles(c)));
      return nested.flat();
    }
    return [];
  },

  _uploadOne(file, relPath) {
    return new Promise((resolve) => {
      const id = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
      const rec = { name: file.name, relPath, progress: 0, done: false, error: false };
      this._uploads.set(id, rec);
      this._renderUploads();

      const fd = new FormData();
      fd.append("files", file, relPath);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/projects/${state.pid}/upload`);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        rec.progress = e.loaded / e.total;
        this._renderUploads();
      };
      const finish = async (ok) => {
        rec.done = ok;
        rec.error = !ok;
        rec.progress = 1;
        this._renderUploads();
        setTimeout(() => {
          this._uploads.delete(id);
          this._renderUploads();
        }, ok ? 1200 : 4000);
        if (ok) await refreshProject();
        resolve();
      };
      xhr.onload = () => finish(xhr.status >= 200 && xhr.status < 300);
      xhr.onerror = () => finish(false);
      xhr.send(fd);
    });
  },

  _renderUploads() {
    const el = document.getElementById("media-bin-uploads");
    if (!el) return;
    const items = [...this._uploads.values()];
    if (!items.length) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = items.map((u) => {
      const pct = Math.round((u.progress || 0) * 100);
      const state_ = u.error ? "error" : u.done ? "done" : "";
      return `
        <div class="bin-clip row mvebin-upload-row">
          <span class="bin-clip-name grow" title="${esc(u.relPath)}">${esc(u.name)}</span>
          <div class="run-all-bar mvebin-upload-bar">
            <div class="run-all-fill ${state_}" style="width:${u.error ? 100 : pct}%"></div>
          </div>
          <span class="dim mono mvebin-upload-pct">${u.error ? "!" : u.done ? "✓" : `${pct}%`}</span>
        </div>`;
    }).join("");
  },

  _injectStyle() {
    if (document.getElementById("mvebin-dnd-style")) return;
    const style = document.createElement("style");
    style.id = "mvebin-dnd-style";
    style.textContent = `
      .mvebin-uploads { flex-shrink: 0; margin-bottom: 6px; }
      .mvebin-upload-row { opacity: .9; }
      .mvebin-upload-bar { width: 64px; flex-shrink: 0; }
      .mvebin-upload-pct { min-width: 28px; text-align: right; }
      .mvebin-path-link { display: block; font-size: 11px; margin: -2px 0 6px 2px;
        text-decoration: none; cursor: pointer; }
      .mvebin-path-link:hover { color: var(--text); text-decoration: underline; }
      .mvebin-dropzone { position: absolute; inset: 0; z-index: 20;
        background: rgba(5, 7, 13, .85);
        border: 2px dashed var(--accent, #a01828); border-radius: 10px;
        display: flex; align-items: center; justify-content: center;
        text-align: center; pointer-events: none; }
      .mvebin-dropzone-inner { color: var(--text, #f5f6fa); font-weight: 600;
        padding: 0 16px; }
    `;
    document.head.appendChild(style);
  },

  async _addPaths(paths) {
    if (!state.pid || !paths.length) return;
    await api(`/projects/${state.pid}/clips`, { method: "POST", body: { paths } });
    await refreshProject();
  },
};
