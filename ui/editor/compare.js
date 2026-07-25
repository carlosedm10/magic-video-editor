/* On-viewer before/after comparison divider for the Color inspector tab
   (docs/PLATFORM-SPEC.md v4 §4). While the Color tab is active a vertical
   draggable divider overlays #player-stage: everything LEFT of the divider
   shows the plain source (the existing player-video elements showing
   through, unfiltered); everything RIGHT of the divider shows a graded
   clone with a CSS `filter` approximation of project["color"].

   Implementation notes (per this task's brief):
   - We never touch player.js internals. We only (a) read whichever
     `.player-video` element currently has the `active` class — the same
     element player.js is driving — for its `src`/`currentTime`/paused
     state, and (b) add/remove our own overlay elements inside
     #player-stage. player.js is never called or mutated.
   - The overlay is a second <video> (muted, our own element) mirrored
     frame-by-frame from the active player video, clipped with
     `clip-path: inset(0 0 0 X%)` to only show its right side (X = divider
     position). Because the underlying (unfiltered) active video is fully
     visible beneath it, the left side naturally shows "original" through
     the gap.
   - The graded look is a CSS `filter` APPROXIMATION of
     cutroom/pipeline/filters.py's build_vf() — CSS has no
     colorbalance/colorchannelmixer/curves equivalent, so presets are
     mapped to the nearest CSS primitives and the sliders reuse the same
     eq-style math (brightness offset, 1+contrast, 1+saturation, a
     kelvin-ish temperature) translated into CSS's multiplicative
     brightness()/contrast()/saturate()/sepia()/hue-rotate() functions. This
     is a live-preview HINT only; the exact graded look comes from the
     ffmpeg-rendered preview (§3 preview_render).
   - Every public method is try/catch'd so a bug here can never blank the
     rest of the app; on an unrecoverable error we tear ourselves down. */

window.EditorUI = window.EditorUI || {};

function _clampNum(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function _buildColorFilterCSS(cfg) {
  cfg = cfg || {};
  const preset = cfg.preset || "none";
  const b = Number(cfg.brightness) || 0;
  const c = Number(cfg.contrast) || 0;
  const s = Number(cfg.saturation) || 0;
  const t = Number(cfg.temperature) || 0;
  const parts = [];

  // Presets: nearest CSS approximation of _preset_vf() in filters.py.
  switch (preset) {
    case "bw":
      parts.push("grayscale(1)", "contrast(1.08)");
      break;
    case "sepia":
      parts.push("sepia(0.85)");
      break;
    case "cinematic":
      // Real filter splits shadows/highlights (colorbalance); CSS can't
      // split by luminance, so approximate the overall teal/orange push.
      parts.push("saturate(1.15)", "contrast(1.06)", "hue-rotate(-6deg)");
      break;
    case "vintage":
      // Real filter lifts blacks (curves) + desaturates 25% + ~5800K warm.
      parts.push("sepia(0.25)", "saturate(0.78)", "contrast(0.92)", "brightness(1.04)");
      break;
    default:
      break;
  }

  // Sliders: ffmpeg's `eq` brightness is an additive offset and
  // contrast/saturation are 1+value multipliers — CSS brightness()/
  // contrast()/saturate() are multiplicative, so `1 + value` lines up well
  // enough for a live hint.
  parts.push(`brightness(${_clampNum(1 + b, 0.2, 2).toFixed(3)})`);
  parts.push(`contrast(${_clampNum(1 + c, 0.2, 2.5).toFixed(3)})`);
  parts.push(`saturate(${_clampNum(1 + s, 0, 2.5).toFixed(3)})`);

  if (t) {
    // Real filter maps t=+1 -> 3500K (warm) / t=-1 -> 9500K (cool) via
    // colortemperature. CSS has no color-temperature primitive: warm pushes
    // a touch of sepia + an orange-ish hue-rotate, cool pushes a blue-ish
    // hue-rotate the other way.
    if (t > 0) {
      parts.push(`sepia(${_clampNum(t * 0.3, 0, 0.4).toFixed(3)})`, `hue-rotate(${(-t * 6).toFixed(1)}deg)`);
    } else {
      parts.push(`hue-rotate(${(-t * 12).toFixed(1)}deg)`, `saturate(${_clampNum(1 + -t * 0.1, 1, 1.3).toFixed(3)})`);
    }
  }

  return parts.join(" ");
}

const Compare = {
  active: false,
  wrap: null,
  overlayVideo: null,
  divider: null,
  pos: 55, // percent from the left
  cfg: null,
  _raf: null,
  _dragMove: null,
  _dragUp: null,

  setLiveConfig(cfg) {
    this.cfg = cfg || this.cfg;
    if (!this.active || !this.overlayVideo) return;
    try {
      this.overlayVideo.style.filter = _buildColorFilterCSS(this.cfg);
    } catch (e) {
      console.error("compare: failed to apply filter", e);
    }
  },

  activate(project) {
    try {
      if (this.active) {
        this.setLiveConfig((project && project.color) || this.cfg);
        return;
      }
      const stage = document.getElementById("player-stage");
      if (!stage) return;

      this.wrap = document.createElement("div");
      this.wrap.className = "compare-overlay";
      this.wrap.id = "compare-overlay";

      this.overlayVideo = document.createElement("video");
      this.overlayVideo.className = "compare-video";
      this.overlayVideo.muted = true;
      this.overlayVideo.playsInline = true;
      this.overlayVideo.preload = "auto";
      this.overlayVideo.onerror = () => console.error("compare: overlay video error");

      const labelLeft = document.createElement("div");
      labelLeft.className = "compare-label left";
      labelLeft.textContent = "Original";
      const labelRight = document.createElement("div");
      labelRight.className = "compare-label right";
      labelRight.textContent = "Graded";

      this.divider = document.createElement("div");
      this.divider.className = "compare-divider";
      const handle = document.createElement("div");
      handle.className = "compare-handle";
      handle.innerHTML = '<i data-lucide="move-horizontal"></i>';
      this.divider.appendChild(handle);
      try { window.refreshIcons?.(); } catch (_e) { /* ignore */ }

      this.wrap.appendChild(this.overlayVideo);
      this.wrap.appendChild(labelLeft);
      this.wrap.appendChild(labelRight);
      this.wrap.appendChild(this.divider);
      stage.appendChild(this.wrap);

      this._wireDrag(stage);
      this.active = true;
      this._applyClip();
      this.setLiveConfig((project && project.color) || this.cfg);
      this._startLoop();
    } catch (e) {
      console.error("compare: failed to activate overlay", e);
      this.deactivate();
    }
  },

  deactivate() {
    this.active = false;
    try { if (this._raf) cancelAnimationFrame(this._raf); } catch (_e) { /* ignore */ }
    this._raf = null;
    try {
      if (this.divider) {
        if (this._dragMove) window.removeEventListener("mousemove", this._dragMove);
        if (this._dragUp) window.removeEventListener("mouseup", this._dragUp);
        if (this._dragMove) window.removeEventListener("touchmove", this._dragMove);
        if (this._dragUp) window.removeEventListener("touchend", this._dragUp);
      }
    } catch (_e) { /* ignore */ }
    try { this.wrap?.remove(); } catch (_e) { /* ignore */ }
    this.wrap = null;
    this.overlayVideo = null;
    this.divider = null;
  },

  _applyClip() {
    if (!this.overlayVideo || !this.divider) return;
    this.overlayVideo.style.clipPath = `inset(0 0 0 ${this.pos}%)`;
    this.divider.style.left = `${this.pos}%`;
  },

  _wireDrag(stage) {
    const move = (clientX) => {
      const rect = stage.getBoundingClientRect();
      if (!rect.width) return;
      this.pos = _clampNum(((clientX - rect.left) / rect.width) * 100, 2, 98);
      this._applyClip();
    };
    this._dragMove = (ev) => {
      try {
        const x = ev.touches ? ev.touches[0].clientX : ev.clientX;
        move(x);
      } catch (e) { console.error("compare: drag move failed", e); }
    };
    this._dragUp = () => {
      window.removeEventListener("mousemove", this._dragMove);
      window.removeEventListener("mouseup", this._dragUp);
      window.removeEventListener("touchmove", this._dragMove);
      window.removeEventListener("touchend", this._dragUp);
    };
    const down = (ev) => {
      try {
        ev.preventDefault();
        window.addEventListener("mousemove", this._dragMove);
        window.addEventListener("mouseup", this._dragUp);
        window.addEventListener("touchmove", this._dragMove, { passive: false });
        window.addEventListener("touchend", this._dragUp);
      } catch (e) { console.error("compare: drag start failed", e); }
    };
    this.divider.addEventListener("mousedown", down);
    this.divider.addEventListener("touchstart", down, { passive: false });
  },

  _startLoop() {
    const tick = () => {
      if (!this.active) return;
      try { this._sync(); } catch (e) {
        console.error("compare: sync loop failed, tearing down overlay", e);
        this.deactivate();
        return;
      }
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  },

  _sync() {
    const src = document.querySelector("#player-stage .player-video.active");
    const ov = this.overlayVideo;
    if (!src || !ov) return;
    if (!src.getAttribute("src") && !src.src) {
      ov.style.visibility = "hidden";
      return;
    }
    ov.style.visibility = "visible";
    if (ov.dataset.mirrorSrc !== src.src) {
      ov.dataset.mirrorSrc = src.src;
      ov.src = src.src;
    }
    if (ov.readyState >= 1 && Math.abs(ov.currentTime - src.currentTime) > 0.12) {
      try { ov.currentTime = src.currentTime; } catch (_e) { /* metadata not ready yet */ }
    }
    if (src.paused && !ov.paused) ov.pause();
    else if (!src.paused && ov.paused) ov.play().catch(() => {});
  },
};

window.EditorUI.compare = Compare;
