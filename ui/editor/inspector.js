/* Right-column inspector: Video (selected segment: clip/in-out/duration +
   transition-into selector), Color (delegates to ui/panels/color.js), Audio
   (delegates to ui/panels/audio.js), Suggestions (ui/editor/suggestions.js).
   Each section is independently try/catch'd so a failure in one panel never
   blanks the others. */

window.EditorUI = window.EditorUI || {};

const Inspector = {
  mount() {
    this.renderVideo();
    this.renderColorAudio();
    try {
      const el = document.getElementById("insp-suggestions");
      window.EditorUI.suggestions?.mount(el);
    } catch (e) {
      console.error("Suggestions panel failed to mount", e);
    }
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
  },

  renderVideo() {
    const el = document.getElementById("insp-video");
    if (!el) return;
    const segs = Editor.segments;
    if (!segs || !segs.length) {
      el.innerHTML = `<div class="card"><b>Video</b>
        <div class="hint">No segments yet — add clips and run the pipeline, or split/trim below.</div></div>`;
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
