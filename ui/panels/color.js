/* Color-filter panel (spec: "Color filters"; v4 §4 removed the old
   side-by-side before/after <img> preview — that job now belongs to the
   draggable on-viewer comparison divider, ui/editor/compare.js, which mounts
   over #player-stage while the inspector's Color tab is active). This panel
   is just preset chips + 4 sliders (brightness/contrast/saturation/
   temperature, -1..1) plus Save, persisting project["color"] via
   PUT /api/projects/{pid}/color. Every change (before saving) is forwarded
   live to window.EditorUI.compare.setLiveConfig() so the divider updates
   instantly as the user drags — a no-op if the overlay isn't mounted.

   Exposes window.ColorPanel.render(container, project, refresh) — project
   and refresh are optional and fall back to the global state/refreshProject
   so any caller (e.g. the Studio/edit tab) can just do
   ColorPanel.render(container). */

const COLOR_PRESETS = [
  ["none", "None"], ["bw", "B&W"], ["sepia", "Sepia"],
  ["cinematic", "Cinematic"], ["vintage", "Vintage"],
];

const COLOR_SLIDERS = [
  ["brightness", "Brightness"], ["contrast", "Contrast"],
  ["saturation", "Saturation"], ["temperature", "Temperature"],
];

function _colorDefaults(project) {
  return {
    preset: "none", brightness: 0, contrast: 0, saturation: 0, temperature: 0,
    ...(project.color || {}),
  };
}

function _pushLiveColor(cfg) {
  try { window.EditorUI?.compare?.setLiveConfig(cfg); } catch (e) { console.error("compare live update failed", e); }
}

window.ColorPanel = window.ColorPanel || {
  render(container, project, refresh) {
    project = project || state.project;
    refresh = refresh || refreshProject;
    if (!container || !project) return;

    const cfg = _colorDefaults(project);

    container.innerHTML = `
      <div class="card">
        <b>Color grading</b>
        <div class="hint">Pick a preset and fine-tune with the sliders — open the viewer's before/after
          divider (this tab) to see it live; the exact look comes from the rendered preview.</div>
        <div class="row" id="color-presets" style="margin:10px 0">
          ${COLOR_PRESETS.map(([key, label]) => `
            <button class="btn small color-preset ${cfg.preset === key ? "primary" : ""}"
              data-preset="${key}">${label}</button>`).join("")}
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:14px">
          ${COLOR_SLIDERS.map(([key, label]) => `
            <div>
              <label class="dim" style="display:flex;justify-content:space-between">
                <span>${label}</span><span id="color-val-${key}">${cfg[key]}</span>
              </label>
              <input type="range" class="color-slider" data-key="${key}" min="-1" max="1"
                step="0.05" value="${cfg[key]}" style="width:100%">
            </div>`).join("")}
        </div>
        <div class="row">
          <button class="btn primary" id="color-save">Save</button>
          <span id="color-feedback" class="dim"></span>
        </div>
      </div>`;

    _pushLiveColor(cfg);

    document.querySelectorAll(".color-preset").forEach((btn) => {
      btn.onclick = () => {
        cfg.preset = btn.dataset.preset;
        document.querySelectorAll(".color-preset").forEach((b) =>
          b.classList.toggle("primary", b === btn));
        _pushLiveColor(cfg);
      };
    });

    document.querySelectorAll(".color-slider").forEach((input) => {
      input.oninput = () => {
        const key = input.dataset.key;
        cfg[key] = Number(input.value);
        $(`#color-val-${key}`).textContent = cfg[key];
        _pushLiveColor(cfg);
      };
    });

    $("#color-save").onclick = async () => {
      const feedback = $("#color-feedback");
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
};
