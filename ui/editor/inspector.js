/* Right-column inspector — FCP-style TABS across the top (spec v4 §4):
   Video | Color | Audio | Subs | FX | Ideas. Icons + short labels, garnet
   active underline, content area scrolls. Each tab's panel lives in its own
   div inside #inspector-pane and is independently try/catch'd so a failure
   in one panel never blanks the others or the tab bar itself.

   Panel ownership:
     Video  -> renderVideo() below (selected segment info + in/out +
               transition-into, unchanged from the pre-tabs inspector).
     Color  -> window.ColorPanel        (ui/panels/color.js)
     Audio  -> window.AudioPanel        (ui/panels/audio.js)
     Subs   -> window.SubtitlesPanel    (ui/panels/subtitles.js, NEW)
     FX     -> window.FxPanel           (ui/panels/fx.js, NEW)
     Ideas  -> window.EditorUI.suggestions (ui/editor/suggestions.js)

   Selecting the Color tab activates the on-viewer before/after comparison
   divider (window.EditorUI.compare, ui/editor/compare.js, NEW); leaving it
   deactivates that overlay.

   NOTE for the integrator: this file is self-contained CSS-wise (it injects
   its own <style> tag once, see _ensureStyles) so no style.css edit is
   required. It does need three new files loaded as <script> tags that don't
   exist in index.html yet: ui/panels/subtitles.js, ui/panels/fx.js and
   ui/editor/compare.js (any position is fine — nothing here is *read*
   until Inspector.mount() runs, which happens well after all script tags
   have executed). See this task's final report for the exact lines. */

window.EditorUI = window.EditorUI || {};

const INSP_TABS = [
  ["video", "video", "Video"],
  ["color", "palette", "Color"],
  ["audio", "volume-2", "Audio"],
  ["subs", "captions", "Subs"],
  ["fx", "wand-2", "FX"],
  ["ideas", "lightbulb", "Ideas"],
];

function _ensureInspectorStyles() {
  if (document.getElementById("insp-tabs-styles")) return;
  const style = document.createElement("style");
  style.id = "insp-tabs-styles";
  style.textContent = `
    #inspector-pane { display: flex; flex-direction: column; padding: 0; overflow: hidden; }
    .insp-tabs { display: flex; flex-shrink: 0; overflow-x: auto; border-bottom: 1px solid var(--border);
      background: var(--panel2); }
    .insp-tab { flex: 1 1 0; min-width: 0; background: none; border: none; border-bottom: 2px solid transparent;
      color: var(--dim); cursor: pointer; padding: 9px 4px 7px; font-size: 10px; display: flex;
      flex-direction: column; align-items: center; gap: 2px; white-space: nowrap;
      transition: color .15s ease, border-color .15s ease; }
    .insp-tab .insp-tab-icon { font-size: 15px; line-height: 1; }
    .insp-tab:hover { color: var(--text); }
    .insp-tab.active { color: var(--text); border-bottom-color: var(--accent-hover); font-weight: 600; }
    .insp-tabpanels { flex: 1; min-height: 0; overflow-y: auto; padding: 12px; }
    .insp-tabpanels .insp-panel { margin-bottom: 4px; }
    .insp-tabpanels .insp-panel .card:last-child { margin-bottom: 0; }

    /* ---- shared bits for the new Subs/FX panels ---- */
    .chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
    .subs-chip { background: var(--panel2); border: 1px solid var(--border); border-radius: 10px;
      padding: 6px 8px; cursor: pointer; text-align: left; color: var(--text); }
    .subs-chip:hover { border-color: var(--accent); }
    .subs-chip.active { border-color: var(--accent-hover); box-shadow: 0 0 0 1px var(--accent-hover); }
    .subs-chip-label { font-size: 11px; color: var(--dim); margin-bottom: 4px; }
    .subs-mini { background: #000; border-radius: 6px; padding: 10px 6px; text-align: center;
      font-size: 12px; color: #fff; text-shadow: 0 0 3px #000, 0 0 3px #000; }
    .subs-mini.bold { font-weight: 800; font-size: 13px; }
    .subs-mini .kw { color: var(--warn); }
    .swatch-row { display: flex; align-items: center; gap: 14px; margin: 8px 0; }
    .swatch-row label { font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 6px; }
    input[type=color] { width: 30px; height: 26px; padding: 0; border-radius: 6px; border: 1px solid var(--border);
      background: var(--panel2); cursor: pointer; }

    /* ---- on-viewer before/after divider (ui/editor/compare.js) ---- */
    .compare-overlay { position: absolute; inset: 0; z-index: 5; pointer-events: none; }
    .compare-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
      background: transparent; pointer-events: none; }
    .compare-divider { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px;
      background: var(--accent-hover); box-shadow: 0 0 8px rgba(194,32,48,.7); cursor: ew-resize;
      pointer-events: auto; z-index: 6; }
    .compare-handle { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
      width: 26px; height: 26px; border-radius: 999px; background: var(--accent-hover); color: #fff;
      display: flex; align-items: center; justify-content: center; font-size: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.4); }
    .compare-label { position: absolute; top: 8px; font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
      color: #fff; background: rgba(0,0,0,.55); padding: 3px 8px; border-radius: 999px; pointer-events: none; }
    .compare-label.left { left: 8px; } .compare-label.right { right: 8px; }
  `;
  document.head.appendChild(style);
}

const Inspector = {
  activeTab: "video",

  mount() {
    _ensureInspectorStyles();
    // A previous project may have left the Color-tab comparison overlay
    // mounted on #player-stage — always start clean.
    try { window.EditorUI.compare?.deactivate(); } catch (e) { console.error(e); }
    this._buildShell();
    this.renderVideo();
    this.renderColorAudio();
    try {
      const el = document.getElementById("insp-suggestions");
      window.EditorUI.suggestions?.mount(el);
    } catch (e) {
      console.error("Suggestions panel failed to mount", e);
    }
    refreshIcons();
  },

  _buildShell() {
    const pane = document.getElementById("inspector-pane");
    if (!pane) return;
    pane.innerHTML = `
      <nav class="insp-tabs" id="insp-tabs">
        ${INSP_TABS.map(([key, icon, label]) => `
          <button class="insp-tab" data-insp-tab="${key}" title="${label}">
            <span class="insp-tab-icon"><i data-lucide="${icon}"></i></span><span>${label}</span>
          </button>`).join("")}
      </nav>
      <div class="insp-tabpanels" id="insp-tabpanels">
        <div id="insp-video" class="insp-section insp-panel" data-panel="video"></div>
        <div id="insp-color" class="insp-section insp-panel" data-panel="color" hidden></div>
        <div id="insp-audio" class="insp-section insp-panel" data-panel="audio" hidden></div>
        <div id="insp-subs" class="insp-section insp-panel" data-panel="subs" hidden></div>
        <div id="insp-fx" class="insp-section insp-panel" data-panel="fx" hidden></div>
        <div id="insp-ideas" class="insp-section insp-panel" data-panel="ideas" hidden>
          <div id="insp-suggestions"></div>
        </div>
      </div>`;
    pane.querySelectorAll("[data-insp-tab]").forEach((btn) => {
      btn.onclick = () => this.switchTab(btn.dataset.inspTab);
    });
    this.activeTab = "video";
    this._applyTabVisibility();
  },

  switchTab(tab) {
    if (tab === this.activeTab) return;
    const leavingColor = this.activeTab === "color";
    this.activeTab = tab;
    this._applyTabVisibility();
    try {
      if (leavingColor) window.EditorUI.compare?.deactivate();
      if (tab === "color") window.EditorUI.compare?.activate(state.project);
    } catch (e) {
      console.error("Color comparison overlay failed to toggle", e);
    }
  },

  _applyTabVisibility() {
    const pane = document.getElementById("inspector-pane");
    if (!pane) return;
    pane.querySelectorAll("[data-insp-tab]").forEach((btn) =>
      btn.classList.toggle("active", btn.dataset.inspTab === this.activeTab));
    pane.querySelectorAll(".insp-panel").forEach((el) =>
      el.hidden = el.dataset.panel !== this.activeTab);
  },

  renderColorAudio() {
    const project = state.project;
    if (!project) return;
    try {
      const el = document.getElementById("insp-color");
      if (el && window.ColorPanel) window.ColorPanel.render(el, project, refreshProject);
    } catch (e) {
      console.error("Color panel failed to render", e);
    }
    try {
      const el = document.getElementById("insp-audio");
      if (el && window.AudioPanel) window.AudioPanel.render(el, project, refreshProject);
    } catch (e) {
      console.error("Audio panel failed to render", e);
    }
    try {
      const el = document.getElementById("insp-subs");
      if (el && window.SubtitlesPanel) window.SubtitlesPanel.render(el, project);
    } catch (e) {
      console.error("Subtitles panel failed to render", e);
    }
    try {
      const el = document.getElementById("insp-fx");
      if (el && window.FxPanel) window.FxPanel.render(el, project);
    } catch (e) {
      console.error("FX panel failed to render", e);
    }
  },

  renderVideo() {
    const el = document.getElementById("insp-video");
    if (!el) return;
    const segs = Editor.segments;
    if (!segs || !segs.length) {
      el.innerHTML = `<div class="card"><b>Video</b>
        <div class="hint">No segments yet — drag a clip from the media bin onto the timeline below to
        start a manual cut, or run the pipeline for an AI-assisted one.</div></div>`;
      return;
    }
    const i = Math.min(Editor.selected, segs.length - 1);
    const s = segs[i];
    const clip = Editor.clip(s.clip_id);
    const tr = s.transition || { type: "none", duration: 0.5 };
    const TR_TYPES = [["none", "None"], ["fade", "Fade"], ["crossfade", "Crossfade"]];

    el.innerHTML = `
      <div class="card">
        <div class="row"><b>Video</b><span class="grow"></span>
          <span class="dim">segment ${i + 1}/${segs.length}</span></div>
        <div class="hint">${esc(clip?.filename || s.clip_id)}</div>
        <div class="field-row">
          <label>In</label>
          <input type="number" step="0.1" id="insp-start" value="${s.start.toFixed(2)}">
          <label style="width:auto">Out</label>
          <input type="number" step="0.1" id="insp-end" value="${s.end.toFixed(2)}">
        </div>
        <div class="dim">Duration ${fmtT(s.end - s.start)}</div>
        <div style="margin-top:10px">
          <label class="dim" style="display:block;margin-bottom:4px">Transition into this segment</label>
          <div class="transition-btns">
            ${TR_TYPES.map(([key, label]) => `
              <button class="btn small tr-btn ${tr.type === key ? "active" : ""}" data-tr="${key}">${label}</button>`).join("")}
          </div>
          ${tr.type !== "none" ? `
            <div class="field-row">
              <label>Duration</label>
              <input type="number" step="0.1" min="0.2" max="1.5" id="insp-tr-dur" value="${tr.duration.toFixed(2)}">
              <span class="dim">seconds</span>
            </div>` : ""}
        </div>
      </div>`;

    const startInput = document.getElementById("insp-start");
    const endInput = document.getElementById("insp-end");
    if (startInput) startInput.onchange = () => Editor.trim(i, "start", Number(startInput.value));
    if (endInput) endInput.onchange = () => Editor.trim(i, "end", Number(endInput.value));

    el.querySelectorAll(".tr-btn").forEach((btn) => {
      btn.onclick = () => Editor.setTransition(i, btn.dataset.tr);
    });
    const trDur = document.getElementById("insp-tr-dur");
    if (trDur) trDur.onchange = () => Editor.setTransitionDuration(i, Number(trDur.value));
  },
};

window.EditorUI.inspector = Inspector;
