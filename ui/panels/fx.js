/* FX inspector panel (spec v4 §4): a per-project DEFAULT transition
   type+duration with an "Apply to all" button that stamps it onto every
   junction in the timeline at once, plus placeholder text for future
   effects. There is no dedicated backend field for this default — it's a
   convenience that writes straight into the existing per-segment
   `transition` (docs/PLATFORM-SPEC.md "Transitions (junction-level)") via
   the shared Editor object (ui/editor/state.js), the same contract the
   Video tab's per-segment transition picker already uses. Applying goes
   through Editor.commit() directly (one history entry / one autosave) so
   "apply to all" doesn't spam 1 history entry per segment.

   NOT to be confused with the top-level ui/fx.js, which is the unrelated
   background aurora canvas effect. */

window.FxPanel = window.FxPanel || {
  _type: null,
  _dur: null,

  render(container, project) {
    if (!container) return;
    const segs = Editor.segments || [];
    const first = segs[0]?.transition || { type: "none", duration: 0.5 };
    const type = this._type || first.type || "none";
    const dur = this._dur ?? (first.duration ?? 0.5);
    const TR_TYPES = [["none", "None"], ["fade", "Fade"], ["crossfade", "Crossfade"]];

    container.innerHTML = `
      <div class="card">
        <b>Default transition</b>
        <div class="hint">Pick a transition and apply it to every junction in the timeline at once.
          You can still fine-tune individual junctions from the Video tab or the timeline chips.</div>
        <div class="transition-btns" id="fx-tr-types">
          ${TR_TYPES.map(([key, label]) => `
            <button class="btn small ${type === key ? "active" : ""}" data-tr="${key}">${label}</button>`).join("")}
        </div>
        ${type !== "none" ? `
          <div class="field-row">
            <label>Duration</label>
            <input type="number" step="0.1" min="0.2" max="1.5" id="fx-dur" value="${dur.toFixed(2)}">
            <span class="dim">seconds</span>
          </div>` : ""}
        <div class="row" style="margin-top:10px">
          <button class="btn primary" id="fx-apply-all" ${segs.length ? "" : "disabled"}>Apply to all junctions</button>
          <span id="fx-feedback" class="dim"></span>
        </div>
      </div>
      <div class="card">
        <b>More effects</b>
        <div class="hint">Coming soon — speed ramps, stabilization, LUTs and more will show up here.</div>
      </div>`;

    container.querySelectorAll("#fx-tr-types button").forEach((btn) => {
      btn.onclick = () => {
        this._type = btn.dataset.tr;
        this._dur = dur;
        this.render(container, project);
      };
    });

    const durInput = container.querySelector("#fx-dur");
    if (durInput) durInput.onchange = () => {
      this._dur = Math.max(0.2, Math.min(1.5, Number(durInput.value) || 0.5));
    };

    const applyBtn = container.querySelector("#fx-apply-all");
    if (applyBtn) applyBtn.onclick = () => {
      try {
        if (!Editor.segments?.length) return;
        const t = this._type || type;
        const d = this._dur ?? dur;
        const next = Editor.segments.map((s) => ({
          ...s,
          transition: { type: t, duration: t === "none" ? (s.transition?.duration ?? 0.5) : d },
        }));
        Editor.commit(next);
        const fb = container.querySelector("#fx-feedback");
        if (fb) { fb.textContent = "Applied to all junctions."; fb.style.color = "var(--accent2)"; }
      } catch (e) {
        console.error("FX apply-to-all failed", e);
        const fb = container.querySelector("#fx-feedback");
        if (fb) { fb.textContent = `Failed: ${e.message}`; fb.style.color = "var(--danger)"; }
      }
    };
  },
};
