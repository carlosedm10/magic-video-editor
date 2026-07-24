/* Settings tab — per-task model strategy (spec: "Per-task model strategy").
   Models card (default_model + per-task overrides sourced from
   GET /api/ollama/models), Transcription card (whisper_model), System card
   (read-only health). Save persists via PUT /api/settings. */

const TASK_INFO = [
  ["take_judge", "Take judging",
    "Scores retakes of the same line to pick the best one. Works fine on smaller/faster models."],
  ["transcript_cleaner", "Transcript cleanup",
    "Finds restarts, abandoned takes and filler to cut before dedup. Wants a bigger model to judge well."],
  ["clip_order", "Story ordering",
    "Decides the narrative order of clips. Wants a bigger model to judge well."],
  ["reel_scorer", "Reel scoring",
    "Scores candidate moments for short-form reels. Wants a bigger model to judge well."],
];

const selectStyle = `background:var(--panel2);border:1px solid var(--border);color:var(--text);
  border-radius:8px;padding:7px 10px;width:100%;max-width:360px;font:inherit`.replace(/\s+/g, " ");

async function renderSettings() {
  const pane = $("#tab-settings");
  pane.innerHTML = `<div class="hint">Loading settings…</div>`;

  let settings;
  let models = [];
  let modelsError = null;
  try {
    settings = await api("/settings");
  } catch (e) {
    pane.innerHTML = `<div class="card dim">Failed to load settings: ${esc(e.message)}</div>`;
    return;
  }
  try {
    models = await api("/ollama/models");
  } catch (e) {
    modelsError = e.message;
  }

  const modelOptions = (selected) => {
    const opts = models.map((m) =>
      `<option value="${esc(m.name)}" ${m.name === selected ? "selected" : ""}>
        ${esc(m.name)} (${m.size_gb}GB)</option>`);
    if (selected && !models.some((m) => m.name === selected)) {
      opts.unshift(`<option value="${esc(selected)}" selected>${esc(selected)} (not pulled)</option>`);
    }
    return opts.join("");
  };

  const taskOptions = (selected) => {
    const nullSel = selected == null ? "selected" : "";
    return `<option value="" ${nullSel}>(use default)</option>` + modelOptions(selected || "");
  };

  pane.innerHTML = `
    <div class="card">
      <b>Models</b>
      <div class="hint">Pick the default Ollama model, and override it per task. Bigger models
        judge better; smaller models are faster.</div>
      ${modelsError ? `<div class="dim" style="color:var(--warn)">
        Couldn't reach Ollama: ${esc(modelsError)}</div>` : ""}
      <div style="margin:10px 0">
        <label class="dim" style="display:block;margin-bottom:4px">Default model</label>
        <select id="s-default-model" style="${selectStyle}">
          ${modelOptions(settings.default_model)}
        </select>
      </div>
      ${TASK_INFO.map(([key, label, desc]) => `
        <div style="margin:14px 0">
          <label class="dim" style="display:block;margin-bottom:4px">${esc(label)}</label>
          <select id="s-task-${key}" data-task="${key}" style="${selectStyle}">
            ${taskOptions(settings.task_models[key])}
          </select>
          <div class="hint" style="margin:4px 0 0">${esc(desc)}</div>
        </div>`).join("")}
    </div>

    <div class="card">
      <b>Transcription</b>
      <div class="hint">Whisper model (mlx-community repo). Default suggestion shown below.</div>
      <input type="text" id="s-whisper-model" value="${esc(settings.whisper_model)}"
        placeholder="mlx-community/whisper-large-v3-turbo" />
    </div>

    <div class="card" id="s-system-card"><b>System</b><div class="hint">Loading health…</div></div>

    <div class="row" style="margin-top:6px">
      <button class="btn primary" id="s-save">Save settings</button>
      <span id="s-feedback" class="dim"></span>
    </div>`;

  renderSystemCard();

  $("#s-save").onclick = async () => {
    const feedback = $("#s-feedback");
    feedback.textContent = "Saving…";
    feedback.style.color = "";
    const task_models = {};
    TASK_INFO.forEach(([key]) => {
      const v = $(`#s-task-${key}`).value;
      task_models[key] = v === "" ? null : v;
    });
    try {
      await api("/settings", {
        method: "PUT",
        body: {
          default_model: $("#s-default-model").value,
          task_models,
          whisper_model: $("#s-whisper-model").value,
        },
      });
      feedback.textContent = "Saved.";
      feedback.style.color = "var(--accent2)";
    } catch (e) {
      feedback.textContent = `Failed to save: ${e.message}`;
      feedback.style.color = "var(--danger)";
    }
  };
}

async function renderSystemCard() {
  const card = $("#s-system-card");
  if (!card) return;
  try {
    const h = await api("/health");
    card.innerHTML = `
      <b>System</b>
      <div class="row" style="margin-top:8px">
        <span>ffmpeg <span class="${h.ffmpeg ? "ok" : "bad"}" style="color:${h.ffmpeg ? "var(--accent2)" : "var(--danger)"}">${h.ffmpeg ? "✓" : "missing"}</span></span>
        <span>ollama <span class="${h.ollama ? "ok" : "bad"}" style="color:${h.ollama ? "var(--accent2)" : "var(--danger)"}">${h.ollama ? "✓" : "down"}</span></span>
        <span class="dim">data dir: ${esc(h.data_dir)}</span>
      </div>
      ${h.ollama ? "" : `<div class="hint" style="color:var(--warn)">
        Ollama looks unreachable — model pickers above may be empty/stale.</div>`}
      <div class="hint">To add a model: <code>ollama pull &lt;name&gt;</code></div>`;
  } catch (e) {
    card.innerHTML = `<b>System</b><div class="dim">Failed to load health: ${esc(e.message)}</div>`;
  }
}

window.TABS.settings = renderSettings;
