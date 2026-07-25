/* Voice-enhancement + 8-band EQ panel.

   "Enhance voice" toggle (persisted via POST /api/projects/{pid}/audio-enhance)
   plus an A/B preview row (two <audio> players loading a 10s original vs.
   enhanced+EQ'd sample from POST /api/projects/{pid}/audio-preview), plus an
   8-band EQ section: 8 vertical sliders (gains persisted via
   GET/PUT /api/projects/{pid}/audio-eq), a Reset button, and a couple of
   presets. EQ changes are debounce-saved to the backend (applied at
   render/preview-render time via magic_video_editor/pipeline/eq.py) AND
   applied LIVE to whatever is currently playing in the player via WebAudio
   BiquadFilterNodes, so moving a slider is heard instantly without waiting
   for a render.

   Exposes window.AudioPanel.render(container) — reads the current project
   off the shared `state` global (ui/core.js) so callers (e.g. the Studio/
   edit tab) just need to give it a container to render into. Also exposes
   window.PANELS.audio(container, project, refresh) as an alternate entry
   point for callers that already have the project/refresh handy. */

// Mirrors magic_video_editor/pipeline/eq.py EQ_FREQS_HZ — keep in sync.
const AUDIO_EQ_FREQS = [60, 150, 400, 1000, 2400, 6000, 12000, 16000];
const AUDIO_EQ_MIN_DB = -12;
const AUDIO_EQ_MAX_DB = 12;
const AUDIO_EQ_Q = 1.0;
const AUDIO_EQ_FLAT = AUDIO_EQ_FREQS.map(() => 0);

const AUDIO_EQ_PRESETS = {
  voz: { label: "Voz", gains: [-2, 0, 0, 0, 2, 1, 0, 0] },
  plano: { label: "Plano", gains: AUDIO_EQ_FLAT.slice() },
  musica: { label: "Música", gains: [3, 2, -1, -1, -1, 1, 2, 3] },
};

function _audioEqFreqLabel(hz) {
  return hz >= 1000 ? `${(hz / 1000).toFixed(hz % 1000 ? 1 : 0)}k` : `${hz}`;
}

function _audioEqNormalizeGains(gains) {
  const out = AUDIO_EQ_FLAT.slice();
  if (Array.isArray(gains)) {
    for (let i = 0; i < AUDIO_EQ_FREQS.length; i++) {
      const g = Number(gains[i]);
      if (Number.isFinite(g)) out[i] = Math.max(AUDIO_EQ_MIN_DB, Math.min(AUDIO_EQ_MAX_DB, g));
    }
  }
  return out;
}

window.AudioPanel = window.AudioPanel || {
  _preview: null, // { clipId, original_url, enhanced_url } | null
  _eqGains: null, // current 8 gains (live working copy, pre-save)
  _eqSaveTimer: null,

  // ---------- live WebAudio EQ (draft playback) ----------
  _audioCtx: null,
  _wiredEls: null, // WeakSet<HTMLMediaElement>
  _chains: null, // Map<HTMLMediaElement, BiquadFilterNode[]>

  _findPlayerVideos() {
    const nodes = document.querySelectorAll(
      "video.player-video, #video-a, #video-b, #video-preview"
    );
    return Array.from(new Set(nodes));
  },

  // Wires a WebAudio graph (MediaElementSource -> 8 peaking biquads ->
  // destination) onto every player <video> element found, ONCE per element
  // (a given HTMLMediaElement can only ever get one
  // createMediaElementSource call — a second call throws). Never throws:
  // any failure here must not break normal <video> playback.
  _ensureLiveEqGraph() {
    try {
      if (!this._wiredEls) this._wiredEls = new WeakSet();
      if (!this._chains) this._chains = new Map();
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!this._audioCtx) this._audioCtx = new AC();
      if (this._audioCtx.state === "suspended") {
        this._audioCtx.resume().catch(() => {});
      }

      for (const el of this._findPlayerVideos()) {
        if (!el || this._wiredEls.has(el)) continue;
        try {
          const source = this._audioCtx.createMediaElementSource(el);
          const filters = AUDIO_EQ_FREQS.map((freq) => {
            const f = this._audioCtx.createBiquadFilter();
            f.type = "peaking";
            f.frequency.value = freq;
            f.Q.value = AUDIO_EQ_Q;
            f.gain.value = 0;
            return f;
          });
          let node = source;
          for (const f of filters) {
            node.connect(f);
            node = f;
          }
          node.connect(this._audioCtx.destination);
          this._chains.set(el, filters);
          this._wiredEls.add(el);
        } catch (e) {
          // Already connected elsewhere, or the browser refused for some
          // other reason — skip this element, playback itself is unaffected
          // since we never touched el.src/el.srcObject.
          console.warn("AudioPanel: could not wire live EQ for", el?.id, e);
        }
      }

      this._applyLiveGains(this._eqGains || AUDIO_EQ_FLAT);
    } catch (e) {
      console.warn("AudioPanel: live EQ graph setup failed (non-fatal)", e);
    }
  },

  _applyLiveGains(gains) {
    if (!this._chains) return;
    try {
      for (const filters of this._chains.values()) {
        filters.forEach((f, i) => {
          f.gain.value = gains[i] ?? 0;
        });
      }
    } catch (e) {
      console.warn("AudioPanel: applying live EQ gains failed (non-fatal)", e);
    }
  },

  // ---------- persistence ----------
  _scheduleEqSave(pid, gains) {
    if (this._eqSaveTimer) clearTimeout(this._eqSaveTimer);
    this._eqSaveTimer = setTimeout(async () => {
      try {
        await api(`/projects/${pid}/audio-eq`, { method: "PUT", body: { gains } });
      } catch (e) {
        console.warn("AudioPanel: saving EQ failed", e);
      }
    }, 400);
  },

  render(container, project, refresh) {
    if (!container) return;
    project = project || state.project;
    refresh = refresh || (() => refreshProject());
    const enabled = !!project.audio_enhance;
    const clips = project.clips || [];

    if (!this._eqGains) this._eqGains = _audioEqNormalizeGains(project.audio_eq);
    const gains = this._eqGains;

    this._ensureLiveEqGraph();

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

    const sliders = AUDIO_EQ_FREQS.map((freq, i) => {
      const g = gains[i];
      return `
        <div class="audio-eq-band" style="display:flex;flex-direction:column;align-items:center;gap:4px;width:44px">
          <div class="dim audio-eq-value" data-band="${i}" style="font-size:11px">${g > 0 ? "+" : ""}${g.toFixed(1)}</div>
          <input type="range" class="audio-eq-slider" data-band="${i}"
            min="${AUDIO_EQ_MIN_DB}" max="${AUDIO_EQ_MAX_DB}" step="0.5" value="${g}"
            style="writing-mode: vertical-lr; direction: rtl; width: 20px; height: 110px;" />
          <div class="dim" style="font-size:11px">${_audioEqFreqLabel(freq)}</div>
        </div>`;
    }).join("");

    container.innerHTML = `
      <div class="card">
        <div class="row">
          <label class="row" style="gap:6px">
            <input type="checkbox" id="audio-enhance-toggle" ${enabled ? "checked" : ""} />
            Enhance voice
          </label>
          <span class="dim">Neural voice enhancement, loudness normalize (-16 LUFS)</span>
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
      </div>
      <div class="card" style="margin-top:12px">
        <div class="row" style="justify-content:space-between">
          <div><b>8-band EQ</b> <span class="dim">applied to render, reels, and draft playback</span></div>
          <div class="row" style="gap:6px">
            <button class="btn small" id="audio-eq-preset-voz">Voz</button>
            <button class="btn small" id="audio-eq-preset-musica">Música</button>
            <button class="btn small" id="audio-eq-preset-plano">Plano</button>
            <button class="btn small" id="audio-eq-reset">Reset</button>
          </div>
        </div>
        <div class="row" style="margin-top:14px;gap:6px;align-items:flex-end;justify-content:center">
          ${sliders}
        </div>
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

    // ---------- EQ wiring ----------
    const applyGainsToUi = (newGains) => {
      this._eqGains = newGains;
      container.querySelectorAll(".audio-eq-slider").forEach((el) => {
        const i = Number(el.dataset.band);
        el.value = newGains[i];
      });
      container.querySelectorAll(".audio-eq-value").forEach((el) => {
        const i = Number(el.dataset.band);
        const g = newGains[i];
        el.textContent = `${g > 0 ? "+" : ""}${g.toFixed(1)}`;
      });
      this._applyLiveGains(newGains);
    };

    container.querySelectorAll(".audio-eq-slider").forEach((slider) => {
      slider.oninput = () => {
        const i = Number(slider.dataset.band);
        const g = Math.max(AUDIO_EQ_MIN_DB, Math.min(AUDIO_EQ_MAX_DB, parseFloat(slider.value) || 0));
        this._eqGains[i] = g;
        const label = container.querySelector(`.audio-eq-value[data-band="${i}"]`);
        if (label) label.textContent = `${g > 0 ? "+" : ""}${g.toFixed(1)}`;
        this._applyLiveGains(this._eqGains);
        this._scheduleEqSave(state.pid, this._eqGains);
      };
    });

    const presetBtn = (id, gains) => {
      const btn = container.querySelector(id);
      if (btn) {
        btn.onclick = () => {
          applyGainsToUi(gains.slice());
          this._scheduleEqSave(state.pid, this._eqGains);
        };
      }
    };
    presetBtn("#audio-eq-preset-voz", AUDIO_EQ_PRESETS.voz.gains);
    presetBtn("#audio-eq-preset-musica", AUDIO_EQ_PRESETS.musica.gains);
    presetBtn("#audio-eq-preset-plano", AUDIO_EQ_PRESETS.plano.gains);
    presetBtn("#audio-eq-reset", AUDIO_EQ_FLAT);
  },
};

window.PANELS = window.PANELS || {};
window.PANELS.audio = (container, project, refresh) =>
  window.AudioPanel.render(container, project, refresh);
