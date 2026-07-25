/* Settings — full-screen page (spec v4 section 5, redesigned per v5.10/
   v5.11/v5.12).
   Mounted by core.js into #tab-settings, which the drawer/shell is expected
   to size full-viewport (this module renders assuming the container it's
   given covers everything -- no drawer-width styling here).

   Left section nav: General | Brand | Models | Performance | About.
   - General (v5.10): export location as a friendly-path row ("~/Movies/…"),
     a "Change…" button that auto-saves immediately (native picker, no Save
     button anywhere in this card), a "Reveal in Finder" icon button, and a
     live structure-preview breadcrumb built from the actually-open project
     (state.project, exposed by core.js). Clicking the path switches it to
     an editable input (Enter saves, Esc cancels) for power users.
   - Brand: settings.brand_profile free-text textarea (spec v5 addendum "SEO
     copywriter + brand profile"), fed to the copywriter agent. Autosaves on
     blur with the shared "Saved ✓" toast (no Save button, per v5.10's
     "apply the same pattern to the rest of Settings").
   - Models (v5.11 + v5.12): a compact "Your models" two-column grid
     (default + per-task selects, descriptions as tooltips) fitting one
     screen, an "Installed" horizontal chip list, and ONE "Browse models"
     button that opens the model browser as an encapsulated modal
     (recommendation block + search + results + install progress). Below
     that, the visually distinct "Transcription — Whisper" box (folded in
     per v5.12) with a curated repo dropdown + explainer. Everything
     auto-saves on change.
   - Performance: max_parallel_ffmpeg / ffmpeg_threads / min_free_ram_gb,
     auto-saving on change.
   - About: corrected product name/branding + health. */

const TASK_INFO = [
  ["take_judge", "Take judging",
    "Scores retakes of the same line to pick the best one. Works fine on smaller/faster models."],
  ["transcript_cleaner", "Transcript cleanup",
    "Finds restarts, abandoned takes and filler to cut before dedup. Wants a bigger model to judge well."],
  ["clip_order", "Story ordering",
    "Decides the narrative order of clips. Wants a bigger model to judge well."],
  ["reel_scorer", "Reel scoring",
    "Scores candidate moments for short-form reels. Wants a bigger model to judge well."],
  ["reviewer", "Suggestions review",
    "Reads the full kept transcript and proposes non-destructive suggestions (redundant/off-topic/etc). Wants a bigger model to judge well."],
  ["dedup_judge", "Cross-clip dedup",
    "Judges whether two sentences from different clips say the same thing. Wants a bigger model to judge well."],
];

const SECTIONS = [
  ["general", "General"],
  ["brand", "Brand"],
  ["models", "Models"],
  ["performance", "Performance"],
  ["about", "About"],
];

const COMPAT_INFO = {
  great: { label: "Runs great", color: "var(--accent2)" },
  tight: { label: "Tight fit", color: "var(--warn)" },
  too_big: { label: "Too big for this Mac", color: "var(--danger)" },
};

// v5.12: curated mlx-community Whisper repos shown as a dropdown instead of
// a raw text input, plus the "Custom repo…" escape hatch.
const WHISPER_OPTIONS = [
  ["mlx-community/whisper-large-v3-turbo", "large-v3-turbo — Recommended (best speed/accuracy balance)"],
  ["mlx-community/whisper-large-v3", "large-v3 (highest accuracy, slower)"],
  ["mlx-community/whisper-medium", "medium"],
  ["mlx-community/whisper-small", "small (fastest, less precise)"],
];
const WHISPER_CUSTOM = "__custom__";

// Field bug follow-up (2026-07-25): whisper's per-clip auto language
// detection can misfire on one clip's first window and TRANSLATE instead
// of transcribing (fluent Spanish -> fluent English, not garbage). "Auto"
// keeps today's per-clip detection (+ pipeline/transcribe.py's
// majority-vote self-heal); any other code pins every clip's language,
// skipping auto-detect. Kept in sync with magic_video_editor/api/settings.py
// LANGUAGE_CODES and the "Idioma" per-project override in mediabin.js.
const LANGUAGE_OPTIONS = [
  ["auto", "Auto (detectar)"],
  ["es", "Español"],
  ["en", "English"],
  ["fr", "Français"],
  ["de", "Deutsch"],
  ["it", "Italiano"],
  ["pt", "Português"],
  ["ca", "Català"],
];

const _sfs = {
  section: "general",
  settings: null,
  models: [],
  modelsError: null,
  libQuery: "",
  libResults: null,
  libLive: null,
  libRamGb: null,
  libCurated: false,
  libError: null,
  recommendation: null,
  recommendationError: null,
  lazyTags: {}, // model name -> {loading, tags, live, error}
  pullJobs: {}, // model -> job dict (while installing)
  polling: new Set(),
  modelModalOpen: false,
  generalEditing: false,
};

function _injectStyleOnce() {
  if (document.getElementById("settings-fullscreen-style")) return;
  const style = document.createElement("style");
  style.id = "settings-fullscreen-style";
  style.textContent = `
    .sfs-root { display:flex; height:100%; min-height:100%; color:var(--text); }
    .sfs-nav {
      flex:0 0 220px; padding:32px 12px; border-right:1px solid var(--border);
      display:flex; flex-direction:column; gap:2px;
    }
    .sfs-nav-title {
      font-weight:700; font-size:20px; letter-spacing:-0.02em; padding:0 12px 20px;
    }
    .sfs-nav-btn {
      text-align:left; background:transparent; border:none; color:var(--dim);
      font:inherit; font-size:14px; padding:10px 12px; border-radius:10px; cursor:pointer;
    }
    .sfs-nav-btn:hover { background:var(--panel2); color:var(--text); }
    .sfs-nav-btn.active { background:var(--panel2); color:var(--text); font-weight:600;
      box-shadow: inset 3px 0 0 var(--accent); }
    .sfs-content { flex:1 1 auto; overflow-y:auto; padding:40px 56px 80px; position:relative; }
    .sfs-content-inner { max-width:720px; margin:0 auto; display:flex; flex-direction:column; gap:32px; }
    .sfs-h1 { font-size:26px; font-weight:700; letter-spacing:-0.02em; margin:0 0 4px; }
    .sfs-sub { color:var(--dim); font-size:14px; margin:0 0 8px; }
    .sfs-card { background:var(--panel); border:1px solid var(--border); border-radius:16px; padding:24px; }
    .sfs-field { margin:18px 0; }
    .sfs-field:first-child { margin-top:0; }
    .sfs-label { display:block; font-weight:600; font-size:13px; margin-bottom:6px; }
    .sfs-hint { color:var(--dim); font-size:12.5px; margin-top:6px; line-height:1.5; }
    .sfs-input, .sfs-select {
      background:var(--panel2); border:1px solid var(--border); color:var(--text);
      border-radius:10px; padding:9px 12px; width:100%; font:inherit; font-size:14px;
    }
    .sfs-row { display:flex; gap:10px; align-items:center; }
    .sfs-row .sfs-input { flex:1 1 auto; }
    .sfs-btn {
      background:var(--panel2); border:1px solid var(--border); color:var(--text);
      border-radius:10px; padding:9px 16px; font:inherit; font-size:13.5px; cursor:pointer;
      white-space:nowrap; display:inline-flex; align-items:center; gap:6px;
    }
    .sfs-btn:hover { border-color:var(--accent); }
    .sfs-btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
    .sfs-btn.primary:hover { background:var(--accent-hover); border-color:var(--accent-hover); }
    .sfs-btn:disabled { opacity:0.5; cursor:default; }
    .sfs-icon-btn {
      background:var(--panel2); border:1px solid var(--border); color:var(--dim);
      border-radius:10px; width:36px; height:36px; flex:0 0 auto; cursor:pointer;
      display:flex; align-items:center; justify-content:center;
    }
    .sfs-icon-btn:hover { border-color:var(--accent); color:var(--text); }
    .sfs-feedback { font-size:13px; color:var(--dim); }
    .sfs-guide { font-size:13px; color:var(--dim); line-height:1.6; margin:0 0 4px; }

    /* ---------- General: export location row (v5.10) ---------- */
    .sfs-export-row { display:flex; align-items:center; gap:14px; }
    .sfs-export-icon {
      flex:0 0 auto; width:40px; height:40px; border-radius:12px; background:var(--panel2);
      display:flex; align-items:center; justify-content:center; color:var(--dim);
    }
    .sfs-export-main { flex:1 1 auto; min-width:0; }
    .sfs-export-label { font-weight:600; font-size:13px; margin-bottom:4px; }
    .sfs-export-path {
      background:transparent; border:none; color:var(--text); font:inherit; font-size:14px;
      padding:2px 0; cursor:text; text-align:left; max-width:100%; overflow:hidden;
      text-overflow:ellipsis; white-space:nowrap; border-bottom:1px dashed transparent;
    }
    .sfs-export-path:hover { border-bottom-color:var(--border); color:var(--dim); }
    .sfs-export-input { font-size:14px; }
    .sfs-breadcrumb-card { padding:18px 24px; }
    .sfs-breadcrumb {
      display:flex; align-items:center; flex-wrap:wrap; gap:4px; margin-top:8px;
      font-size:13px; color:var(--dim);
    }
    .sfs-crumb { display:inline-flex; align-items:center; gap:5px; color:var(--text); }
    .sfs-crumb:last-child { color:var(--accent2); }
    .sfs-crumb svg { width:14px; height:14px; flex:0 0 auto; }
    .sfs-crumb-sep { width:13px; height:13px; color:var(--dim); flex:0 0 auto; }

    /* ---------- toast (auto-persist pattern, everywhere) ---------- */
    .sfs-toast {
      position:fixed; right:32px; bottom:28px; z-index:80;
      background:var(--panel); border:1px solid var(--border); color:var(--accent2);
      border-radius:10px; padding:10px 16px; font-size:13px; font-weight:600;
      box-shadow:0 8px 28px rgba(0,0,0,.35); opacity:0; transform:translateY(6px);
      transition:opacity .18s ease, transform .18s ease; pointer-events:none;
    }
    .sfs-toast.show { opacity:1; transform:translateY(0); }
    .sfs-toast.sfs-toast-error { color:var(--danger); }

    /* ---------- Models: compact grid + chips (v5.11) ---------- */
    .sfs-card-head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
    .sfs-2col { display:grid; grid-template-columns:1fr 1fr; gap:16px 24px; }
    .sfs-field-compact { display:flex; flex-direction:column; gap:6px; min-width:0; }
    .sfs-label-row { display:flex; align-items:center; gap:6px; font-weight:600; font-size:13px; }
    .sfs-info-icon { width:13px; height:13px; color:var(--dim); flex:0 0 auto; cursor:help; }
    .sfs-divider { border-top:1px solid var(--border); margin:22px 0 16px; }
    .sfs-chip-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .sfs-installed-chip {
      display:inline-flex; align-items:center; gap:8px; border:1px solid var(--border);
      border-radius:999px; padding:6px 8px 6px 14px; font-size:12.5px; background:var(--panel2);
    }
    .sfs-installed-chip-size { color:var(--dim); }
    .sfs-installed-chip-x {
      background:transparent; border:none; color:var(--dim); cursor:pointer; padding:2px;
      display:flex; align-items:center; justify-content:center; border-radius:50%;
    }
    .sfs-installed-chip-x:hover { color:var(--danger); background:var(--panel); }
    .sfs-installed-chip-x svg { width:13px; height:13px; }

    /* ---------- Transcription/Whisper box, folded into Models (v5.12) ---------- */
    .sfs-whisper-card { border-top:2px solid var(--accent2); }
    .sfs-whisper-head { display:flex; align-items:center; gap:12px; }
    .sfs-whisper-icon {
      flex:0 0 auto; width:34px; height:34px; border-radius:10px;
      background:var(--panel2); color:var(--accent2);
      display:flex; align-items:center; justify-content:center;
    }
    .sfs-whisper-title { font-weight:700; font-size:14.5px; }
    .sfs-whisper-explainer { line-height:1.6; }

    /* ---------- model browser modal (v5.11) ---------- */
    .sfs-modal-overlay {
      position:fixed; inset:0; z-index:70; background:rgba(2,3,7,.55);
      display:flex; align-items:center; justify-content:center; padding:24px;
    }
    .sfs-modal {
      width:min(720px, 100%); max-height:min(84vh, 820px); display:flex; flex-direction:column;
      background:var(--panel); border:1px solid var(--border); border-radius:18px;
      box-shadow:0 24px 60px rgba(0,0,0,.5); overflow:hidden;
    }
    .sfs-modal-head {
      display:flex; align-items:center; justify-content:space-between;
      padding:20px 24px; border-bottom:1px solid var(--border); flex:0 0 auto;
    }
    .sfs-modal-body { padding:20px 24px 28px; overflow-y:auto; flex:1 1 auto; }

    .sfs-lib-card {
      border:1px solid var(--border); border-radius:12px; padding:14px 16px; margin-top:12px;
      display:flex; flex-direction:column; gap:8px;
    }
    .sfs-lib-name { font-weight:700; font-size:14.5px; }
    .sfs-lib-desc { color:var(--dim); font-size:12.5px; }
    .sfs-tag-row { display:flex; flex-wrap:wrap; gap:8px; }
    .sfs-chip {
      display:inline-flex; align-items:center; gap:6px; border:1px solid var(--border);
      border-radius:999px; padding:5px 10px; font-size:12px; background:var(--panel2);
    }
    .sfs-chip-dot { width:7px; height:7px; border-radius:50%; }
    .sfs-reco-row { display:flex; gap:14px; margin-top:12px; }
    .sfs-reco-card {
      flex:1 1 0; border:1px solid var(--border); border-radius:12px; padding:16px;
      display:flex; flex-direction:column; gap:8px; background:var(--panel2);
    }
    .sfs-reco-kicker { font-size:11.5px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; color:var(--dim); }
    .sfs-reco-model { font-weight:700; font-size:15px; }
    .sfs-reco-why { font-size:12.5px; color:var(--dim); line-height:1.5; flex:1 1 auto; }
    .sfs-reco-meta { font-size:12px; color:var(--dim); }
    .sfs-section-heading { font-size:13px; font-weight:600; margin:4px 0 2px; color:var(--dim); }
    .sfs-progress { height:6px; border-radius:4px; background:var(--panel2); overflow:hidden; margin-top:4px; }
    .sfs-progress-fill { height:100%; background:var(--accent2); transition:width .2s; }

    .sfs-about-row { display:flex; justify-content:space-between; gap:16px; padding:8px 0; border-top:1px solid var(--border); font-size:13.5px; }
    .sfs-about-row:first-child { border-top:none; }
    .sfs-about-key { color:var(--dim); }
    .sfs-ok { color:var(--accent2); }
    .sfs-bad { color:var(--danger); }
    .sfs-textarea {
      background:var(--panel2); border:1px solid var(--border); color:var(--text);
      border-radius:10px; padding:12px 14px; width:100%; font:inherit; font-size:14px;
      line-height:1.5; resize:vertical; min-height:260px;
    }
    .sfs-textarea:focus { outline:none; border-color:var(--accent); }
    .sfs-charcount { color:var(--dim); font-size:12px; margin-top:6px; text-align:right; }
  `;
  document.head.appendChild(style);
}

/* ---------- shared "Saved ✓" toast (auto-persist pattern, everywhere) ---------- */

function _sfsToast(msg, isError) {
  const host = document.querySelector(".sfs-content") || document.body;
  const el = document.createElement("div");
  el.className = "sfs-toast" + (isError ? " sfs-toast-error" : "");
  el.textContent = msg;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 220);
  }, 1700);
}

async function renderSettings() {
  _injectStyleOnce();
  const pane = $("#tab-settings");
  pane.innerHTML = `<div class="sfs-root"><div class="sfs-content"><div class="sfs-content-inner">
    <div class="sfs-sub">Loading settings…</div></div></div></div>`;

  try {
    _sfs.settings = await api("/settings");
  } catch (e) {
    pane.innerHTML = `<div class="sfs-root"><div class="sfs-content"><div class="sfs-content-inner">
      <div class="sfs-card">Failed to load settings: ${esc(e.message)}</div>
    </div></div></div>`;
    return;
  }
  try {
    _sfs.models = await api("/ollama/models");
    _sfs.modelsError = null;
  } catch (e) {
    _sfs.modelsError = e.message;
  }
  try {
    _sfs.health = await api("/health");
  } catch (e) {
    _sfs.health = null;
  }

  _sfsRenderShell();
}

function _sfsRenderShell() {
  const pane = $("#tab-settings");
  pane.innerHTML = `
    <div class="sfs-root">
      <nav class="sfs-nav">
        <div class="sfs-nav-title">Settings</div>
        ${SECTIONS.map(([key, label]) => `
          <button class="sfs-nav-btn ${key === _sfs.section ? "active" : ""}" data-section="${key}">
            ${esc(label)}
          </button>`).join("")}
      </nav>
      <div class="sfs-content"><div class="sfs-content-inner" id="sfs-section"></div></div>
    </div>`;

  pane.querySelectorAll("[data-section]").forEach((btn) => {
    btn.onclick = () => {
      _sfs.section = btn.dataset.section;
      _sfs.generalEditing = false;
      _sfsRenderShell();
    };
  });

  const host = $("#sfs-section");
  if (_sfs.section === "general") _sfsRenderGeneral(host);
  else if (_sfs.section === "brand") _sfsRenderBrand(host);
  else if (_sfs.section === "models") _sfsRenderModels(host);
  else if (_sfs.section === "performance") _sfsRenderPerformance(host);
  else if (_sfs.section === "about") _sfsRenderAbout(host);

  // The model browser is a standalone overlay (survives section switches
  // while an install is in flight) -- keep it mounted/visible if it was
  // already open (e.g. renderTab() got called again by refreshProject()).
  if (_sfs.modelModalOpen) _sfsShowModelModal();
}

/* ---------- General (v5.10: friendly path, auto-save, breadcrumb) ---------- */

function _sfsFriendlyPath(p) {
  if (!p) return "";
  const m = p.match(/^\/Users\/[^/]+(\/.*)?$/);
  if (m) return "~" + (m[1] || "");
  return p;
}

function _sfsBasename(p) {
  if (!p) return "";
  const clean = p.replace(/\/+$/, "");
  const parts = clean.split("/");
  return parts[parts.length - 1] || clean;
}

function _sfsSanitizeStem(name) {
  const cleaned = (name || "").replace(/[/:\\]/g, "").replace(/\s+/g, " ").trim();
  return cleaned || "project";
}

function _sfsBreadcrumbCrumbs(exportDir) {
  const rootName = _sfsBasename(exportDir) || "Exports";
  const proj = state.project;
  let projName, leaf;
  if (proj) {
    projName = proj.name || "Untitled project";
    const reels = proj.reels || [];
    if (reels.length) {
      const latest = reels[reels.length - 1];
      leaf = `${_sfsSanitizeStem(latest.title || `Reel ${latest.rank ?? ""}`)}.mp4`;
    } else {
      leaf = `${_sfsSanitizeStem(projName)}.mp4`;
    }
  } else {
    projName = "<project name>";
    leaf = "<project name>.mp4";
  }
  return [
    { icon: "folder", label: rootName },
    { icon: "folder", label: projName },
    { icon: "file-video", label: leaf },
  ];
}

function _sfsBreadcrumbHtml(exportDir) {
  return _sfsBreadcrumbCrumbs(exportDir).map((c, i) => `
    ${i ? '<i data-lucide="chevron-right" class="sfs-crumb-sep"></i>' : ""}
    <span class="sfs-crumb"><i data-lucide="${c.icon}"></i>${esc(c.label)}</span>`).join("");
}

function _sfsRenderGeneral(host) {
  const s = _sfs.settings;
  const editing = _sfs.generalEditing;
  host.innerHTML = `
    <div>
      <div class="sfs-h1">General</div>
      <div class="sfs-sub">Where finished exports land.</div>
    </div>
    <div class="sfs-card">
      <div class="sfs-export-row">
        <div class="sfs-export-icon"><i data-lucide="folder"></i></div>
        <div class="sfs-export-main">
          <div class="sfs-export-label">Export location</div>
          ${editing
            ? `<input type="text" class="sfs-input sfs-export-input" id="sfs-export-input"
                 value="${esc(s.export_dir || "")}" />`
            : `<button class="sfs-export-path" id="sfs-export-path-btn"
                 title="Click to edit the path directly">${esc(_sfsFriendlyPath(s.export_dir))}</button>`}
        </div>
        <button class="sfs-btn" id="sfs-change-folder"><i data-lucide="folder-open"></i> Change…</button>
        <button class="sfs-icon-btn" id="sfs-reveal-folder" title="Reveal in Finder">
          <i data-lucide="external-link"></i>
        </button>
      </div>
      <div class="sfs-hint">Final renders and reels are written here, inside a folder per project.</div>
    </div>
    <div class="sfs-card sfs-breadcrumb-card">
      <div class="sfs-label" style="margin-bottom:0">Your exports</div>
      <div class="sfs-breadcrumb">${_sfsBreadcrumbHtml(s.export_dir)}</div>
    </div>`;

  const startEditing = () => {
    _sfs.generalEditing = true;
    _sfsRenderGeneral(host);
    refreshIcons();
    const input = $("#sfs-export-input");
    input?.focus();
    input?.select();
  };
  const stopEditing = () => {
    _sfs.generalEditing = false;
    _sfsRenderGeneral(host);
    refreshIcons();
  };
  const saveExportDir = async (path) => {
    if (!path || !path.trim()) { stopEditing(); return; }
    try {
      _sfs.settings = await api("/settings", { method: "PUT", body: { export_dir: path } });
      stopEditing();
      _sfsToast("Saved ✓");
    } catch (e) {
      _sfsToast(`Couldn't save: ${e.message}`, true);
    }
  };

  $("#sfs-export-path-btn")?.addEventListener("click", startEditing);

  const input = $("#sfs-export-input");
  if (input) {
    let cancelled = false;
    input.onkeydown = (e) => {
      // Stop propagation: this is an inline field-level Enter/Esc (save/
      // cancel just this edit), not the app-wide Escape handler in core.js
      // that closes the whole Settings overlay -- letting it bubble would
      // cancel-and-also-close instead of just cancel-in-place.
      if (e.key === "Enter") { e.stopPropagation(); cancelled = false; saveExportDir(input.value); }
      else if (e.key === "Escape") { e.stopPropagation(); cancelled = true; stopEditing(); }
    };
    input.onblur = () => { if (!cancelled) stopEditing(); };
  }

  $("#sfs-change-folder").onclick = async () => {
    const api_ = window.pywebview?.api;
    if (api_?.pick_folder) {
      try {
        const picked = await api_.pick_folder();
        const path = Array.isArray(picked) ? picked[0] : picked;
        if (path) await saveExportDir(path);
      } catch (e) {
        _sfsToast(`Folder picker failed: ${e.message}`, true);
      }
    } else {
      // Dev/browser fallback (never the primary design, per the app-first
      // principle) -- just drop into the inline editable path.
      startEditing();
    }
  };

  $("#sfs-reveal-folder").onclick = async () => {
    try {
      await api("/open-folder", { method: "POST", body: { path: s.export_dir } });
    } catch (e) {
      _sfsToast(`Couldn't open folder: ${e.message}`, true);
    }
  };
}

/* ---------- Brand ---------- */

const BRAND_PLACEHOLDER =
  `Channel: e.g. "Carlos builds things" (YouTube + TikTok/Reels/Shorts)
Audience: indie devs and makers who like behind-the-scenes build logs
Tone: direct, a little dry-humored, no hype/clickbait
Recurring links: youtube.com/@example, example.com
Recurring hashtags: #buildinpublic #indiedev #softwareengineering
CTA: "Subscribe for the next build" / "Full write-up on the blog, link below"`;

function _sfsRenderBrand(host) {
  const s = _sfs.settings;
  const value = s.brand_profile || "";
  host.innerHTML = `
    <div>
      <div class="sfs-h1">Brand</div>
      <div class="sfs-sub">Describe your brand once — the AI copywriter uses it to write
        titles, descriptions and hashtags for your reels and the main video.</div>
    </div>
    <div class="sfs-card">
      <div class="sfs-field">
        <label class="sfs-label">Brand profile</label>
        <textarea class="sfs-textarea" id="sfs-brand-profile"
          placeholder="${esc(BRAND_PLACEHOLDER)}">${esc(value)}</textarea>
        <div class="sfs-charcount" id="sfs-brand-charcount">${value.length} characters</div>
        <div class="sfs-hint">Free-form plain text: your channel/handle, target audience, tone of
          voice, recurring links, hashtags you always use, and your usual call-to-action. It's
          passed as-is to the copywriter agent when it writes reel and video titles/descriptions.
          Saves automatically when you click away.</div>
      </div>
    </div>`;

  const textarea = $("#sfs-brand-profile");
  const charcount = $("#sfs-brand-charcount");
  textarea.oninput = () => {
    charcount.textContent = `${textarea.value.length} characters`;
  };
  textarea.onblur = () => _sfsSaveBrand();
}

async function _sfsSaveBrand() {
  const textarea = $("#sfs-brand-profile");
  if (!textarea) return;
  const value = textarea.value;
  if (_sfs.settings && (_sfs.settings.brand_profile || "") === value) return;
  try {
    _sfs.settings = await api("/settings", { method: "PUT", body: { brand_profile: value } });
    _sfsToast("Saved ✓");
  } catch (e) {
    _sfsToast(`Couldn't save: ${e.message}`, true);
  }
}

/* ---------- Models (v5.11 restructure + v5.12 Whisper box) ---------- */

function _sfsModelOptions(selected) {
  const opts = _sfs.models.map((m) =>
    `<option value="${esc(m.name)}" ${m.name === selected ? "selected" : ""}>
      ${esc(m.name)} (${m.size_gb}GB)</option>`);
  if (selected && !_sfs.models.some((m) => m.name === selected)) {
    opts.unshift(`<option value="${esc(selected)}" selected>${esc(selected)} (not pulled)</option>`);
  }
  return opts.join("");
}

function _sfsTaskOptions(selected) {
  const nullSel = selected == null ? "selected" : "";
  return `<option value="" ${nullSel}>(use default)</option>` + _sfsModelOptions(selected || "");
}

// Ollama mode -> one-line status shown at the top of Models (field-bug
// follow-up: the packaged app used to never spawn its bundled Ollama at all
// when the system one wasn't running -- this makes which path is actually
// serving requests visible instead of silently invisible).
const _OLLAMA_MODE_LABELS = {
  system: "Ollama: usando instalación del sistema",
  bundled: "Ollama: integrado",
  downloaded: "Ollama: descargado automáticamente",
  starting: "Ollama: iniciando…",
  downloading: "Ollama: descargando runtime…",
  unreachable: "Ollama: no disponible",
};

function _sfsOllamaStatusLine() {
  const mode = _sfs.health && _sfs.health.ollama_mode;
  if (!mode) return "";
  const label = _OLLAMA_MODE_LABELS[mode] || `Ollama: ${mode}`;
  const busy = mode === "starting" || mode === "downloading";
  const bad = mode === "unreachable";
  const cls = bad ? "sfs-bad" : busy ? "" : "sfs-ok";
  return `<div class="sfs-hint ${cls}" style="margin-bottom:12px">${esc(label)}</div>`;
}

function _sfsInstalledChipsHtml() {
  if (_sfs.modelsError) return `<div class="sfs-hint">Unavailable — Ollama isn't reachable.</div>`;
  if (!_sfs.models.length) return `<div class="sfs-hint">No models installed yet.</div>`;
  return _sfs.models.map((m) => `
    <span class="sfs-installed-chip">
      <span>${esc(m.name)}</span>
      <span class="sfs-installed-chip-size">${m.size_gb}GB</span>
      <button class="sfs-installed-chip-x" data-delete-model="${esc(m.name)}" title="Delete">
        <i data-lucide="x"></i>
      </button>
    </span>`).join("");
}

function _sfsWhisperCurrentValue(s) {
  return WHISPER_OPTIONS.some(([id]) => id === s.whisper_model) ? s.whisper_model : WHISPER_CUSTOM;
}

function _sfsRenderModels(host) {
  const s = _sfs.settings;
  const whisperSel = _sfsWhisperCurrentValue(s);
  const whisperCustomVisible = whisperSel === WHISPER_CUSTOM;
  host.innerHTML = `
    <div>
      <div class="sfs-h1">Models</div>
      <div class="sfs-sub">Pick the default Ollama model, override it per task, and manage what's installed.</div>
    </div>

    <div class="sfs-card">
      ${_sfsOllamaStatusLine()}
      ${_sfs.modelsError ? `<div class="sfs-hint" style="color:var(--warn)">
        Couldn't reach Ollama: ${esc(_sfs.modelsError)}</div>` : ""}
      <div class="sfs-label" style="margin-bottom:12px">Your models</div>
      <div class="sfs-2col">
        <div class="sfs-field-compact" style="grid-column:1 / -1">
          <label class="sfs-label-row"><span>Default model</span></label>
          <select class="sfs-select" id="s-default-model">${_sfsModelOptions(s.default_model)}</select>
        </div>
        ${TASK_INFO.map(([key, label, desc]) => `
          <div class="sfs-field-compact">
            <label class="sfs-label-row">
              <span>${esc(label)}</span>
              <i data-lucide="info" class="sfs-info-icon" title="${esc(desc)}"></i>
            </label>
            <select class="sfs-select" id="s-task-${key}" data-task="${key}">
              ${_sfsTaskOptions(s.task_models[key])}
            </select>
          </div>`).join("")}
      </div>

      <div class="sfs-divider"></div>
      <div class="sfs-card-head">
        <div class="sfs-label" style="margin:0">Installed</div>
        <button class="sfs-btn primary" id="sfs-browse-models">
          <i data-lucide="search"></i> Browse models
        </button>
      </div>
      <div class="sfs-chip-list" id="sfs-installed-chips">${_sfsInstalledChipsHtml()}</div>
    </div>

    <div class="sfs-card sfs-whisper-card">
      <div class="sfs-whisper-head">
        <div class="sfs-whisper-icon"><i data-lucide="mic"></i></div>
        <div class="sfs-whisper-title">Transcription — Whisper</div>
      </div>
      <div class="sfs-field">
        <label class="sfs-label">Whisper model</label>
        <select class="sfs-select" id="s-whisper-select">
          ${WHISPER_OPTIONS.map(([id, label]) =>
            `<option value="${esc(id)}" ${whisperSel === id ? "selected" : ""}>${esc(label)}</option>`).join("")}
          <option value="${WHISPER_CUSTOM}" ${whisperCustomVisible ? "selected" : ""}>Custom repo…</option>
        </select>
        <input type="text" class="sfs-input" id="s-whisper-custom" style="margin-top:8px; ${whisperCustomVisible ? "" : "display:none"}"
          value="${esc(whisperCustomVisible ? (s.whisper_model || "") : "")}"
          placeholder="mlx-community/whisper-..." />
      </div>
      <div class="sfs-field">
        <label class="sfs-label">Idioma del contenido</label>
        <select class="sfs-select" id="s-transcription-language">
          ${LANGUAGE_OPTIONS.map(([code, label]) =>
            `<option value="${code}" ${s.transcription_language === code ? "selected" : ""}>${esc(label)}</option>`).join("")}
        </select>
        <div class="sfs-hint">Auto detecta el idioma clip a clip -- rara vez falla, pero un
          único clip mal detectado puede salir TRADUCIDO a otro idioma en vez de transcrito
          (Whisper traduce cuando confunde el idioma). Fijar un idioma aquí lo aplica a todos
          los clips de todos los proyectos que no tengan su propio "Idioma" (bandeja de medios).</div>
      </div>
      <div class="sfs-hint sfs-whisper-explainer">
        Speech-to-text uses Whisper — the open-source standard for transcription — not an
        Ollama LLM. It produces the word-level timestamps every edit decision depends on, and
        runs on the Apple GPU via MLX. The Ollama models above only reason over the resulting text.
      </div>
    </div>`;

  $("#s-default-model").onchange = _sfsSaveModels;
  TASK_INFO.forEach(([key]) => { $(`#s-task-${key}`).onchange = _sfsSaveModels; });

  $("#sfs-browse-models").onclick = _sfsOpenModelModal;
  _sfsAttachDeleteHandlers();

  const whisperSelect = $("#s-whisper-select");
  const whisperCustom = $("#s-whisper-custom");
  whisperSelect.onchange = async () => {
    const v = whisperSelect.value;
    if (v === WHISPER_CUSTOM) {
      whisperCustom.style.display = "";
      whisperCustom.focus();
      return;
    }
    whisperCustom.style.display = "none";
    await _sfsSaveWhisper(v);
  };
  whisperCustom.onkeydown = (e) => { if (e.key === "Enter") whisperCustom.blur(); };
  whisperCustom.onblur = () => _sfsSaveWhisper(whisperCustom.value);

  $("#s-transcription-language").onchange = (e) => _sfsSaveTranscriptionLanguage(e.target.value);
}

async function _sfsSaveModels() {
  const task_models = {};
  TASK_INFO.forEach(([key]) => {
    const el = $(`#s-task-${key}`);
    if (el) task_models[key] = el.value === "" ? null : el.value;
  });
  try {
    _sfs.settings = await api("/settings", {
      method: "PUT",
      body: { default_model: $("#s-default-model").value, task_models },
    });
    _sfsToast("Saved ✓");
  } catch (e) {
    _sfsToast(`Couldn't save: ${e.message}`, true);
  }
}

async function _sfsSaveWhisper(value) {
  if (!value || !value.trim()) return;
  if (_sfs.settings && _sfs.settings.whisper_model === value.trim()) return;
  try {
    _sfs.settings = await api("/settings", { method: "PUT", body: { whisper_model: value.trim() } });
    _sfsToast("Saved ✓");
  } catch (e) {
    _sfsToast(`Couldn't save: ${e.message}`, true);
  }
}

async function _sfsSaveTranscriptionLanguage(value) {
  if (_sfs.settings && _sfs.settings.transcription_language === value) return;
  try {
    _sfs.settings = await api("/settings", { method: "PUT", body: { transcription_language: value } });
    _sfsToast("Saved ✓");
  } catch (e) {
    _sfsToast(`Couldn't save: ${e.message}`, true);
  }
}

function _sfsAttachDeleteHandlers() {
  document.querySelectorAll("[data-delete-model]").forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.deleteModel;
      if (!confirm(`Delete "${name}" from Ollama?`)) return;
      btn.disabled = true;
      try {
        await api(`/ollama/models/${encodeURIComponent(name)}`, { method: "DELETE" });
        _sfs.models = await api("/ollama/models");
        _sfsRefreshInstalledUI();
      } catch (e) {
        _sfsToast(`Delete failed: ${e.message}`, true);
        btn.disabled = false;
      }
    };
  });
}

function _sfsRefreshInstalledUI() {
  if (_sfs.section === "models") {
    const list = $("#sfs-installed-chips");
    if (list) { list.innerHTML = _sfsInstalledChipsHtml(); refreshIcons(); }
    _sfsAttachDeleteHandlers();
  }
}

/* ---------- Model browser modal (v5.11) ----------
   Encapsulated dialog: hardware recommendation + search + results + install
   progress. Lives as a standalone overlay outside #tab-settings so closing
   it (or switching Settings section) never interrupts an in-flight pull --
   polling in _sfsWatchPullJob keeps running regardless of _sfs.modelModalOpen. */

function _sfsEnsureModelModal() {
  if (document.getElementById("sfs-model-modal")) return;
  const el = document.createElement("div");
  el.id = "sfs-model-modal";
  el.className = "sfs-modal-overlay";
  el.hidden = true;
  el.innerHTML = `
    <div class="sfs-modal">
      <div class="sfs-modal-head">
        <div class="sfs-h1" style="margin:0; font-size:20px">Browse models</div>
        <button class="sfs-icon-btn" id="sfs-model-modal-close" title="Close">
          <i data-lucide="x"></i>
        </button>
      </div>
      <div class="sfs-modal-body">
        <div class="sfs-guide">Models run 100% locally via Ollama — nothing leaves this Mac.
          Search below, check the fit for your machine's RAM, and install.</div>
        <div id="sfs-reco-block" style="margin-top:14px"></div>
        <div class="sfs-row" style="margin-top:18px">
          <input type="text" class="sfs-input" id="sfs-lib-search" placeholder="Search models… (e.g. qwen, llama, gemma)" />
          <button class="sfs-btn" id="sfs-lib-search-btn">Search</button>
        </div>
        <div id="sfs-lib-results" style="margin-top:6px"></div>
      </div>
    </div>`;
  document.body.appendChild(el);

  $("#sfs-model-modal-close").onclick = _sfsCloseModelModal;
  el.addEventListener("click", (e) => { if (e.target.id === "sfs-model-modal") _sfsCloseModelModal(); });
  $("#sfs-lib-search-btn").onclick = () => _sfsSearchLibrary($("#sfs-lib-search").value);
  $("#sfs-lib-search").onkeydown = (e) => { if (e.key === "Enter") _sfsSearchLibrary(e.target.value); };
}

function _sfsShowModelModal() {
  _sfsEnsureModelModal();
  $("#sfs-model-modal").hidden = false;
  $("#sfs-lib-search").value = _sfs.libQuery;
  if (_sfs.libResults !== null) _sfsRenderLibraryResults();
  else _sfsSearchLibrary("");
  if (_sfs.recommendation !== null || _sfs.recommendationError) _sfsAttachRecommendationHandlers();
  else _sfsLoadRecommendation();
  refreshIcons();
}

function _sfsOpenModelModal() {
  _sfs.modelModalOpen = true;
  _sfsShowModelModal();
}

function _sfsCloseModelModal() {
  _sfs.modelModalOpen = false;
  const el = document.getElementById("sfs-model-modal");
  if (el) el.hidden = true;
}

/* ---------- Recommendation block ---------- */

function _sfsIsInstalled(modelRef) {
  return _sfs.models.some((m) => m.name === modelRef);
}

function _sfsRecoCardHtml(kicker, pick) {
  if (!pick) return "";
  const modelRef = pick.model;
  const installed = _sfsIsInstalled(modelRef);
  const job = _sfs.pullJobs[modelRef];
  const installing = job && job.status === "running";
  const pct = job ? Math.round((job.progress || 0) * 100) : 0;
  return `
    <div class="sfs-reco-card">
      <div class="sfs-reco-kicker">${esc(kicker)}</div>
      <div class="sfs-reco-model">${esc(modelRef)}</div>
      <div class="sfs-reco-meta">${pick.size_gb != null ? pick.size_gb + "GB" : ""}</div>
      <div class="sfs-reco-why">${esc(pick.why || "")}</div>
      <button class="sfs-btn ${installed ? "" : "primary"}" style="align-self:flex-start"
        data-pull-model="${esc(modelRef)}" ${installed || installing ? "disabled" : ""}>
        ${installed ? "Installed ✓" : (installing ? `${pct}%` : "Install")}
      </button>
      ${installing ? `<div class="sfs-progress"><div class="sfs-progress-fill" style="width:${pct}%"></div></div>` : ""}
    </div>`;
}

function _sfsRecommendationHtml() {
  if (_sfs.recommendationError) return "";
  if (!_sfs.recommendation) return `<div class="sfs-hint">Checking your Mac's hardware…</div>`;
  const r = _sfs.recommendation;
  return `
    <div class="sfs-guide" style="margin:0 0 2px">Tu Mac: <strong>${esc(r.chip)}</strong>, ${r.ram_gb}GB</div>
    <div class="sfs-reco-row">
      ${_sfsRecoCardHtml("Best overall", r.best_overall)}
      ${_sfsRecoCardHtml("Optimal para este Mac", r.optimal)}
    </div>`;
}

async function _sfsLoadRecommendation() {
  try {
    _sfs.recommendation = await api("/ollama/recommendation");
    _sfs.recommendationError = null;
  } catch (e) {
    _sfs.recommendation = null;
    _sfs.recommendationError = e.message;
  }
  const box = $("#sfs-reco-block");
  if (box) box.innerHTML = _sfsRecommendationHtml();
  _sfsAttachRecommendationHandlers();
  refreshIcons();
}

function _sfsAttachRecommendationHandlers() {
  const box = $("#sfs-reco-block");
  if (!box) return;
  box.querySelectorAll("[data-pull-model]").forEach((btn) => {
    btn.onclick = () => _sfsPullModel(btn.dataset.pullModel, () => {
      const b = $("#sfs-reco-block");
      if (b) b.innerHTML = _sfsRecommendationHtml();
      _sfsAttachRecommendationHandlers();
      refreshIcons();
    });
  });
}

async function _sfsSearchLibrary(q) {
  _sfs.libQuery = q;
  const box = $("#sfs-lib-results");
  if (box) box.innerHTML = `<div class="sfs-hint">Searching…</div>`;
  try {
    const res = await api(`/ollama/library?q=${encodeURIComponent(q)}`);
    _sfs.libResults = res.models;
    _sfs.libLive = res.live;
    _sfs.libRamGb = res.ram_gb;
    _sfs.libCurated = !!res.curated;
    _sfs.libError = null;
  } catch (e) {
    _sfs.libResults = [];
    _sfs.libError = e.message;
  }
  _sfsRenderLibraryResults();
}

function _sfsRenderLibraryResults() {
  const box = $("#sfs-lib-results");
  if (!box) return;
  if (_sfs.libError) {
    box.innerHTML = `<div class="sfs-hint" style="color:var(--warn)">Search failed: ${esc(_sfs.libError)}</div>`;
    return;
  }
  const notice = _sfs.libLive === false
    ? `<div class="sfs-hint" style="color:var(--warn)">Showing the offline curated catalog (couldn't reach ollama.com).</div>`
    : "";
  const heading = (_sfs.libCurated && !_sfs.libQuery)
    ? `<div class="sfs-section-heading">Populares</div>` : "";
  if (!_sfs.libResults.length) {
    box.innerHTML = notice + heading + `<div class="sfs-hint">No matches.</div>`;
    return;
  }
  box.innerHTML = notice + heading + _sfs.libResults.map((m) => {
    const lazy = _sfs.lazyTags[m.name];
    const tags = (lazy && lazy.tags) ? lazy.tags : m.tags;
    const showLoadAffordance = (!tags || !tags.length) && !(lazy && lazy.loading);
    return `
    <div class="sfs-lib-card">
      <div class="sfs-lib-name">${esc(m.name)}</div>
      ${m.description ? `<div class="sfs-lib-desc">${esc(m.description)}</div>` : ""}
      <div class="sfs-tag-row">
        ${(tags || []).map((t) => {
          const compat = COMPAT_INFO[t.compatibility];
          const modelRef = `${m.name}:${t.tag}`;
          const installed = _sfsIsInstalled(modelRef);
          const job = _sfs.pullJobs[modelRef];
          const installing = job && job.status === "running";
          const pct = job ? Math.round((job.progress || 0) * 100) : 0;
          return `<div class="sfs-chip" style="flex-direction:column;align-items:stretch;gap:4px">
            <div class="sfs-row" style="gap:8px">
              <span>${esc(t.tag)}</span>
              <span class="sfs-about-key">${t.size_gb != null ? t.size_gb + "GB" : "size unknown"}</span>
              ${compat ? `<span style="display:inline-flex;align-items:center;gap:5px">
                <span class="sfs-chip-dot" style="background:${compat.color}"></span>
                <span style="color:${compat.color}">${compat.label}</span></span>` : ""}
              <button class="sfs-btn" style="padding:4px 10px;font-size:12px" data-pull-model="${esc(modelRef)}"
                ${installed || installing ? "disabled" : ""}>${installed ? "Installed ✓" : (installing ? `${pct}%` : "Install")}</button>
            </div>
            ${installing ? `<div class="sfs-progress"><div class="sfs-progress-fill" style="width:${pct}%"></div></div>` : ""}
          </div>`;
        }).join("")}
        ${lazy && lazy.loading ? `<div class="sfs-hint">Loading sizes…</div>` : ""}
        ${lazy && lazy.error ? `<div class="sfs-hint" style="color:var(--warn)">Couldn't load sizes: ${esc(lazy.error)}</div>` : ""}
        ${showLoadAffordance ? `<button class="sfs-btn" style="padding:4px 10px;font-size:12px" data-load-tags="${esc(m.name)}">Load sizes</button>` : ""}
      </div>
    </div>`;
  }).join("");

  box.querySelectorAll("[data-pull-model]").forEach((btn) => {
    btn.onclick = () => _sfsPullModel(btn.dataset.pullModel);
  });
  box.querySelectorAll("[data-load-tags]").forEach((btn) => {
    btn.onclick = () => _sfsLoadTags(btn.dataset.loadTags);
  });
  refreshIcons();
}

async function _sfsLoadTags(name) {
  _sfs.lazyTags[name] = { loading: true };
  _sfsRenderLibraryResults();
  try {
    const res = await api(`/ollama/library/${encodeURIComponent(name)}/tags`);
    _sfs.lazyTags[name] = { loading: false, tags: res.tags };
  } catch (e) {
    _sfs.lazyTags[name] = { loading: false, error: e.message };
  }
  _sfsRenderLibraryResults();
}

async function _sfsPullModel(modelRef, onProgress) {
  try {
    const { job_id } = await api("/ollama/pull", { method: "POST", body: { model: modelRef } });
    _sfs.pullJobs[modelRef] = { status: "running", progress: 0 };
    _sfsRenderLibraryResults();
    if (onProgress) onProgress();
    _sfsWatchPullJob(job_id, modelRef, onProgress);
  } catch (e) {
    _sfsToast(`Couldn't start install: ${e.message}`, true);
  }
}

function _sfsWatchPullJob(jobId, modelRef, onProgress) {
  // Deliberately NOT gated on the modal or even the Settings section being
  // open/visible -- an install must keep going (and keep updating the
  // "Installed" chip list once it lands) no matter what the user is looking
  // at, per v5.11 §2 ("closing the modal never loses an in-flight pull").
  if (_sfs.polling.has(jobId)) return;
  _sfs.polling.add(jobId);
  const poll = async () => {
    let job;
    try {
      job = await api(`/jobs/${jobId}`);
    } catch (_e) {
      _sfs.polling.delete(jobId);
      return;
    }
    _sfs.pullJobs[modelRef] = job;
    if (_sfs.modelModalOpen) {
      _sfsRenderLibraryResults();
      if (onProgress) onProgress();
    }
    if (job.status === "running") {
      setTimeout(poll, 1200);
    } else {
      _sfs.polling.delete(jobId);
      try {
        _sfs.models = await api("/ollama/models");
        _sfs.modelsError = null;
      } catch (e) {
        _sfs.modelsError = e.message;
      }
      _sfsRefreshInstalledUI();
      if (_sfs.modelModalOpen) {
        _sfsRenderLibraryResults();
        if (onProgress) onProgress();
      }
      if (job.status === "error") _sfsToast(`Install failed: ${job.error || "unknown error"}`, true);
      else _sfsToast("Installed ✓");
    }
  };
  poll();
}

/* ---------- Performance ---------- */

function _sfsRenderPerformance(host) {
  const perf = _sfs.settings.performance || {};
  host.innerHTML = `
    <div>
      <div class="sfs-h1">Performance</div>
      <div class="sfs-sub">Resource limits so heavy renders never freeze the machine.</div>
    </div>
    <div class="sfs-card">
      <div class="sfs-field">
        <label class="sfs-label">Max parallel ffmpeg jobs</label>
        <input type="number" min="1" step="1" class="sfs-input" id="sfs-perf-parallel"
          value="${perf.max_parallel_ffmpeg ?? 2}" />
        <div class="sfs-hint">How many ffmpeg processes can run at once across all projects.
          Lower = safer on shared/older Macs, higher = faster batches on powerful ones.</div>
      </div>
      <div class="sfs-field">
        <label class="sfs-label">ffmpeg threads per job</label>
        <input type="number" min="1" step="1" class="sfs-input" id="sfs-perf-threads"
          value="${perf.ffmpeg_threads ?? ""}" placeholder="auto" />
        <div class="sfs-hint">Leave empty for auto (uses about half the CPU cores). Set a number to cap
          how many threads a single ffmpeg encode can use.</div>
      </div>
      <div class="sfs-field">
        <label class="sfs-label">Minimum free RAM (GB)</label>
        <input type="number" min="0" step="0.5" class="sfs-input" id="sfs-perf-ram"
          value="${perf.min_free_ram_gb ?? 4}" />
        <div class="sfs-hint">Heavy steps wait/queue instead of starting when available RAM drops below this.</div>
      </div>
    </div>`;

  ["sfs-perf-parallel", "sfs-perf-threads", "sfs-perf-ram"].forEach((id) => {
    $(`#${id}`).onchange = _sfsSavePerformance;
  });
}

async function _sfsSavePerformance() {
  const threadsVal = $("#sfs-perf-threads").value.trim();
  const performance = {
    max_parallel_ffmpeg: parseInt($("#sfs-perf-parallel").value, 10) || 1,
    ffmpeg_threads: threadsVal === "" ? null : parseInt(threadsVal, 10),
    min_free_ram_gb: parseFloat($("#sfs-perf-ram").value) || 0,
  };
  try {
    _sfs.settings = await api("/settings", { method: "PUT", body: { performance } });
    _sfsToast("Saved ✓");
  } catch (e) {
    _sfsToast(`Couldn't save: ${e.message}`, true);
  }
}

/* ---------- About ---------- */

async function _sfsRenderAbout(host) {
  host.innerHTML = `
    <div>
      <div class="sfs-h1">About</div>
      <div class="sfs-sub">Magic Video Editor by carlosedm10</div>
    </div>
    <div class="sfs-card" id="sfs-about-card"><div class="sfs-hint">Loading health…</div></div>`;

  try {
    const h = await api("/health");
    $("#sfs-about-card").innerHTML = `
      <div class="sfs-about-row"><span class="sfs-about-key">App</span>
        <span>${esc(h.name || "Magic Video Editor")} by ${esc(h.by || "carlosedm10")}</span></div>
      <div class="sfs-about-row"><span class="sfs-about-key">Version</span><span>${esc(h.version)}</span></div>
      <div class="sfs-about-row"><span class="sfs-about-key">Data directory</span><span>${esc(h.data_dir)}</span></div>
      <div class="sfs-about-row"><span class="sfs-about-key">ffmpeg</span>
        <span class="${h.ffmpeg ? "sfs-ok" : "sfs-bad"}">${h.ffmpeg ? "✓ found" : "✗ missing"}</span></div>
      <div class="sfs-about-row"><span class="sfs-about-key">Ollama</span>
        <span class="${h.ollama ? "sfs-ok" : "sfs-bad"}">${h.ollama ? "✓ reachable" : "✗ unreachable"}</span></div>
      <div class="sfs-about-row"><span class="sfs-about-key">Default model</span><span>${esc(h.model)}</span></div>
      <div class="sfs-about-row"><span class="sfs-about-key">Whisper model</span><span>${esc(h.whisper)}</span></div>
      ${h.ollama ? "" : `<div class="sfs-hint" style="color:var(--warn);margin-top:8px">
        Ollama looks unreachable — model pickers in Models may be empty or stale.</div>`}`;
  } catch (e) {
    $("#sfs-about-card").innerHTML = `<div class="sfs-hint">Failed to load health: ${esc(e.message)}</div>`;
  }
}

window.TABS.settings = renderSettings;
