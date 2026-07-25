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
     magic_video_editor/pipeline/filters.py's build_vf() — CSS has no
     colorbalance/colorchannelmixer/curves equivalent, so presets are
     mapped to the nearest CSS primitives and the sliders reuse the same
     eq-style math (brightness offset, 1+contrast, 1+saturation, a
     kelvin-ish temperature) translated into CSS's multiplicative
     brightness()/contrast()/saturate()/sepia()/hue-rotate() functions. This
     is a live-preview HINT only; the exact graded look comes from the
     ffmpeg-rendered preview (§3 preview_render).
   - Every public method is try/catch'd so a bug here can never blank the
     rest of the app; on an unrecoverable error we tear ourselves down.

   v5.14 regression audit (owner-mandated code walkthrough, no bug found in
   this file, one latent correctness gap fixed while auditing):
     - activate()/deactivate() only add/remove OUR OWN overlay elements and
       read `state.project`/the DOM — neither ever calls .pause(), .load(),
       or reassigns .src/.currentTime on player.js's own <video> elements
       (#video-a/#video-b/#video-preview). Toggling the Color tab on/off
       therefore cannot pause or detach the draft/preview videos player.js
       is driving; _sync()'s writes are all confined to our own
       `this.overlayVideo`.
     - Gap found + fixed: _sync()'s source lookup was hardcoded to
       `.player-video.active`, which is the CSS class player.js toggles only
       between #video-a/#video-b (draft mode) and never touches for
       #video-preview. With the Color tab active while Player.mode ===
       "preview", this looked up a hidden, possibly stopped draft video
       instead of the visible preview video, so the divider would silently
       mirror the wrong (frozen) source. Fixed by picking whichever
       `.player-video` is actually VISIBLE (not our own compare-video),
       which tracks either mode correctly without this file needing to know
       about Player.mode at all (still fully decoupled from player.js). */

window.EditorUI = window.EditorUI || {};

function _clampNum(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// v5.7: professional color schema (see magic_video_editor/pipeline/filters.py).
// Mirrors just enough of PRESET_PARAMS/_resolve_preset_base to give the CSS
// approximation the SAME resolved numbers the server would render with —
// preset baselines apply only to sliders the user hasn't moved off their
// neutral (0) default, exactly like the Python side. Kept intentionally
// tiny (no LUTs, no colorbalance/colorlevels-exact math — impossible in
// CSS); LUTs are never approximated here, see the note element instead.
const _CMP_SLIDER_KEYS = [
  "exposure", "temperature", "tint", "black_point", "white_point",
  "brightness", "contrast", "saturation", "vibrance", "sharpness",
];
const _CMP_PRESET_PARAMS = {
  none: {},
  bw: { saturation: -1.0 },
  sepia: { saturation: -0.5, temperature: 0.6, tint: 0.1 },
  cinematic: {
    temperature: -0.15, tint: 0.05, black_point: 0.03, white_point: 0.03,
    saturation: -0.1, vibrance: 0.15,
  },
  vintage: { black_point: 0.07, white_point: 0.07, saturation: -0.25, temperature: 0.35 },
};

function _cmpResolve(cfg) {
  const resolved = {};
  _CMP_SLIDER_KEYS.forEach((k) => (resolved[k] = 0));
  Object.assign(resolved, _CMP_PRESET_PARAMS[cfg.preset || "none"] || {});
  _CMP_SLIDER_KEYS.forEach((k) => {
    const v = Number(cfg[k]);
    if (v) resolved[k] = v; // explicit (non-zero) user value always wins over the preset baseline
  });
  return resolved;
}

function _buildColorFilterCSS(cfg) {
  cfg = cfg || {};
  const r = _cmpResolve(cfg);
  const parts = [];

  // exposure: photographic stops double the light per +1 EV; CSS
  // brightness() is a plain multiplier, so 2**EV lines up naturally.
  // brightness/black_point (shadow lift) fold into the same multiplier.
  const brightnessMult = Math.pow(2, r.exposure) * (1 + r.brightness) * (1 + r.black_point * 0.3);
  parts.push(`brightness(${_clampNum(brightnessMult, 0.15, 4).toFixed(3)})`);

  // contrast + the levels squeeze (black_point/white_point) both tighten
  // the tonal range, so they compound into one contrast() call.
  const contrastMult = (1 + r.contrast) * (1 + (r.black_point + r.white_point) * 1.4);
  parts.push(`contrast(${_clampNum(contrastMult, 0.2, 3).toFixed(3)})`);

  // saturation + vibrance (vibrance is the "smart"/skin-protecting version —
  // CSS has no per-hue selectivity, so it just contributes at reduced
  // weight rather than pretending to protect skin tones).
  const saturateMult = (1 + r.saturation) * (1 + r.vibrance * 0.5);
  parts.push(`saturate(${_clampNum(saturateMult, 0, 3).toFixed(3)})`);

  // temperature: +1 (warm, ~3500K) -> a touch of sepia + orange hue-rotate;
  // -1 (cool, ~8500K) -> a blue-ish hue-rotate the other way.
  if (r.temperature) {
    const t = _clampNum(r.temperature, -1, 1);
    if (t > 0) parts.push(`sepia(${_clampNum(t * 0.3, 0, 0.4).toFixed(3)})`, `hue-rotate(${(-t * 6).toFixed(1)}deg)`);
    else parts.push(`hue-rotate(${(-t * 12).toFixed(1)}deg)`);
  }
  // tint: magenta<->green on the CSS hue wheel — a small hue-rotate nudge in
  // the opposite direction from temperature's warm/cool axis.
  if (r.tint) parts.push(`hue-rotate(${(r.tint * 10).toFixed(1)}deg)`);

  // sharpness has no CSS filter equivalent at all — intentionally not
  // approximated (see the Color panel's Detail group hint).

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
      // v5.7: LUTs can't be approximated in CSS at all (no lut3d/haldclut
      // equivalent) — say so instead of silently showing an incomplete
      // grade. The Color panel's own debounced preview-frame image is
      // where the exact LUT'd look actually shows up.
      const lutActive = !!(this.cfg?.lut?.name) && Number(this.cfg?.lut?.intensity ?? 1) > 0;
      if (this._lutNote) {
        this._lutNote.hidden = !lutActive;
        this._lutNote.textContent = lutActive
          ? "LUT active — this is an approximation; see Exact preview in the Color panel."
          : "";
      }
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

      // Inline-styled (not a shared CSS class) so this file stays fully
      // self-contained -- ui/index.html/style.css belong to other agents
      // this phase, same rule the top-of-file note already states.
      this._lutNote = document.createElement("div");
      this._lutNote.hidden = true;
      Object.assign(this._lutNote.style, {
        position: "absolute", top: "8px", left: "50%", transform: "translateX(-50%)",
        zIndex: "7", fontSize: "11px", color: "#fff", background: "rgba(0,0,0,.65)",
        border: "1px solid var(--accent2, #35c28f)", borderRadius: "999px",
        padding: "3px 10px", pointerEvents: "none", whiteSpace: "nowrap",
        maxWidth: "90%", overflow: "hidden", textOverflow: "ellipsis",
      });

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
      this.wrap.appendChild(this._lutNote);
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
    this._lutNote = null;
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
    // Pick whichever player.js video is actually visible right now (draft's
    // video-a/video-b OR video-preview) rather than assuming draft mode via
    // the "active" class — see the v5.14 audit note at the top of this
    // file. Never touches player.js's elements beyond reading them.
    const src = Array.from(document.querySelectorAll("#player-stage .player-video"))
      .find((v) => v !== this.overlayVideo && v.style.visibility !== "hidden");
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
