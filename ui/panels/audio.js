/* Voice-enhancement panel — "Enhance voice" toggle (persisted via
   POST /api/projects/{pid}/audio-enhance) plus an A/B preview row (two
   <audio> players loading a 10s original vs. enhanced sample from
   POST /api/projects/{pid}/audio-preview).

   Exposes window.AudioPanel.render(container) — reads the current project
   off the shared `state` global (ui/core.js) so callers (e.g. the Studio/
   edit tab) just need to give it a container to render into. Also exposes
   window.PANELS.audio(container, project, refresh) as an alternate entry
   point for callers that already have the project/refresh handy. */

window.AudioPanel = window.AudioPanel || {
  _preview: null, // { clipId, original_url, enhanced_url } | null

  render(container, project, refresh) {
    if (!container) return;
    project = project || state.project;
    refresh = refresh || (() => refreshProject());
    const enabled = !!project.audio_enhance;
    const clips = project.clips || [];

    const clipOptions = clips
      .map((c) => `<option value="${c.id}">${esc(c.filename || c.path)}</option>`)
      .join("");

    const preview = this._preview;
    const previewRow =
      preview && preview.clipId
        ? `<div class="row" style="margin-top:10px">
            <div>
              <div class="dim">Original</div>
              <audio controls src="${preview.original_url}"></audio>
            </div>
            <div>
              <div class="dim">Enhanced</div>
              <audio controls src="${preview.enhanced_url}"></audio>
            </div>
          </div>`
        : `<div class="dim" style="margin-top:10px">No preview generated yet.</div>`;

    container.innerHTML = `
      <div class="card">
        <div class="row">
          <label class="row" style="gap:6px">
            <input type="checkbox" id="audio-enhance-toggle" ${enabled ? "checked" : ""} />
            Enhance voice
          </label>
          <span class="dim">Noise reduction, presence lift, loudness normalize (-16 LUFS)</span>
        </div>
        <div class="hint">Applied to the final render and reels when enabled.</div>
        <div class="row" style="margin-top:12px">
          <select id="audio-preview-clip" ${clips.length ? "" : "disabled"}>
            ${clips.length ? clipOptions : '<option value="">No clips yet</option>'}
          </select>
          <input id="audio-preview-t" type="number" min="0" step="0.5" value="0"
            style="width:80px" title="Start time (seconds)" />
          <button class="btn small" id="audio-preview-btn" ${clips.length ? "" : "disabled"}>
            Generate preview
          </button>
        </div>
        ${previewRow}
      </div>`;

    const toggle = container.querySelector("#audio-enhance-toggle");
    if (toggle) {
      toggle.onchange = async () => {
        try {
          await api(`/projects/${state.pid}/audio-enhance`, {
            method: "POST",
            body: { enabled: toggle.checked },
          });
          await refresh();
        } catch (e) {
          alert(e.message);
        }
      };
    }

    const previewBtn = container.querySelector("#audio-preview-btn");
    if (previewBtn) {
      previewBtn.onclick = async () => {
        const clipId = container.querySelector("#audio-preview-clip").value;
        const t = parseFloat(container.querySelector("#audio-preview-t").value) || 0;
        if (!clipId) return;
        previewBtn.disabled = true;
        previewBtn.textContent = "Generating…";
        try {
          const res = await api(`/projects/${state.pid}/audio-preview`, {
            method: "POST",
            body: { clip_id: clipId, t },
          });
          window.AudioPanel._preview = {
            clipId,
            original_url: res.original_url,
            enhanced_url: res.enhanced_url,
          };
          window.AudioPanel.render(container, project, refresh);
        } catch (e) {
          alert(e.message);
        } finally {
          previewBtn.disabled = false;
          previewBtn.textContent = "Generate preview";
        }
      };
    }
  },
};

window.PANELS = window.PANELS || {};
window.PANELS.audio = (container, project, refresh) =>
  window.AudioPanel.render(container, project, refresh);
