/* Voice-enhancement + 8-band EQ panel.

   "Enhance voice" toggle (persisted via POST /api/projects/{pid}/audio-enhance)
   is neural (DeepFilterNet3) and can't run live like the 8-band EQ does —
   it only ever audibly applies on a real render/export. The "Probar (desde
   el cursor)" button next to it is the on-demand fix (spec v7.13 "audio
   preview UX"): POST /api/projects/{pid}/audio-preview-at with the player's
   current EDL cursor position (window.EditorUI.player.currentEdlTime())
   generates a short (~8s) A/B sample of the current program audio through
   the SAME enhance chain render.py uses, and a single <audio> element plus
   an "Original ⇄ Mejorado" toggle lets the user compare without exporting.
   Also still has the older clip-picker A/B preview row (two <audio> players
   loading a 10s original vs. enhanced+EQ'd sample from
   POST /api/projects/{pid}/audio-preview), plus an
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

function _audioEqIsFlat(gains) {
  return !Array.isArray(gains) || gains.every((g) => Math.abs(Number(g) || 0) < 1e-6);
}

/* ---------- STUTTER-FIX root-cause note (reproduced by code walkthrough) ----------
   Draft playback ("se ralla"/hangs at segment transitions, first clip dead
   until the user leaves+reenters the project, breaks again on the very next
   cross-clip auto-advance) was NOT a player.js regression — player.js's
   segment-swap path (mount/_loadSegment/_advance/_preloadNext) is unchanged
   and correct. It was this file:

   render() used to call `this._ensureLiveEqGraph()` UNCONDITIONALLY, on
   every single render — and this panel is rendered eagerly and unconditionally
   by ui/editor/inspector.js's Inspector.mount() -> renderColorAudio() (ALL
   inspector tabs pre-render, just hidden) AND again on every
   EditorUI.onProjectRefreshed() (ui/editor/state.js), which fires on
   literally every project open and every background job completion — none
   of which are user gestures.

   _ensureLiveEqGraph() built a real `new AudioContext()` and called
   `createMediaElementSource()` on #video-a AND #video-b (the two
   double-buffered elements player.js swaps between for gapless cross-clip
   playback) the very first time this ran — i.e. at project-open, cold,
   before any click. Per browser autoplay policy a context created outside a
   user-gesture call stack starts (and, since our resume() call is likewise
   not inside a gesture's synchronous stack at that point, STAYS) "suspended".

   Once createMediaElementSource() has been called on a <video>, that
   element's audio is PERMANENTLY rerouted through the WebAudio graph — there
   is no way back to native routing. With the destination never pulling
   samples (context suspended), Chromium/WebKit backpressure the media
   element's own decode pipeline waiting on the stalled graph — which doesn't
   just silence audio, it visibly stutters/stalls the <video>'s playback
   too. That is the "se ralla" / hang. It reproduces on BOTH video-a and
   video-b identically (both get wired at the same cold mount), which is why
   it also breaks the very first cross-clip auto-advance (_advance()'s
   doSwap() swaps to the OTHER element, already wired to the same suspended
   graph) — not a separate bug in the swap logic.

   "Leave and re-enter the project" only appeared to fix it because opening a
   project from a click IS a user gesture — Inspector.mount() (and thus
   _ensureLiveEqGraph()'s resume() call) then runs synchronously inside that
   click's call stack, so resume() actually succeeds that time. It's a
   coincidence of timing, not a real fix.

   Fix (this file): build the AudioContext/graph LAZILY — only when the
   project's EQ is already non-flat on load, or the moment the user first
   touches a slider/preset/reset — and add a persistent, cheap, gesture-
   linked kicker that resumes the context on the next pointerdown/keydown/
   click anywhere, so even the "non-flat on cold load" case unsuspends on the
   user's very first interaction instead of needing a leave+reenter. When the
   EQ is flat and no graph has ever been created, render() now does nothing
   WebAudio-related at all — playback is 100% native, exactly as before the
   EQ feature existed. */
function _audioEqWireGestureResume() {
  if (window.__audioEqGestureResumeWired) return;
  window.__audioEqGestureResumeWired = true;
  const tryResume = () => {
    const ctx = window.AudioPanel?._audioCtx;
    if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  };
  // Capture phase, passive: cheap no-op until a graph actually exists, and
  // never interferes with the app's own gesture handling either way.
  window.addEventListener("pointerdown", tryResume, { capture: true, passive: true });
  window.addEventListener("keydown", tryResume, { capture: true, passive: true });
}
_audioEqWireGestureResume();

window.AudioPanel = window.AudioPanel || {
  _preview: null, // { clipId, original_url, enhanced_url } | null
  _eqGains: null, // current 8 gains (live working copy, pre-save)
  _eqSaveTimer: null,

  // ---------- on-demand "probar desde el cursor" enhance preview ----------
  // "Enhance voice" is neural (DeepFilterNet3) and can't run live like the
  // 8-band EQ does -- it only ever audibly applies on a real render/export.
  // This is the on-demand fix: POST /api/projects/{pid}/audio-preview-at
  // with the player's current EDL cursor position generates a short A/B
  // sample (same enhance chain as render) so the user can hear it without
  // exporting. _cursorPreview shape while idle: null. While loading:
  // { loading: true, mode }. Once loaded:
  // { loading: false, mode: "enhanced"|"original", start_s, duration_s,
  //   original_url, enhanced_url, _autoplay }. On error:
  // { loading: false, error }.
  _cursorPreview: null,

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

    // Teardown: about to blow away container.innerHTML below, which would
    // otherwise leave any currently-playing cursor-preview <audio> element
    // orphaned/detached-but-still-playing in some browsers. Pause it first.
    const prevCursorAudioEl = container.querySelector("#audio-cursor-preview-player");
    if (prevCursorAudioEl) {
      try {
        prevCursorAudioEl.pause();
      } catch (e) {
        // ignore
      }
    }

    // Bug fix: _eqGains/_preview are a module-level singleton that used to
    // be seeded once ever (`if (!this._eqGains)`) and never revisited — the
    // SPA never reloads on project switch, so opening project B after A
    // kept showing AND live-applying A's EQ gains to B's playback until the
    // user happened to touch a slider. Reset both whenever the project
    // we're rendering for actually changed.
    if (this._eqProjectId !== project.id) {
      this._eqProjectId = project.id;
      this._eqGains = _audioEqNormalizeGains(project.audio_eq);
      this._preview = null;
      this._cursorPreview = null;
    } else if (!this._eqGains) {
      this._eqGains = _audioEqNormalizeGains(project.audio_eq);
    }
    const gains = this._eqGains;

    // LAZY by design (see the root-cause note above): only wire the WebAudio
    // graph here if it already exists (keep it in sync with current gains)
    // or the project's saved EQ is non-flat. A flat EQ with no prior graph
    // means this render() call touches NOTHING WebAudio-related — playback
    // stays 100% native.
    if (this._audioCtx || !_audioEqIsFlat(gains)) this._ensureLiveEqGraph();

    const clipOptions = clips
      .map((c) => `<option value="${c.id}">${esc(c.filename || c.path)}</option>`)
      .join("");

    // ---------- cursor-position enhance preview markup ----------
    const cp = this._cursorPreview;
    let cursorStatusHtml = "";
    let cursorPlayerHtml = "";
    if (cp && cp.loading) {
      cursorStatusHtml = `<span class="dim">Generando muestra…</span>`;
    } else if (cp && cp.error) {
      cursorStatusHtml = `<span class="dim" style="color:#c00">${esc(cp.error)}</span>`;
    } else if (cp && cp.original_url && cp.enhanced_url) {
      const autoplayThisRender = !!cp._autoplay;
      cp._autoplay = false; // consume once — a later unrelated re-render must not replay it
      const src = cp.mode === "original" ? cp.original_url : cp.enhanced_url;
      const modeLabel = cp.mode === "original" ? "Original" : "Mejorado";
      cursorPlayerHtml = `
        <div class="row" style="margin-top:8px;gap:8px;align-items:center">
          <button class="btn small" id="audio-cursor-ab-toggle">Original ⇄ Mejorado</button>
          <span class="dim">Reproduciendo: ${modeLabel} · desde ${cp.start_s.toFixed(1)}s (${cp.duration_s.toFixed(1)}s)</span>
        </div>
        <div class="row" style="margin-top:6px">
          <audio controls id="audio-cursor-preview-player" src="${src}" ${autoplayThisRender ? "autoplay" : ""}></audio>
        </div>`;
    }

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
        <div class="hint">
          El realce solo se aplica al render/exportación final — no se oye en Draft.
          Usa "Probar (desde el cursor)" para escucharlo en el editor sin exportar.
        </div>
        <div class="row" style="margin-top:8px;gap:8px;align-items:center">
          <button class="btn small" id="audio-cursor-preview-btn">Probar (desde el cursor)</button>
          ${cursorStatusHtml}
        </div>
        ${cursorPlayerHtml}
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
          showToast(e.message);
        }
      };
    }

    const cursorBtn = container.querySelector("#audio-cursor-preview-btn");
    if (cursorBtn) {
      cursorBtn.onclick = async () => {
        const t = window.EditorUI?.player?.currentEdlTime?.() ?? 0;
        const prevMode = (this._cursorPreview && this._cursorPreview.mode) || "enhanced";
        this._cursorPreview = { loading: true, mode: prevMode };
        window.AudioPanel.render(container, project, refresh);
        try {
          const res = await api(`/projects/${state.pid}/audio-preview-at`, {
            method: "POST",
            body: { start_s: t, duration_s: 8 },
          });
          window.AudioPanel._cursorPreview = {
            loading: false,
            mode: "enhanced",
            start_s: res.start_s,
            duration_s: res.duration_s,
            original_url: res.original_url,
            enhanced_url: res.enhanced_url,
            _autoplay: true,
          };
        } catch (e) {
          window.AudioPanel._cursorPreview = { loading: false, error: e.message };
        }
        window.AudioPanel.render(container, project, refresh);
      };
    }

    const cursorAbToggle = container.querySelector("#audio-cursor-ab-toggle");
    if (cursorAbToggle) {
      cursorAbToggle.onclick = () => {
        const current = window.AudioPanel._cursorPreview;
        if (!current || current.loading) return;
        current.mode = current.mode === "original" ? "enhanced" : "original";
        current._autoplay = true;
        window.AudioPanel.render(container, project, refresh);
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
          showToast(e.message);
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
        // Touching a slider is a real user gesture (this handler runs
        // synchronously inside the browser's trusted "input" dispatch) — the
        // exact trigger the lazy-creation policy is waiting for. Safe/
        // idempotent if a graph already exists.
        this._ensureLiveEqGraph();
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
          // Same reasoning as the slider: a preset/reset click is a genuine
          // gesture, in the trusted synchronous "click" call stack.
          this._ensureLiveEqGraph();
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
