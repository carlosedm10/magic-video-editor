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
   private contract between the two files.

   Main audio track (spec vNext "Main audio track", music bed with
   auto-ducking): a separate "+ Music" import path (Files picker via the
   pywebview dialog / hidden <input>, "add by path…", and Finder drag&drop,
   routed here by extension) accepts .mp3/.wav/.m4a into project
   ["audio_assets"] — a list wholly separate from project["clips"] (see
   pipeline/ingest.py's MUSIC_EXTS comment: the camera-clip pipeline never
   sees these). Rendered as its own "Audio" section below the camera-group
   list, draggable with the private MIME type "application/x-mve-audio"
   (asset id as payload) onto the timeline's main-audio lane
   (ui/editor/timeline.js's _ensureAudioTrack/renderAudioTrack), which PUTs
   project["audio_track"] via api/audio.py directly (no Editor/history
   integration — this is deliberately NOT routed through ui/editor/state.js,
   which this task doesn't own/touch). */

window.EditorUI = window.EditorUI || {};

window.EditorUI.mediabin = {
  _wired: false,
  _uploads: new Map(),
  _dragDepth: 0,

  render(project) {
    if (!this._wired) this._wireStatic();
    this._renderAudioAssets(project);
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
            const discard = this._discardInfo(c, project);
            const rowTitle = discard
              ? discard.tooltip
              : (draggable ? "Drag onto the timeline to append this clip" : "");
            return `
            <div class="bin-clip row${discard ? " bin-clip-discarded" : ""}" data-clip="${c.id}" ${draggable ? 'draggable="true"' : ""}
              title="${esc(rowTitle)}">
              <span class="bin-clip-name grow" title="${esc(c.filename)}">${esc(c.filename)}</span>
              <span class="dim mono">${c.info ? fmtT(c.info.duration) : "…"}</span>
              ${discard ? `<span class="pill discard-pill" title="${esc(discard.tooltip)}">${esc(discard.label)}</span>` : ""}
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
        const clip = project.clips.find((c) => c.id === el.dataset.clip);
        // Bug fix: a clip whose preview proxy is still generating (see
        // pipeline/ingest.py's make_proxy:*/analyze_clip:* queue jobs) has
        // nothing browser-safe for server.py's media_preview to stream yet
        // -- entering source mode used to hit a black player (silently
        // served the undecodable HEVC/10-bit original) or now a 425. Show a
        // quick hint instead of handing player.js a clip it can't play.
        if (this._proxyPending(clip)) {
          showToast(`Preview generating for "${clip.filename}"…`);
          return;
        }
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
      if (!await confirmModal("Remove this clip from the project?", { danger: true, okLabel: "Delete" })) return;
      await api(`/projects/${project.id}/clips/${el.dataset.del}`, { method: "DELETE" });
      refreshProject();
    });
    refreshIcons();
  },

  _proxyTag(c) {
    if (this._proxyPending(c)) return '<span class="dim" title="Generating preview proxy…"><i data-lucide="loader-2" class="lucide-spin"></i></span>';
    if (c.info && c.info.has_video && c.proxy) return '<span class="pill dim" title="H.264 preview proxy ready">proxy</span>';
    return "";
  },

  // True while a clip has video but no "proxy" key yet -- the
  // make_proxy:*/analyze_clip:* queue job (pipeline/ingest.py) hasn't run
  // (or finished) for it. Cleared to a normal state (proxy pill, or nothing
  // for an already browser-safe original) the moment the key appears, which
  // happens via the existing 2s queue poll -> refreshProject() once the
  // queue item settles (ui/core.js) -- no new poller needed here.
  _proxyPending(c) {
    return !!(c && c.info && c.info.has_video && !("proxy" in c));
  },

  /* ---------- discarded-clip flag (owner roadmap #5) ----------
     After a takes run, a camera clip whose sentences were ALL cut (nothing
     of it survived — every s.kept===false for that clip_id) is flagged so
     the user can spot it and delete it. Deliberately NOT topic-based: this
     only looks at pipeline/takes.py's own kept/reason verdicts on THIS
     clip's sentences — a clip that merely covers the same subject as
     another clip is never touched, only one whose content was actually cut
     as a repeat/blooper/dedup. Manually-excluded clips (reason set by the
     user via api/projects.py's kept toggle -> "excluded manually") are
     intentionally not auto-flagged as "blooper" since that was the user's
     own choice, not the pipeline's. */
  _discardInfo(clip, project) {
    const sentences = project?.sentences || [];
    if (!sentences.length) return null; // takes hasn't run yet -- never flag
    const own = sentences.filter((s) => s.clip_id === clip.id);
    if (!own.length) return null; // nothing to judge yet (e.g. audio-only clip)
    if (own.some((s) => s.kept)) return null; // at least one kept sentence -> not discarded

    const reasons = own.map((s) => (s.reason || "").toLowerCase());
    const isManual = (r) => r.includes("manual"); // "excluded manually"
    const isDup = (r) => r.includes("duplicad") || (r.includes("dedup")) ||
      (r.includes("duplicate") && r.includes("across clips"));
    const isBlooper = (r) => r.includes("blooper") || r.includes("repetici") ||
      r.includes("repeated") || r.includes("out-of-context") ||
      r.includes("restart") || r.includes("stuck take") || r.includes("fragment");

    if (reasons.every(isManual)) return null; // user's own call, not a pipeline verdict

    const autoReasons = reasons.filter((r) => !isManual(r));
    const dupCount = autoReasons.filter(isDup).length;
    const blooperCount = autoReasons.filter(isBlooper).length;
    if (!dupCount && !blooperCount) return null; // no auto-cut reasons recognized -- don't guess

    const label = dupCount > blooperCount ? "descartado — duplicado" : "descartado — blooper";
    const tooltip = "Todo su contenido se cortó como repetición/blooper — revísalo y bórralo si quieres.";
    return { label, tooltip };
  },

  /* ---------- main audio track: audio_assets section (spec vNext) ----------
     A separate list from project.clips/the camera-clip pipeline — see
     pipeline/ingest.py's MUSIC_EXTS comment. Its own injected container
     (#media-bin-audio, created once in _wireStatic) so this never gets
     clobbered by / doesn't have to duplicate the camera-group rebuild logic
     above. Draggable with the private "application/x-mve-audio" MIME (asset
     id payload) onto the timeline's main-audio lane. */
  _renderAudioAssets(project) {
    const el = document.getElementById("media-bin-audio");
    if (!el) return;
    const assets = project?.audio_assets || [];
    if (!assets.length) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = `
      <div class="bin-group-head row">
        <b>Audio</b>
        <span class="grow"></span>
        <span class="dim">${assets.length}</span>
      </div>
      ${assets.map((a) => `
        <div class="bin-clip row bin-audio-clip" data-audio="${a.id}" draggable="true"
          title="Drag onto the main audio lane in the timeline">
          <i data-lucide="music"></i>
          <span class="bin-clip-name grow" title="${esc(a.filename)}">${esc(a.filename)}</span>
          <span class="dim mono">${fmtT(a.duration || 0)}</span>
          <button class="icon-btn danger" data-del-audio="${a.id}" title="Remove"><i data-lucide="x"></i></button>
        </div>`).join("")}`;

    el.querySelectorAll(".bin-audio-clip").forEach((row) => {
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("application/x-mve-audio", row.dataset.audio);
        e.dataTransfer.setData("text/plain", row.dataset.audio);
      });
    });
    el.querySelectorAll("[data-del-audio]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        if (!await confirmModal("Remove this audio file from the project?", { danger: true, okLabel: "Delete" })) return;
        await api(`/projects/${project.id}/audio-assets/${btn.dataset.delAudio}`, { method: "DELETE" });
        refreshProject();
      };
    });
    refreshIcons();
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

    // Main audio track (spec vNext): its own injected container for the
    // "Audio" (audio_assets) section, placed above the camera-group list —
    // _renderAudioAssets owns its content, this module's clip-list rebuild
    // never touches it.
    const audioSection = document.createElement("div");
    audioSection.id = "media-bin-audio";
    audioSection.className = "bin-group";
    audioSection.hidden = true;
    if (list?.parentElement) list.parentElement.insertBefore(audioSection, list);

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

    // Music-bed import (spec vNext "Main audio track") — a dedicated input/
    // button, deliberately separate from the camera-clip fileInput above
    // (which also technically accepts audio/* for the pre-existing
    // role="audio" external-mic-sync clips): this one always lands in
    // project["audio_assets"], never project["clips"].
    const audioInput = document.createElement("input");
    audioInput.type = "file";
    audioInput.multiple = true;
    audioInput.accept = "audio/*,.mp3,.wav,.m4a";
    audioInput.hidden = true;
    document.body.appendChild(audioInput);

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
    audioInput.onchange = async () => {
      const files = Array.from(audioInput.files || []);
      audioInput.value = "";
      await Promise.all(files.map((f) => this._uploadAudioOne(f)));
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

    // "+ Music" button (music-bed import) — injected next to Files/Folder,
    // same DOM-injection pattern ui/editor/timeline.js already uses for
    // chrome ui/index.html (owned by another agent this phase) doesn't know
    // about yet.
    if (addFolder && !document.getElementById("add-audio")) {
      const addAudio = document.createElement("button");
      addAudio.id = "add-audio";
      addAudio.className = "btn small";
      addAudio.title = "Import a music bed (.mp3/.wav/.m4a) for the main audio track";
      addAudio.innerHTML = '<i data-lucide="music"></i> Music';
      addAudio.onclick = async () => {
        if (window.pywebview?.api?.pick_files) {
          const paths = (await window.pywebview.api.pick_files()).filter((p) => this._isMusicPath(p));
          if (paths.length) await this._addAudioPaths(paths);
        } else {
          audioInput.click();
        }
      };
      addFolder.parentElement?.insertBefore(addAudio, addFolder.nextSibling);
      refreshIcons();
    }

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
        const raw = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
        if (!raw.length) return;
        // Route by extension: .mp3/.wav/.m4a are music-bed imports
        // (audio_assets), everything else is the existing clip/folder path.
        const musicPaths = raw.filter((p) => this._isMusicPath(p));
        const clipPaths = raw.filter((p) => !this._isMusicPath(p));
        if (clipPaths.length) await this._addPaths(clipPaths);
        if (musicPaths.length) await this._addAudioPaths(musicPaths);
        e.target.value = "";
      };
    }

    // Drag & drop from Finder onto the whole bin (spec v5.3 §1).
    if (bin) {
      if (getComputedStyle(bin).position === "static") bin.style.position = "relative";
      const overlay = document.createElement("div");
      overlay.className = "mvebin-dropzone";
      overlay.hidden = true;
      overlay.innerHTML = '<div class="mvebin-dropzone-inner">Drop clips, a camera folder, or an audio file for the music bed</div>';
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
    // Route by extension (spec vNext "Main audio track"): a lone .mp3/.wav/
    // .m4a dropped onto the bin is treated as a music-bed import
    // (audio_assets), everything else keeps going through the existing
    // camera-clip upload path.
    await Promise.all(pairs.map(([f, relPath]) =>
      this._isMusicPath(relPath) ? this._uploadAudioOne(f) : this._uploadOne(f, relPath)
    ));
  },

  _isMusicPath(p) {
    return /\.(mp3|wav|m4a)$/i.test(p || "");
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

  // `endpoint` defaults to the camera-clip upload path; _uploadAudioOne below
  // reuses this same progress-bar plumbing against the music-bed endpoint
  // (spec vNext "Main audio track") instead of duplicating it.
  _uploadOne(file, relPath, endpoint) {
    return new Promise((resolve) => {
      const id = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
      const rec = { name: file.name, relPath, progress: 0, done: false, error: false };
      this._uploads.set(id, rec);
      this._renderUploads();

      const fd = new FormData();
      fd.append("files", file, relPath);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", endpoint || `/api/projects/${state.pid}/upload`);
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

  _uploadAudioOne(file) {
    return this._uploadOne(file, file.name, `/api/projects/${state.pid}/audio-assets/upload`);
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
      /* Draggable-to-timeline affordance (spec #4 "manual is first-class"):
         a plain grab cursor + hover cue so dragging a clip onto the timeline
         reads as an obvious, primary action, not a hidden feature. */
      .bin-clip[draggable="true"] { cursor: grab; }
      .bin-clip[draggable="true"]:active { cursor: grabbing; }
      .bin-clip[draggable="true"]:hover { box-shadow: inset 0 0 0 1px var(--border); }
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
      /* main audio track: audio_assets section (spec vNext), visually
         distinct from camera clips via an accented music icon. */
      #media-bin-audio { margin-bottom: 6px; }
      .bin-audio-clip { cursor: grab; }
      .bin-audio-clip > i[data-lucide="music"] { color: var(--accent2, #35c28f); flex-shrink: 0; }
      /* Discarded-clip flag (owner roadmap #5): every sentence of this clip
         was cut as a repeat/blooper/dedup -- dim the row + a small pill so
         it reads as "review me, nothing of this survived the cut" without
         hiding it or auto-deleting anything. */
      .bin-clip-discarded { opacity: .55; }
      .bin-clip-discarded .bin-clip-name { text-decoration: line-through; text-decoration-color: var(--border); }
      .discard-pill { background: rgba(160, 24, 40, .18); color: var(--accent, #a01828);
        border: 1px solid rgba(160, 24, 40, .35); white-space: nowrap; }
    `;
    document.head.appendChild(style);
  },

  async _addPaths(paths) {
    if (!state.pid || !paths.length) return;
    await api(`/projects/${state.pid}/clips`, { method: "POST", body: { paths } });
    await refreshProject();
  },

  async _addAudioPaths(paths) {
    if (!state.pid || !paths.length) return;
    await api(`/projects/${state.pid}/audio-assets`, { method: "POST", body: { paths } });
    await refreshProject();
  },
};
