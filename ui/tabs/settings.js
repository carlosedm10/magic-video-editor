/* Settings — full-screen page (spec v4 section 5).
   Mounted by core.js into #tab-settings, which the drawer/shell is expected
   to size full-viewport (this module renders assuming the container it's
   given covers everything -- no drawer-width styling here).

   Left section nav: General | Brand | Models | Performance | Transcription | About.
   - General: export_dir (native pick_folder w/ manual-paste fallback) +
     "Open folder" (POST /api/open-folder). Saves via PUT /api/settings.
   - Brand: settings.brand_profile free-text textarea (spec v5 addendum "SEO
     copywriter + brand profile"), fed to the copywriter agent. Autosave on
     blur + explicit Save button, character count.
   - Models: default + per-task dropdowns (GET /api/ollama/models) PLUS the
     Ollama model manager (GET /api/ollama/library, POST /api/ollama/pull
     polled via GET /api/jobs/{id}, DELETE /api/ollama/models/{name}).
   - Performance: max_parallel_ffmpeg / ffmpeg_threads / min_free_ram_gb.
   - Transcription: whisper_model.
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
  ["transcription", "Transcription"],
  ["about", "About"],
];

const COMPAT_INFO = {
  great: { label: "Runs great", color: "var(--accent2)" },
  tight: { label: "Tight fit", color: "var(--warn)" },
  too_big: { label: "Too big for this Mac", color: "var(--danger)" },
};

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
    .sfs-content { flex:1 1 auto; overflow-y:auto; padding:40px 56px 80px; }
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
      white-space:nowrap;
    }
    .sfs-btn:hover { border-color:var(--accent); }
    .sfs-btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
    .sfs-btn.primary:hover { background:var(--accent-hover); border-color:var(--accent-hover); }
    .sfs-btn:disabled { opacity:0.5; cursor:default; }
    .sfs-feedback { font-size:13px; color:var(--dim); }
    .sfs-guide { font-size:13px; color:var(--dim); line-height:1.6; margin:0 0 4px; }
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
    .sfs-installed-row {
      display:flex; align-items:center; justify-content:space-between; gap:10px;
      padding:9px 0; border-top:1px solid var(--border);
    }
    .sfs-installed-row:first-child { border-top:none; }
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
      _sfsRenderShell();
    };
  });

  const host = $("#sfs-section");
  if (_sfs.section === "general") _sfsRenderGeneral(host);
  else if (_sfs.section === "brand") _sfsRenderBrand(host);
  else if (_sfs.section === "models") _sfsRenderModels(host);
  else if (_sfs.section === "performance") _sfsRenderPerformance(host);
  else if (_sfs.section === "transcription") _sfsRenderTranscription(host);
  else if (_sfs.section === "about") _sfsRenderAbout(host);
}

/* ---------- General ---------- */

function _sfsRenderGeneral(host) {
  const s = _sfs.settings;
  host.innerHTML = `
    <div>
      <div class="sfs-h1">General</div>
      <div class="sfs-sub">Where finished exports land.</div>
    </div>
    <div class="sfs-card">
      <div class="sfs-field">
        <label class="sfs-label">Export folder</label>
        <div class="sfs-row">
          <input type="text" class="sfs-input" id="sfs-export-dir" value="${esc(s.export_dir || "")}" />
          <button class="sfs-btn" id="sfs-choose-folder">Choose…</button>
          <button class="sfs-btn" id="sfs-open-folder">Open folder</button>
        </div>
        <div class="sfs-hint">Final renders and reels are written here, under a per-project folder.
          If a native folder picker isn't available (browser mode), paste the path directly.</div>
      </div>
      <div class="sfs-row" style="margin-top:20px">
        <button class="sfs-btn primary" id="sfs-save-general">Save</button>
        <span class="sfs-feedback" id="sfs-general-feedback"></span>
      </div>
    </div>`;

  $("#sfs-choose-folder").onclick = async () => {
    const api_ = window.pywebview?.api;
    if (api_?.pick_folder) {
      try {
        const picked = await api_.pick_folder();
        const path = Array.isArray(picked) ? picked[0] : picked;
        if (path) $("#sfs-export-dir").value = path;
      } catch (e) {
        alert(`Folder picker failed: ${e.message}`);
      }
    } else {
      alert("Native folder picker isn't available here — paste the folder path directly.");
    }
  };

  $("#sfs-open-folder").onclick = async () => {
    try {
      await api("/open-folder", { method: "POST", body: { path: $("#sfs-export-dir").value } });
    } catch (e) {
      alert(`Couldn't open folder: ${e.message}`);
    }
  };

  $("#sfs-save-general").onclick = async () => {
    const feedback = $("#sfs-general-feedback");
    feedback.textContent = "Saving…";
    feedback.style.color = "";
    try {
      _sfs.settings = await api("/settings", {
        method: "PUT",
        body: { export_dir: $("#sfs-export-dir").value },
      });
      feedback.textContent = "Saved.";
      feedback.style.color = "var(--accent2)";
    } catch (e) {
      feedback.textContent = `Failed to save: ${e.message}`;
      feedback.style.color = "var(--danger)";
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
          Saves automatically when you click away, or use Save below.</div>
      </div>
      <div class="sfs-row" style="margin-top:6px">
        <button class="sfs-btn primary" id="sfs-save-brand">Save</button>
        <span class="sfs-feedback" id="sfs-brand-feedback"></span>
      </div>
    </div>`;

  const textarea = $("#sfs-brand-profile");
  const charcount = $("#sfs-brand-charcount");
  textarea.oninput = () => {
    charcount.textContent = `${textarea.value.length} characters`;
  };
  textarea.onblur = () => _sfsSaveBrand({ silent: true });
  $("#sfs-save-brand").onclick = () => _sfsSaveBrand({ silent: false });
}

async function _sfsSaveBrand({ silent }) {
  const textarea = $("#sfs-brand-profile");
  if (!textarea) return;
  const feedback = $("#sfs-brand-feedback");
  const value = textarea.value;
  if (silent && _sfs.settings && (_sfs.settings.brand_profile || "") === value) return;
  if (feedback) {
    feedback.textContent = "Saving…";
    feedback.style.color = "";
  }
  try {
    _sfs.settings = await api("/settings", {
      method: "PUT",
      body: { brand_profile: value },
    });
    if (feedback) {
      feedback.textContent = "Saved.";
      feedback.style.color = "var(--accent2)";
    }
  } catch (e) {
    if (feedback) {
      feedback.textContent = `Failed to save: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  }
}

/* ---------- Models ---------- */

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

function _sfsRenderModels(host) {
  const s = _sfs.settings;
  host.innerHTML = `
    <div>
      <div class="sfs-h1">Models</div>
      <div class="sfs-sub">Pick the default Ollama model, override it per task, and manage what's installed.</div>
    </div>

    <div class="sfs-card">
      <div class="sfs-label" style="margin-bottom:12px">Task models</div>
      ${_sfs.modelsError ? `<div class="sfs-hint" style="color:var(--warn)">
        Couldn't reach Ollama: ${esc(_sfs.modelsError)}</div>` : ""}
      <div class="sfs-field">
        <label class="sfs-label">Default model</label>
        <select class="sfs-select" id="s-default-model">${_sfsModelOptions(s.default_model)}</select>
      </div>
      ${TASK_INFO.map(([key, label, desc]) => `
        <div class="sfs-field">
          <label class="sfs-label">${esc(label)}</label>
          <select class="sfs-select" id="s-task-${key}" data-task="${key}">
            ${_sfsTaskOptions(s.task_models[key])}
          </select>
          <div class="sfs-hint">${esc(desc)}</div>
        </div>`).join("")}
      <div class="sfs-row" style="margin-top:6px">
        <button class="sfs-btn primary" id="s-save-models">Save</button>
        <span class="sfs-feedback" id="s-models-feedback"></span>
      </div>
    </div>

    <div class="sfs-card">
      <div class="sfs-label">Get more models</div>
      <div class="sfs-guide">Models run 100% locally via Ollama — nothing leaves this Mac.
        Search below, check the fit for your machine's RAM, and install.</div>
      <div id="sfs-reco-block" style="margin-top:14px">${_sfsRecommendationHtml()}</div>
      <div class="sfs-row" style="margin-top:16px">
        <input type="text" class="sfs-input" id="sfs-lib-search" placeholder="Search models… (e.g. qwen, llama, gemma)"
          value="${esc(_sfs.libQuery)}" />
        <button class="sfs-btn" id="sfs-lib-search-btn">Search</button>
      </div>
      <div id="sfs-lib-results" style="margin-top:6px"></div>
    </div>

    <div class="sfs-card">
      <div class="sfs-label">Installed models</div>
      <div id="sfs-installed-list">${_sfsInstalledListHtml()}</div>
    </div>`;

  $("#s-save-models").onclick = async () => {
    const feedback = $("#s-models-feedback");
    feedback.textContent = "Saving…";
    feedback.style.color = "";
    const task_models = {};
    TASK_INFO.forEach(([key]) => {
      const v = $(`#s-task-${key}`).value;
      task_models[key] = v === "" ? null : v;
    });
    try {
      _sfs.settings = await api("/settings", {
        method: "PUT",
        body: { default_model: $("#s-default-model").value, task_models },
      });
      feedback.textContent = "Saved.";
      feedback.style.color = "var(--accent2)";
    } catch (e) {
      feedback.textContent = `Failed to save: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  };

  $("#sfs-lib-search-btn").onclick = () => _sfsSearchLibrary($("#sfs-lib-search").value);
  $("#sfs-lib-search").onkeydown = (e) => { if (e.key === "Enter") _sfsSearchLibrary(e.target.value); };

  if (_sfs.libResults !== null) _sfsRenderLibraryResults();
  else _sfsSearchLibrary("");

  if (_sfs.recommendation !== null || _sfs.recommendationError) _sfsAttachRecommendationHandlers();
  else _sfsLoadRecommendation();

  _sfsAttachDeleteHandlers();
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
}

function _sfsAttachRecommendationHandlers() {
  const box = $("#sfs-reco-block");
  if (!box) return;
  box.querySelectorAll("[data-pull-model]").forEach((btn) => {
    btn.onclick = () => _sfsPullModel(btn.dataset.pullModel, () => {
      const b = $("#sfs-reco-block");
      if (b) b.innerHTML = _sfsRecommendationHtml();
      _sfsAttachRecommendationHandlers();
    });
  });
}

function _sfsInstalledListHtml() {
  if (_sfs.modelsError) return `<div class="sfs-hint">Unavailable — Ollama isn't reachable.</div>`;
  if (!_sfs.models.length) return `<div class="sfs-hint">No models installed yet.</div>`;
  return _sfs.models.map((m) => `
    <div class="sfs-installed-row">
      <span>${esc(m.name)} <span class="sfs-about-key">(${m.size_gb}GB${m.family ? ", " + esc(m.family) : ""})</span></span>
      <button class="sfs-btn" data-delete-model="${esc(m.name)}">Delete</button>
    </div>`).join("");
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
    alert(`Couldn't start install: ${e.message}`);
  }
}

function _sfsWatchPullJob(jobId, modelRef, onProgress) {
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
    if (_sfs.section === "models") {
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
      if (_sfs.section === "models") {
        const list = $("#sfs-installed-list");
        if (list) list.innerHTML = _sfsInstalledListHtml();
        _sfsAttachDeleteHandlers();
        _sfsRenderLibraryResults();
        if (onProgress) onProgress();
      }
      if (job.status === "error") alert(`Install failed: ${job.error || "unknown error"}`);
    }
  };
  poll();
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
        const list = $("#sfs-installed-list");
        if (list) list.innerHTML = _sfsInstalledListHtml();
        _sfsAttachDeleteHandlers();
      } catch (e) {
        alert(`Delete failed: ${e.message}`);
        btn.disabled = false;
      }
    };
  });
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
      <div class="sfs-row" style="margin-top:6px">
        <button class="sfs-btn primary" id="sfs-save-perf">Save</button>
        <span class="sfs-feedback" id="sfs-perf-feedback"></span>
      </div>
    </div>`;

  $("#sfs-save-perf").onclick = async () => {
    const feedback = $("#sfs-perf-feedback");
    feedback.textContent = "Saving…";
    feedback.style.color = "";
    const threadsVal = $("#sfs-perf-threads").value.trim();
    const performance = {
      max_parallel_ffmpeg: parseInt($("#sfs-perf-parallel").value, 10) || 1,
      ffmpeg_threads: threadsVal === "" ? null : parseInt(threadsVal, 10),
      min_free_ram_gb: parseFloat($("#sfs-perf-ram").value) || 0,
    };
    try {
      _sfs.settings = await api("/settings", { method: "PUT", body: { performance } });
      feedback.textContent = "Saved.";
      feedback.style.color = "var(--accent2)";
    } catch (e) {
      feedback.textContent = `Failed to save: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  };
}

/* ---------- Transcription ---------- */

function _sfsRenderTranscription(host) {
  const s = _sfs.settings;
  host.innerHTML = `
    <div>
      <div class="sfs-h1">Transcription</div>
      <div class="sfs-sub">Whisper model used to transcribe clips (mlx-community repo).</div>
    </div>
    <div class="sfs-card">
      <div class="sfs-field">
        <label class="sfs-label">Whisper model</label>
        <input type="text" class="sfs-input" id="s-whisper-model" value="${esc(s.whisper_model)}"
          placeholder="mlx-community/whisper-large-v3-turbo" />
        <div class="sfs-hint">Any mlx-community Whisper repo id. Larger models transcribe more accurately but slower.</div>
      </div>
      <div class="sfs-row" style="margin-top:6px">
        <button class="sfs-btn primary" id="sfs-save-whisper">Save</button>
        <span class="sfs-feedback" id="sfs-whisper-feedback"></span>
      </div>
    </div>`;

  $("#sfs-save-whisper").onclick = async () => {
    const feedback = $("#sfs-whisper-feedback");
    feedback.textContent = "Saving…";
    feedback.style.color = "";
    try {
      _sfs.settings = await api("/settings", {
        method: "PUT",
        body: { whisper_model: $("#s-whisper-model").value },
      });
      feedback.textContent = "Saved.";
      feedback.style.color = "var(--accent2)";
    } catch (e) {
      feedback.textContent = `Failed to save: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  };
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
