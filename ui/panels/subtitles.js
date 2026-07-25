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
   instant) and schedules a debounced PUT. */

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

  render(container, project) {
    project = project || state.project;
    if (!container || !project) return;
    if (this._loadedPid !== project.id) {
      this._loadedPid = project.id;
      this._cfg = null;
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
      btn.onclick = () => { cfg.position = btn.dataset.position; rerenderAndSave(); };
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
      scheduleSave();
    };
  },
};
