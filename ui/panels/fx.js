/* FX inspector panel (spec v7.5 "Transitions catalog (FCP-style, ffmpeg
   xfade)"): the FX tab IS the transitions browser now. All of the actual
   catalog-fetching/thumbnail/apply/drag logic lives in
   ui/editor/transitions.js (window.EditorUI.transitions) — this file is
   just the thin Inspector-panel adapter so ui/editor/inspector.js's
   existing `window.FxPanel.render(el, project)` call keeps working
   unchanged.

   NOT to be confused with the top-level ui/fx.js, which is the unrelated
   background aurora canvas effect. */

window.FxPanel = window.FxPanel || {
  render(container, project) {
    if (!container) return;
    try {
      window.EditorUI.transitions?.render(container, project);
    } catch (e) {
      console.error("Transitions browser failed to render", e);
      container.innerHTML = `<div class="card"><b>FX</b><div class="hint">Failed to load: ${e.message}</div></div>`;
    }
  },
};
