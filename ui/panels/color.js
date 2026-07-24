/* Color-filter panel (spec: "Color filters"). Preset chips + 4 sliders
   (brightness/contrast/saturation/temperature, -1..1) + a live before/after
   preview pair against the first camera clip at t=25% duration, debounced
   300ms, plus Apply/Save persisting project["color"] via
   PUT /api/projects/{pid}/color.

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

function _colorPreviewClip(project) {
  return project.clips.find((c) => c.role === "camera" && c.info?.has_video);
}

window.ColorPanel = window.ColorPanel || {
  render(container, project, refresh) {
    project = project || state.project;
    refresh = refresh || refreshProject;
    if (!container || !project) return;

    const cfg = _colorDefaults(project);
    const clip = _colorPreviewClip(project);

    container.innerHTML = `
      <div class="card">
        <b>Color grading</b>
        <div class="hint">Pick a preset and fine-tune with the sliders — the preview updates live.</div>
        ${!clip ? '<div class="dim">Add a camera clip to preview color filters.</div>' : `
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
        <div class="row" style="gap:20px;align-items:flex-start">
          <div>
            <div class="dim" style="margin-bottom:4px">Before</div>
            <img id="color-before" style="max-width:280px;width:100%;border-radius:10px;background:#000;display:block" />
          </div>
          <div>
            <div class="dim" style="margin-bottom:4px">After</div>
            <img id="color-after" style="max-width:280px;width:100%;border-radius:10px;background:#000;display:block" />
          </div>
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn primary" id="color-save">Save</button>
          <span id="color-feedback" class="dim"></span>
        </div>
        `}
      </div>`;

    if (!clip) return;

    const t = (clip.info.duration || 1) * 0.25;
    const beforeUrl = `/api/projects/${project.id}/preview-frame?clip_id=${clip.id}&t=${t}&preset=none`;
    let debounceTimer = null;

    const updateAfter = () => {
      const q = new URLSearchParams({
        clip_id: clip.id, t: String(t), preset: cfg.preset,
        brightness: String(cfg.brightness), contrast: String(cfg.contrast),
        saturation: String(cfg.saturation), temperature: String(cfg.temperature),
      });
      $("#color-after").src = `/api/projects/${project.id}/preview-frame?${q.toString()}&_=${Date.now()}`;
    };

    $("#color-before").src = `${beforeUrl}&_=${Date.now()}`;
    updateAfter();

    document.querySelectorAll(".color-preset").forEach((btn) => {
      btn.onclick = () => {
        cfg.preset = btn.dataset.preset;
        document.querySelectorAll(".color-preset").forEach((b) =>
          b.classList.toggle("primary", b === btn));
        updateAfter();
      };
    });

    document.querySelectorAll(".color-slider").forEach((input) => {
      input.oninput = () => {
        const key = input.dataset.key;
        cfg[key] = Number(input.value);
        $(`#color-val-${key}`).textContent = cfg[key];
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(updateAfter, 300);
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
