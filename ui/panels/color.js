/* Color-filter panel (spec v5.7: professional color pipeline + LUT import).
   Rebuilds the old 4-slider panel into Lightroom/Premiere-style groups over
   the new schema (magic_video_editor/pipeline/filters.py DEFAULT_COLOR):

     WB       -> temperature, tint
     Tone     -> exposure, black_point, white_point, brightness, contrast
     Presence -> saturation, vibrance
     Detail   -> sharpness
     Look     -> preset chips + LUT dropdown/import/intensity

   Every slider pairs a <input type=range> with a numeric <input type=number>
   (both editable, kept in sync) and double-click-resets to its neutral
   default (0 for every slider; LUT intensity defaults to 1.0). Every change
   (before Save) is forwarded live to window.EditorUI.compare.setLiveConfig()
   (CSS-approximation divider — a no-op if it isn't mounted) AND schedules a
   debounced GET /api/projects/{pid}/preview-frame?<all params> fetch that
   renders this card's "Exact preview" thumbnail from the REAL ffmpeg chain
   (the only way to see an active LUT's true look, since CSS has no
   lut3d/haldclut equivalent — compare.js shows a note to that effect on the
   on-viewer divider).

   LUT import tries the pywebview native dialog first
   (window.pywebview.api.pick_lut_files, if the desktop shell exposes it —
   see this task's final report: app.py doesn't have one yet, out of this
   panel's file ownership) and falls back to a plain <input type=file>
   (accept=".cube,.3dl,.png") feeding POST /api/luts/import as multipart —
   fully functional either way per the app-first "native else file input"
   rule.

   Exposes window.ColorPanel.render(container, project, refresh) — project
   and refresh are optional and fall back to the global state/refreshProject
   so any caller (e.g. the Studio/edit tab) can just do
   ColorPanel.render(container). */

const COLOR_PRESETS = [
  ["none", "None"], ["bw", "B&W"], ["sepia", "Sepia"],
  ["cinematic", "Cinematic"], ["vintage", "Vintage"],
];

// key, label, min, max, step, group
const COLOR_SLIDERS = [
  ["temperature", "Temperature", -1, 1, 0.01, "wb"],
  ["tint", "Tint", -1, 1, 0.01, "wb"],
  ["exposure", "Exposure (EV)", -3, 3, 0.05, "tone"],
  ["black_point", "Black point", 0, 0.5, 0.01, "tone"],
  ["white_point", "White point", 0, 0.5, 0.01, "tone"],
  ["brightness", "Brightness", -1, 1, 0.01, "tone"],
  ["contrast", "Contrast", -1, 1, 0.01, "tone"],
  ["saturation", "Saturation", -1, 1, 0.01, "presence"],
  ["vibrance", "Vibrance", -1, 1, 0.01, "presence"],
  ["sharpness", "Sharpness", 0, 1, 0.01, "detail"],
];

const COLOR_GROUPS = [
  ["wb", "White Balance"],
  ["tone", "Tone"],
  ["presence", "Presence"],
  ["detail", "Detail"],
];

const _LUT_ACCEPT = ".cube,.3dl,.png";

function _colorDefaults(project) {
  return {
    preset: "none",
    exposure: 0, temperature: 0, tint: 0, black_point: 0, white_point: 0,
    brightness: 0, contrast: 0, saturation: 0, vibrance: 0, sharpness: 0,
    lut: { name: null, intensity: 1.0 },
    ...(project.color || {}),
  };
}

function _pushLiveColor(cfg) {
  try { window.EditorUI?.compare?.setLiveConfig(cfg); } catch (e) { console.error("compare live update failed", e); }
}

// v5.7: the current EDL position gives preview-frame a real clip_id/t to
// render from — falls back to null (panel shows a placeholder) when there's
// nothing to seek into yet (no segments, or the player module isn't mounted).
function _currentPreviewContext() {
  try {
    const t = window.EditorUI?.player?.currentEdlTime?.();
    if (typeof t !== "number" || !window.Editor?.segments?.length) return null;
    const hit = window.Editor.segmentAtEdlTime(t);
    if (!hit) return null;
    const seg = window.Editor.segments[hit.index];
    if (!seg) return null;
    return { clip_id: seg.clip_id, t: hit.local };
  } catch (_e) {
    return null;
  }
}

window.ColorPanel = window.ColorPanel || {
  _luts: [],
  _previewToken: 0,
  _previewTimer: null,

  async render(container, project, refresh) {
    project = project || state.project;
    refresh = refresh || refreshProject;
    if (!container || !project) return;

    const cfg = _colorDefaults(project);
    if (!cfg.lut) cfg.lut = { name: null, intensity: 1.0 };

    try {
      this._luts = await api("/luts");
    } catch (e) {
      console.error("failed to load LUT library", e);
      this._luts = this._luts || [];
    }

    this._renderShell(container, project, refresh, cfg);
  },

  _renderShell(container, project, refresh, cfg) {
    // `dataKey` is what oninput/dblclick-reset wiring keys off of (plain
    // field name for the 10 flat sliders, "lut.intensity" — a dotted path —
    // for the one nested field), `value` is passed explicitly so this
    // works identically for both without any cfg[key] lookup magic.
    const sliderRow = (dataKey, label, min, max, step, value) => `
      <div class="color-ctrl" style="margin-bottom:12px">
        <label class="dim" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px">
          <span>${label}</span>
          <input type="number" class="color-num" data-key="${dataKey}" min="${min}" max="${max}" step="${step}"
            value="${value}" style="width:64px;text-align:right;padding:2px 6px">
        </label>
        <input type="range" class="color-slider" data-key="${dataKey}" min="${min}" max="${max}" step="${step}"
          value="${value}" style="width:100%" title="Double-click to reset to ${dataKey === "lut.intensity" ? "1" : "0"}">
      </div>`;

    const groupCard = (groupKey, groupLabel) => {
      const sliders = COLOR_SLIDERS.filter(([, , , , , g]) => g === groupKey);
      if (!sliders.length) return "";
      return `
        <div class="card">
          <b>${groupLabel}</b>
          ${sliders.map(([key, label, min, max, step]) => sliderRow(key, label, min, max, step, cfg[key])).join("")}
        </div>`;
    };

    const lutOptions = this._luts
      .map((l) => `<option value="${esc(l.name)}" ${cfg.lut.name === l.name ? "selected" : ""}>${esc(l.name)}</option>`)
      .join("");
    const lutActive = !!cfg.lut.name;

    container.innerHTML = `
      <div class="card" id="color-exact-preview">
        <div class="row"><b>Exact preview</b><span class="grow"></span>
          <span class="dim" id="color-preview-note">Rendered from the real ffmpeg chain — the only accurate way to see a LUT.</span></div>
        <img id="color-preview-img" style="max-width:100%;border-radius:8px;display:none;background:#000;margin-top:8px" />
      </div>
      <div class="card">
        <b>Look</b>
        <div class="hint">Presets set a baseline look; any slider you touch above always overrides it for that
          control (matches the render exactly — see filters.py). Open the viewer's before/after divider (this
          tab) for a live CSS approximation of the basic controls.</div>
        <div class="row" id="color-presets" style="margin:10px 0 14px;flex-wrap:wrap">
          ${COLOR_PRESETS.map(([key, label]) => `
            <button class="btn small color-preset ${cfg.preset === key ? "primary" : ""}"
              data-preset="${key}">${label}</button>`).join("")}
        </div>
        <div class="field-row">
          <label style="width:60px">LUT</label>
          <select id="color-lut-select" style="flex:1">
            <option value="">None</option>
            ${lutOptions}
          </select>
        </div>
        <div class="row" style="margin-top:8px">
          <button class="btn small" id="color-lut-import"><i data-lucide="upload"></i> Import LUT…</button>
          <button class="btn small" id="color-lut-delete" title="Remove selected LUT from the library"
            ${lutActive ? "" : "disabled"}><i data-lucide="trash-2"></i></button>
          <span class="dim" id="color-lut-feedback"></span>
        </div>
        <div id="color-lut-intensity-row" style="margin-top:10px${lutActive ? "" : ";display:none"}">
          ${sliderRow("lut.intensity", "LUT intensity", 0, 1, 0.01, cfg.lut.intensity)}
        </div>
      </div>
      ${COLOR_GROUPS.map(([k, label]) => groupCard(k, label)).join("")}
      <div class="card">
        <div class="row">
          <button class="btn primary" id="color-save">Save</button>
          <span id="color-feedback" class="dim"></span>
        </div>
      </div>`;

    // Shared by every control (sliders, presets, LUT select) — one place
    // that does the "live CSS approximation + debounced exact preview" pair
    // the whole panel's brief asks for.
    const onChange = () => {
      _pushLiveColor(cfg);
      this._schedulePreviewFrame(project.id, cfg);
    };

    this._wireInputs(container, cfg, onChange);
    this._wireSave(container, project, refresh, cfg);
    this._wireLut(container, project, refresh, cfg, onChange);

    onChange();
    refreshIcons();
  },

  _wireInputs(container, cfg, onChange) {
    container.querySelectorAll(".color-preset").forEach((btn) => {
      btn.onclick = () => {
        cfg.preset = btn.dataset.preset;
        container.querySelectorAll(".color-preset").forEach((b) =>
          b.classList.toggle("primary", b === btn));
        onChange();
      };
    });

    // Two-way slider<->number binding, keyed by a dotted path so the LUT
    // intensity control (cfg.lut.intensity) reuses the exact same wiring as
    // the flat sliders (cfg[key]) without a special case.
    const setPath = (path, v) => {
      if (path === "lut.intensity") cfg.lut.intensity = v;
      else cfg[path] = v;
    };
    const defaultOf = (path) => (path === "lut.intensity" ? 1.0 : 0);

    container.querySelectorAll(".color-slider").forEach((range) => {
      const key = range.dataset.key;
      // A plain quoted attribute selector -- every dataKey we emit is
      // either a bare field name or "lut.intensity"; a literal "." inside
      // the quotes needs no escaping here.
      const num = container.querySelector(`.color-num[data-key="${key}"]`);
      range.oninput = () => {
        setPath(key, Number(range.value));
        if (num) num.value = range.value;
        onChange();
      };
      range.ondblclick = () => {
        const def = defaultOf(key);
        setPath(key, def);
        range.value = def;
        if (num) num.value = def;
        onChange();
      };
      if (num) {
        num.onchange = () => {
          const min = Number(range.min), max = Number(range.max);
          const v = Math.max(min, Math.min(max, Number(num.value) || 0));
          setPath(key, v);
          range.value = v;
          num.value = v;
          onChange();
        };
      }
    });
  },

  _wireSave(container, project, refresh, cfg) {
    container.querySelector("#color-save").onclick = async () => {
      const feedback = container.querySelector("#color-feedback");
      feedback.textContent = "Saving…";
      feedback.style.color = "";
      try {
        await api(`/projects/${project.id}/color`, { method: "PUT", body: cfg });
        feedback.textContent = "Saved.";
        feedback.style.color = "var(--accent2)";
        await refresh();
      } catch (e) {
        feedback.textContent = `Failed to save: ${e.message}`;
        feedback.style.color = "var(--danger)";
      }
    };
  },

  _wireLut(container, project, refresh, cfg, onChange) {
    const select = container.querySelector("#color-lut-select");
    const deleteBtn = container.querySelector("#color-lut-delete");
    const importBtn = container.querySelector("#color-lut-import");
    const feedback = container.querySelector("#color-lut-feedback");
    const intensityRow = container.querySelector("#color-lut-intensity-row");

    if (select) {
      select.onchange = () => {
        cfg.lut.name = select.value || null;
        if (intensityRow) intensityRow.style.display = cfg.lut.name ? "" : "none";
        if (deleteBtn) deleteBtn.disabled = !cfg.lut.name;
        onChange();
      };
    }

    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        if (!cfg.lut.name) return;
        if (!(await confirmModal(`Remove "${cfg.lut.name}" from the LUT library? This can't be undone.`, { okLabel: "Remove", danger: true }))) return;
        try {
          await api(`/luts/${encodeURIComponent(cfg.lut.name)}`, { method: "DELETE" });
          cfg.lut.name = null;
          this.render(container, project, refresh);
        } catch (e) {
          feedback.textContent = `Delete failed: ${e.message}`;
          feedback.style.color = "var(--danger)";
        }
      };
    }

    // Hidden browser-mode fallback input, feeding the SAME import path a
    // native pick would use (see the file header for the native/fallback
    // rule). Created once per render() call and discarded with the DOM.
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = _LUT_ACCEPT;
    fileInput.hidden = true;
    container.appendChild(fileInput);
    fileInput.onchange = async () => {
      const file = fileInput.files?.[0];
      fileInput.value = "";
      if (!file) return;
      await this._importLutFile(file, container, project, refresh, feedback);
    };

    if (importBtn) {
      importBtn.onclick = async () => {
        // Native dialog first, IF the desktop shell exposes a LUT-capable
        // picker. window.pywebview.api.pick_files() exists today but is
        // hardcoded to media extensions (magic_video_editor/app.py) so
        // reusing it here would hide .cube/.3dl files entirely — this looks
        // for a dedicated pick_lut_files instead and falls back cleanly
        // when it isn't there (see this task's final report).
        const nativeApi = window.pywebview?.api;
        if (nativeApi?.pick_lut_files) {
          try {
            const paths = await nativeApi.pick_lut_files();
            if (paths?.length) {
              await this._importLutPath(paths[0], container, project, refresh, feedback);
              return;
            }
          } catch (e) {
            console.error("native LUT picker failed, falling back to file input", e);
          }
        }
        fileInput.click();
      };
    }
  },

  async _importLutFile(file, container, project, refresh, feedback) {
    feedback.textContent = "Importing…";
    feedback.style.color = "";
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const res = await fetch("/api/luts/import", { method: "POST", body: form });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const { name } = await res.json();
      project.color = project.color || {};
      project.color.lut = { name, intensity: project.color?.lut?.intensity ?? 1.0 };
      this.render(container, project, refresh);
    } catch (e) {
      feedback.textContent = `Import failed: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  },

  async _importLutPath(path, container, project, refresh, feedback) {
    feedback.textContent = "Importing…";
    feedback.style.color = "";
    try {
      const { name } = await api("/luts/import", { method: "POST", body: { path } });
      project.color = project.color || {};
      project.color.lut = { name, intensity: project.color?.lut?.intensity ?? 1.0 };
      this.render(container, project, refresh);
    } catch (e) {
      feedback.textContent = `Import failed: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  },

  // Debounced GET /preview-frame with every current param — the "ground
  // truth" companion to compare.js's CSS approximation, and the only place
  // an active LUT's real look is actually visible before rendering.
  _schedulePreviewFrame(pid, cfg) {
    clearTimeout(this._previewTimer);
    this._previewTimer = setTimeout(() => this._fetchPreviewFrame(pid, cfg), 450);
  },

  async _fetchPreviewFrame(pid, cfg) {
    const img = document.getElementById("color-preview-img");
    const note = document.getElementById("color-preview-note");
    if (!img) return;
    const ctx = _currentPreviewContext();
    if (!ctx) {
      img.style.display = "none";
      if (note) note.textContent = "Select or play a clip to see the exact preview frame.";
      return;
    }
    const params = new URLSearchParams({
      clip_id: ctx.clip_id,
      t: ctx.t.toFixed(3),
      preset: cfg.preset || "none",
      exposure: cfg.exposure, temperature: cfg.temperature, tint: cfg.tint,
      black_point: cfg.black_point, white_point: cfg.white_point,
      brightness: cfg.brightness, contrast: cfg.contrast, saturation: cfg.saturation,
      vibrance: cfg.vibrance, sharpness: cfg.sharpness,
      lut_name: cfg.lut?.name || "", lut_intensity: cfg.lut?.intensity ?? 1,
    });
    const token = ++this._previewToken;
    try {
      const res = await fetch(`/api/projects/${pid}/preview-frame?${params.toString()}`);
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      if (token !== this._previewToken) return; // a newer request already landed — drop this one
      const url = URL.createObjectURL(blob);
      const old = img.dataset.blobUrl;
      img.src = url;
      img.dataset.blobUrl = url;
      img.style.display = "block";
      if (note) {
        note.textContent = cfg.lut?.name
          ? "Rendered from the real ffmpeg chain, including the LUT."
          : "Rendered from the real ffmpeg chain.";
      }
      if (old) URL.revokeObjectURL(old);
    } catch (e) {
      console.error("preview-frame failed", e);
      if (token === this._previewToken && note) note.textContent = "Preview frame failed to render.";
    }
  },
};

window.PANELS = window.PANELS || {};
window.PANELS.color = (container, project, refresh) =>
  window.ColorPanel.render(container, project, refresh);
