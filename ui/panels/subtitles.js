/* Subtitles inspector panel (spec v4 §6, CapCut-style): enable toggle, style
   chips (clean/bold/karaoke) with mini text previews, font dropdown, size
   S/M/L, color pickers (text + outline), position bottom/center, words-per-
   cue number. Backend contract: GET/PUT /api/projects/{pid}/subtitles +
   GET /api/fonts (magic_video_editor/api/subtitles.py, magic_video_editor/pipeline/subtitles.py).

   Exposes window.SubtitlesPanel.render(container, project) — reads/writes
   the NORMALIZED config from the dedicated subtitles endpoint (not the raw,
   possibly-partial project["subtitles"] field) so defaults always match the
   backend's `normalize_config`. Loads once per project id and caches; every
   control change updates the local cfg immediately (so the UI feels
   instant) and schedules a debounced PUT.

   ---------- v7 §7.6 addendum: main-video cue list + player integration ----------
   A new "Cues" card (below the style controls, same shape as the Reel
   Editor's Subs tab) lists every cue from GET /projects/{pid}/subtitles/cues
   with an editable text input, keyed by that cue's GLOBAL `index` (see
   magic_video_editor/pipeline/subtitles.py's cue_list docstring) — typing
   there writes straight into cfg.cue_overrides[index] and debounce-saves the
   whole config, exactly like every other field on this panel.

   Two methods exist purely for ui/editor/player.js's subtitle inline-edit
   feature to call into (the player owns the double-click/drag gesture on
   the video overlay; this file stays the single source of truth for
   loading/saving the subtitles config so there's one save path, not two):
     - setCueOverride(project, index, text): persists one cue's text (used
       right after the player's contentEditable overlay commits an edit).
     - saveStyleField(fields): persists a partial cfg patch (used for the
       vertical-drag-to-reposition gesture's {position, vpos} result).
     - focusCue(index): scrolls/focuses that cue's row in THIS panel (used
       right when the player opens the Subs tab on double-click). */

const SUBS_STYLES = [
  ["clean", "Clean"],
  ["bold", "Bold"],
  ["karaoke", "Karaoke"],
];
const SUBS_SIZES = [["S", "S"], ["M", "M"], ["L", "L"]];
const SUBS_POSITIONS = [["bottom", "Bottom"], ["center", "Center"]];

function _subsMiniPreview(style, color, outline) {
  const textShadow = `-1px -1px 0 ${outline}, 1px -1px 0 ${outline}, -1px 1px 0 ${outline}, 1px 1px 0 ${outline}`;
  if (style === "karaoke") {
    return `<div class="subs-mini" style="color:${color};text-shadow:${textShadow}">
      Sample <span class="kw">subtitle</span> text</div>`;
  }
  const boldCls = style === "bold" ? " bold" : "";
  return `<div class="subs-mini${boldCls}" style="color:${color};text-shadow:${textShadow}">Sample subtitle text</div>`;
}

window.SubtitlesPanel = window.SubtitlesPanel || {
  _cfg: null,
  _fonts: null,
  _loadedPid: null,
  _saveTimer: null,
  _cues: null,           // v7 §7.6: null = not loaded yet, [] = loaded-but-empty
  _cuesLoading: false,

  render(container, project) {
    project = project || state.project;
    if (!container || !project) return;
    if (this._loadedPid !== project.id) {
      this._loadedPid = project.id;
      this._cfg = null;
      this._cues = null;
      this._load(container, project);
      return;
    }
    if (!this._cfg || !this._fonts) {
      container.innerHTML = '<div class="card"><b>Subtitles</b><div class="hint">Loading…</div></div>';
      return;
    }
    this._draw(container, project);
  },

  async _load(container, project) {
    container.innerHTML = '<div class="card"><b>Subtitles</b><div class="hint">Loading…</div></div>';
    try {
      const [cfg, fontsRes] = await Promise.all([
        api(`/projects/${project.id}/subtitles`),
        api("/fonts"),
      ]);
      this._cfg = cfg;
      this._fonts = fontsRes.fonts || [];
      this._draw(container, project);
    } catch (e) {
      const notReady = e.status === 404;
      container.innerHTML = `
        <div class="card"><b>Subtitles</b>
          <div class="hint">${notReady
            ? "Not available yet for this project."
            : `Couldn't load subtitles config: ${esc(e.message)}`}</div>
          <button class="btn small" id="subs-retry">Retry</button>
        </div>`;
      const retry = container.querySelector("#subs-retry");
      if (retry) retry.onclick = () => { this._loadedPid = null; this.render(container, project); };
    }
  },

  _draw(container, project) {
    const cfg = this._cfg;
    const fonts = this._fonts.length ? this._fonts : [cfg.font];

    container.innerHTML = `
      <div class="card">
        <div class="row">
          <label class="row" style="gap:6px">
            <input type="checkbox" id="subs-enabled" ${cfg.enabled ? "checked" : ""} />
            Enable subtitles
          </label>
          <span class="grow"></span>
          <span id="subs-feedback" class="dim"></span>
        </div>
        <div class="hint">Burned into the final render and previews; live-previewed as a DOM overlay
          on the player while enabled.</div>

        <label class="dim" style="display:block;margin-bottom:2px">Style</label>
        <div class="chip-row" id="subs-styles">
          ${SUBS_STYLES.map(([key, label]) => `
            <button class="subs-chip ${cfg.style === key ? "active" : ""}" data-style="${key}">
              <div class="subs-chip-label">${label}</div>
              ${_subsMiniPreview(key, cfg.color, cfg.outline_color)}
            </button>`).join("")}
        </div>

        <div class="field-row">
          <label>Font</label>
          <select id="subs-font" style="flex:1">
            ${fonts.map((f) => `<option value="${esc(f)}" ${cfg.font === f ? "selected" : ""}>${esc(f)}</option>`).join("")}
          </select>
        </div>

        <label class="dim" style="display:block;margin:8px 0 2px">Size</label>
        <div class="transition-btns" id="subs-sizes">
          ${SUBS_SIZES.map(([key, label]) => `
            <button class="btn small ${cfg.size === key ? "active" : ""}" data-size="${key}">${label}</button>`).join("")}
        </div>

        <label class="dim" style="display:block;margin:10px 0 2px">Position</label>
        <div class="transition-btns" id="subs-positions">
          ${SUBS_POSITIONS.map(([key, label]) => `
            <button class="btn small ${cfg.position === key ? "active" : ""}" data-position="${key}">${label}</button>`).join("")}
        </div>

        <div class="swatch-row">
          <label>Text <input type="color" id="subs-color" value="${cfg.color}"></label>
          <label>Outline <input type="color" id="subs-outline" value="${cfg.outline_color}"></label>
        </div>

        <div class="field-row">
          <label>Words/cue</label>
          <input type="number" id="subs-wpc" min="1" max="12" step="1" value="${cfg.words_per_cue}">
        </div>
      </div>
      <div class="card">
        <b>Cues</b>
        <div class="hint">Typo fixes for the main video's burned-in captions — double-click a subtitle
          on the player (while paused) to jump straight to its row here.</div>
        <div id="subs-cue-list">${this._cueListHtml()}</div>
      </div>`;

    const feedback = container.querySelector("#subs-feedback");
    const setFeedback = (text, isError) => {
      if (!feedback) return;
      feedback.textContent = text;
      feedback.style.color = isError ? "var(--danger)" : "var(--accent2)";
    };

    const scheduleSave = () => {
      clearTimeout(this._saveTimer);
      setFeedback("Saving…", false);
      feedback && (feedback.style.color = "");
      this._saveTimer = setTimeout(async () => {
        try {
          const saved = await api(`/projects/${project.id}/subtitles`, { method: "PUT", body: this._cfg });
          this._cfg = saved;
          if (state.project) state.project.subtitles = saved;
          window.EditorUI?.player?.reloadSubtitles?.();
          setFeedback("Saved", false);
        } catch (e) {
          setFeedback(`Save failed: ${e.message}`, true);
        }
      }, 400);
    };

    const rerenderAndSave = () => { this._draw(container, project); scheduleSave(); };

    const enabledInput = container.querySelector("#subs-enabled");
    if (enabledInput) enabledInput.onchange = () => { cfg.enabled = enabledInput.checked; scheduleSave(); };

    container.querySelectorAll("#subs-styles .subs-chip").forEach((btn) => {
      btn.onclick = () => { cfg.style = btn.dataset.style; rerenderAndSave(); };
    });
    container.querySelectorAll("#subs-sizes button").forEach((btn) => {
      btn.onclick = () => { cfg.size = btn.dataset.size; rerenderAndSave(); };
    });
    container.querySelectorAll("#subs-positions button").forEach((btn) => {
      // An explicit preset click always wins over any prior vertical-drag
      // nudge from the player overlay (v7 §7.6) -- reset vpos so the two
      // controls never fight each other.
      btn.onclick = () => { cfg.position = btn.dataset.position; cfg.vpos = 0; rerenderAndSave(); };
    });

    const fontSelect = container.querySelector("#subs-font");
    if (fontSelect) fontSelect.onchange = () => { cfg.font = fontSelect.value; scheduleSave(); };

    const colorInput = container.querySelector("#subs-color");
    if (colorInput) colorInput.oninput = () => { cfg.color = colorInput.value; scheduleSave(); };
    const outlineInput = container.querySelector("#subs-outline");
    if (outlineInput) outlineInput.oninput = () => { cfg.outline_color = outlineInput.value; scheduleSave(); };

    const wpcInput = container.querySelector("#subs-wpc");
    if (wpcInput) wpcInput.onchange = () => {
      cfg.words_per_cue = Math.max(1, Math.min(12, Number(wpcInput.value) || 4));
      this._cues = null; // grouping changed -- the cue list must be refetched, not just re-rendered
      rerenderAndSave();
    };

    this._wireCueInputs(container, project);
    if (!this._cues && !this._cuesLoading) this._loadCues(project);
  },

  /* ---------- v7 §7.6: cue list (main video) ---------- */

  _cueListHtml() {
    if (!this._cues) return '<div class="dim">Loading…</div>'; // null = not fetched yet (about to be, or in flight)
    if (!this._cues.length) return '<div class="dim">No cues yet — run the pipeline first.</div>';
    return this._cues.map((c) => `
      <div class="field-row" data-cue-row="${c.index}">
        <label class="mono">${fmtT(c.edl_t_start)}</label>
        <input type="text" class="subs-cue-input" data-cue-idx="${c.index}" value="${esc(c.text)}">
      </div>`).join("");
  },

  _wireCueInputs(container, project) {
    container.querySelectorAll(".subs-cue-input").forEach((inp) => {
      inp.oninput = () => {
        const idx = inp.dataset.cueIdx;
        const cue = this._cues.find((c) => String(c.index) === idx);
        if (cue) cue.text = inp.value;
        clearTimeout(this._cueSaveTimers?.[idx]);
        this._cueSaveTimers = this._cueSaveTimers || {};
        this._cueSaveTimers[idx] = setTimeout(() => this.setCueOverride(project, Number(idx), inp.value), 500);
      };
    });
  },

  async _loadCues(project) {
    if (this._cuesLoading) return;
    this._cuesLoading = true;
    try {
      const { cues } = await api(`/projects/${project.id}/subtitles/cues`);
      this._cues = cues || [];
    } catch (_e) {
      this._cues = [];
    }
    this._cuesLoading = false;
    const list = document.getElementById("subs-cue-list");
    if (list) { list.innerHTML = this._cueListHtml(); this._wireCueInputs(list, project); }
  },

  /* ---------- public API for ui/editor/player.js's subtitle inline edit ----------
     Kept here (rather than duplicated in player.js) so there is exactly ONE
     path that loads/mutates/saves the subtitles config -- the player's
     double-click/drag gestures just call into it. All three tolerate the
     panel never having been rendered yet (e.g. the user never opened the
     Subs tab this session) by lazily loading the config first. */

  async _ensureLoaded(project) {
    if (this._loadedPid === project.id && this._cfg && this._fonts) return true;
    try {
      const [cfg, fontsRes] = await Promise.all([
        this._cfg && this._loadedPid === project.id ? this._cfg : api(`/projects/${project.id}/subtitles`),
        this._fonts ? { fonts: this._fonts } : api("/fonts"),
      ]);
      this._cfg = cfg;
      this._fonts = fontsRes.fonts || [];
      this._loadedPid = project.id;
      return true;
    } catch (e) {
      console.error("Couldn't load subtitles config", e);
      return false;
    }
  },

  async setCueOverride(project, index, text) {
    project = project || state.project;
    if (!project || !(await this._ensureLoaded(project))) return;
    this._cfg.cue_overrides = { ...(this._cfg.cue_overrides || {}), [index]: text };
    try {
      this._cfg = await api(`/projects/${project.id}/subtitles`, { method: "PUT", body: this._cfg });
      if (state.project) state.project.subtitles = this._cfg;
      window.EditorUI?.player?.reloadSubtitles?.();
    } catch (e) {
      console.error("Failed to save subtitle cue override", e);
    }
    const cue = this._cues?.find((c) => c.index === index);
    if (cue) cue.text = text;
    const container = document.getElementById("insp-subs");
    if (container && !container.hidden) this._draw(container, project);
  },

  async saveStyleField(fields) {
    const project = state.project;
    if (!project || !(await this._ensureLoaded(project))) return;
    Object.assign(this._cfg, fields);
    try {
      this._cfg = await api(`/projects/${project.id}/subtitles`, { method: "PUT", body: this._cfg });
      if (state.project) state.project.subtitles = this._cfg;
      window.EditorUI?.player?.reloadSubtitles?.();
    } catch (e) {
      console.error("Failed to save subtitle style field", e);
    }
    const container = document.getElementById("insp-subs");
    if (container && !container.hidden) this._draw(container, project);
  },

  async focusCue(index) {
    const project = state.project;
    if (!project) return;
    if (this._loadedPid !== project.id || !this._cues) {
      if (!(await this._ensureLoaded(project))) return;
      await this._loadCues(project);
    }
    const container = document.getElementById("insp-subs");
    if (container && !container.querySelector(`[data-cue-row="${index}"]`)) this._draw(container, project);
    requestAnimationFrame(() => {
      const input = document.querySelector(`.subs-cue-input[data-cue-idx="${index}"]`);
      if (!input) return;
      input.scrollIntoView({ block: "center", behavior: "smooth" });
      input.focus();
      input.select?.();
    });
  },
};
