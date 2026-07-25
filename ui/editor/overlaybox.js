/* Manual overlay track — player-stage half (spec v5.9b): for every
   project["overlays"] item, an absolutely-positioned MUTED <video> synced to
   the EDL clock is appended into #player-stage (owned by ui/editor/player.js
   — this module only ever ADDS/REMOVES its own elements there, never
   touches player.js's own video-a/video-b/subtitle-overlay/fade-overlay
   elements, and guards every touch point so a player.js internals change
   can never throw here). When an overlay is selected (Editor.overlaySelected,
   set by ui/editor/timeline.js's overlay track), a draggable/resizable
   bounding box + an opacity slider chip appear over it — move = drag the
   box body (x/y), corners = drag to resize (scale, anchored on the
   OPPOSITE corner, the standard PiP gesture every editor uses).

   Overlays only ever render in Draft mode (window.EditorUI.player.mode ===
   "draft") — the rendered preview/final files already bake overlays in via
   the ffmpeg pass (spec v5.9b "Render"), so showing our own live copy on
   top of a Preview-mode playback would double them up.

   One shared requestAnimationFrame loop (mount() starts it once, globally)
   keeps every overlay video's currentTime/play-state in sync with
   window.EditorUI.player.currentEdlTime()/`.playing` and repositions the
   bounding box — same "poll the player every frame" shape player.js itself
   uses for its own subtitle overlay / playhead sync, kept fully independent
   here so a bug in this module can never stall playback. */

window.EditorUI = window.EditorUI || {};

const MIN_OV_SCALE = 0.02;
const MAX_OV_SCALE = 1.0;

const OverlayBox = {
  _rafId: null,
  _videoEls: new Map(),   // overlay id -> <video>
  _pid: null,
  _bbox: null,
  _chip: null,
  _dragging: false,

  mount() {
    this._injectStyles();
    if (!this._rafId) this._startLoop();
  },

  _injectStyles() {
    if (document.getElementById("ovbox-styles")) return;
    const style = document.createElement("style");
    style.id = "ovbox-styles";
    style.textContent = `
      .ov-preview-video { position: absolute; object-fit: cover; pointer-events: none;
        z-index: 3; display: none; background: transparent; }
      #ov-bbox { position: absolute; z-index: 8; border: 2px dashed var(--accent2, #35c28f);
        cursor: move; display: none; box-sizing: border-box; }
      #ov-bbox .ov-handle { position: absolute; width: 12px; height: 12px; border-radius: 3px;
        background: var(--accent2, #35c28f); border: 1px solid #05070d; cursor: nwse-resize; }
      #ov-bbox .ov-handle.tl { left: -6px; top: -6px; cursor: nwse-resize; }
      #ov-bbox .ov-handle.tr { right: -6px; top: -6px; cursor: nesw-resize; }
      #ov-bbox .ov-handle.bl { left: -6px; bottom: -6px; cursor: nesw-resize; }
      #ov-bbox .ov-handle.br { right: -6px; bottom: -6px; cursor: nwse-resize; }
      #ov-chip { position: absolute; z-index: 9; display: none; align-items: center; gap: 6px;
        background: var(--panel, #0a101dcc); border: 1px solid var(--border, #1c2333);
        border-radius: 999px; padding: 4px 8px; backdrop-filter: blur(12px); pointer-events: auto; }
      #ov-chip input[type=range] { width: 74px; }
      #ov-chip .ov-chip-del { background: none; border: none; color: var(--danger, #e5534b);
        cursor: pointer; font-size: 12px; padding: 2px 4px; line-height: 1; }
    `;
    document.head.appendChild(style);
  },

  _ensureChrome(stage) {
    if (!this._bbox) {
      this._bbox = document.createElement("div");
      this._bbox.id = "ov-bbox";
      ["tl", "tr", "bl", "br"].forEach((corner) => {
        const h = document.createElement("div");
        h.className = `ov-handle ${corner}`;
        h.dataset.corner = corner;
        h.addEventListener("pointerdown", (e) => this._onHandlePointerDown(e, corner));
        this._bbox.appendChild(h);
      });
      this._bbox.addEventListener("pointerdown", (e) => {
        if (e.target.classList.contains("ov-handle")) return;
        this._onMovePointerDown(e);
      });
      stage.appendChild(this._bbox);
    }
    if (!this._chip) {
      this._chip = document.createElement("div");
      this._chip.id = "ov-chip";
      const label = document.createElement("span");
      label.className = "dim";
      label.textContent = "Opacity";
      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = "0"; slider.max = "1"; slider.step = "0.01";
      slider.id = "ov-chip-opacity";
      slider.oninput = () => {
        const id = Editor.overlaySelected;
        if (id) Editor.overlayOpacity(id, Number(slider.value));
      };
      const del = document.createElement("button");
      del.className = "ov-chip-del";
      del.title = "Delete overlay";
      del.innerHTML = '<i data-lucide="trash-2"></i>';
      del.onclick = () => {
        const id = Editor.overlaySelected;
        if (id) Editor.deleteOverlay(id);
      };
      this._chip.appendChild(label);
      this._chip.appendChild(slider);
      this._chip.appendChild(del);
      stage.appendChild(this._chip);
      try { refreshIcons(); } catch (_e) { /* lucide not ready yet — harmless */ }
    }
  },

  /* ---------- frame geometry ----------
     #player-stage uses object-fit: contain on the main videos, so the
     rendered frame is usually letterboxed inside the stage box — overlay
     x/y/scale are fractions of that FRAME, not the outer stage element, so
     every position/size computation needs this rect. Reads (never writes)
     the currently-active main video's intrinsic size to get the source
     aspect ratio — the one piece of player.js state this module has no
     other way to learn — falling back to 16:9 before metadata loads. */
  _frameRect(stage) {
    const stageRect = stage.getBoundingClientRect();
    let mainVideo = null;
    try { mainVideo = stage.querySelector(".player-video.active"); } catch (_e) { mainVideo = null; }
    const vw = mainVideo?.videoWidth || 0;
    const vh = mainVideo?.videoHeight || 0;
    const aspect = vw > 0 && vh > 0 ? vw / vh : 16 / 9;
    const stageAspect = stageRect.width / (stageRect.height || 1);
    let w, h, offX, offY;
    if (aspect > stageAspect) {
      w = stageRect.width; h = w / aspect; offX = 0; offY = (stageRect.height - h) / 2;
    } else {
      h = stageRect.height; w = h * aspect; offY = 0; offX = (stageRect.width - w) / 2;
    }
    return { width: w, height: h, offX, offY };
  },

  _clipAspect(clipId, videoEl) {
    const vw = videoEl?.videoWidth || 0;
    const vh = videoEl?.videoHeight || 0;
    if (vw > 0 && vh > 0) return vw / vh;
    return 16 / 9;
  },

  /* ---------- main loop ---------- */
  _startLoop() {
    const tick = () => {
      try { this._tick(); } catch (e) { console.error("overlaybox tick failed", e); }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  },

  render() {
    // Immediate re-sync after a discrete edit (opacity slider, delete,
    // selection change) rather than waiting up to one rAF frame.
    try { this._tick(); } catch (e) { console.error("overlaybox render failed", e); }
  },

  _tick() {
    const stage = document.getElementById("player-stage");
    const player = window.EditorUI.player;
    if (!stage || !player) return;
    this._ensureChrome(stage);

    if (Editor.pid !== this._pid) {
      // Project switched — every previous overlay id is meaningless now.
      this._videoEls.forEach((v) => { try { v.remove(); } catch (_e) { /* already gone */ } });
      this._videoEls.clear();
      this._pid = Editor.pid;
    }

    const overlays = Editor.overlays || [];
    const draft = player.mode === "draft";
    const t = draft ? (player.currentEdlTime?.() ?? 0) : -1;

    // Reconcile video elements: one per overlay, removed when its overlay
    // (or the whole project) goes away.
    const liveIds = new Set(overlays.map((o) => o.id));
    this._videoEls.forEach((v, id) => {
      if (!liveIds.has(id)) { try { v.remove(); } catch (_e) { /* ignore */ } this._videoEls.delete(id); }
    });

    const frame = draft ? this._frameRect(stage) : null;

    overlays.forEach((o) => {
      let v = this._videoEls.get(o.id);
      if (!v) {
        v = document.createElement("video");
        v.className = "ov-preview-video";
        v.muted = true;
        v.playsInline = true;
        v.preload = "auto";
        v.onerror = () => console.error("overlay video error", o.clip_id, v.error);
        stage.insertBefore(v, this._bbox || null);
        this._videoEls.set(o.id, v);
      }

      const active = draft && t >= o.t_start && t < o.t_start + o.duration;
      if (!active) {
        v.style.display = "none";
        if (!v.paused) { try { v.pause(); } catch (_e) { /* ignore */ } }
        return;
      }

      const src = `/api/projects/${Editor.pid}/media/preview/${o.clip_id}`;
      if (v.dataset.clipId !== o.clip_id) {
        v.dataset.clipId = o.clip_id;
        v.src = src;
      }
      const localTime = Math.max(0, t - o.t_start + o.clip_in);
      if (Math.abs((v.currentTime || 0) - localTime) > 0.25) {
        try { v.currentTime = localTime; } catch (_e) { /* metadata not ready yet */ }
      }
      if (player.playing && v.paused) v.play().catch(() => {});
      else if (!player.playing && !v.paused) { try { v.pause(); } catch (_e) { /* ignore */ } }

      const ovAspect = this._clipAspect(o.clip_id, v);
      const w = o.scale * frame.width;
      const h = w / ovAspect;
      v.style.left = `${frame.offX + o.x * frame.width}px`;
      v.style.top = `${frame.offY + o.y * frame.height}px`;
      v.style.width = `${w}px`;
      v.style.height = `${h}px`;
      v.style.opacity = String(o.opacity);
      v.style.display = "block";
    });

    this._renderBBox(stage, frame, draft);
  },

  _renderBBox(stage, frame, draft) {
    const id = Editor.overlaySelected;
    const ov = id ? (Editor.overlays || []).find((o) => o.id === id) : null;
    if (!ov || !draft || !this._bbox || !this._chip) {
      if (this._bbox) this._bbox.style.display = "none";
      if (this._chip) this._chip.style.display = "none";
      return;
    }
    const v = this._videoEls.get(id);
    const ovAspect = this._clipAspect(ov.clip_id, v);
    const w = ov.scale * frame.width;
    const h = w / ovAspect;
    const left = frame.offX + ov.x * frame.width;
    const top = frame.offY + ov.y * frame.height;
    this._bbox.style.left = `${left}px`;
    this._bbox.style.top = `${top}px`;
    this._bbox.style.width = `${w}px`;
    this._bbox.style.height = `${h}px`;
    this._bbox.style.display = "block";

    const slider = document.getElementById("ov-chip-opacity");
    if (slider && document.activeElement !== slider) slider.value = String(ov.opacity);
    this._chip.style.left = `${left}px`;
    this._chip.style.top = `${Math.max(0, top - 30)}px`;
    this._chip.style.display = "flex";
  },

  /* ---------- move (drag the box body) ---------- */
  _onMovePointerDown(e) {
    e.stopPropagation();
    e.preventDefault();
    const id = Editor.overlaySelected;
    const ov = id ? (Editor.overlays || []).find((o) => o.id === id) : null;
    const stage = document.getElementById("player-stage");
    if (!id || !ov || !stage) return;
    const frame = this._frameRect(stage);
    const startX = e.clientX, startY = e.clientY;
    const startXFrac = ov.x, startYFrac = ov.y;
    this._dragging = true;
    const onMove = (ev) => {
      const dxFrac = (ev.clientX - startX) / (frame.width || 1);
      const dyFrac = (ev.clientY - startY) / (frame.height || 1);
      const x = Math.max(0, Math.min(1, startXFrac + dxFrac));
      const y = Math.max(0, Math.min(1, startYFrac + dyFrac));
      Editor.overlayTransformLive(id, { x, y });
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      this._dragging = false;
      Editor.commitOverlayEdit("Move overlay (player)");
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  /* ---------- resize (drag a corner, opposite corner stays anchored) ----------
     Standard PiP gesture: whichever corner is DIAGONALLY OPPOSITE the one
     being dragged stays fixed on screen; scale (width, as a frame fraction)
     follows the horizontal distance from that anchor to the pointer, height
     follows from the overlay clip's own aspect ratio (locked, not
     independently draggable — there is no non-uniform-stretch control). */
  _onHandlePointerDown(e, corner) {
    e.stopPropagation();
    e.preventDefault();
    const id = Editor.overlaySelected;
    const ov = id ? (Editor.overlays || []).find((o) => o.id === id) : null;
    const stage = document.getElementById("player-stage");
    if (!id || !ov || !stage) return;
    const frame = this._frameRect(stage);
    const v = this._videoEls.get(id);
    const ovAspect = this._clipAspect(ov.clip_id, v);
    const hFracPerScale = (frame.width / (frame.height || 1)) / ovAspect;
    const heightFrac = ov.scale * hFracPerScale;

    // The anchor is the corner diagonally opposite the one grabbed, fixed
    // in FRAME-fraction terms for the whole gesture.
    const anchorX = corner.includes("l") ? ov.x + ov.scale : ov.x;
    const anchorY = corner.includes("t") ? ov.y + heightFrac : ov.y;
    this._dragging = true;

    const onMove = (ev) => {
      const rect = stage.getBoundingClientRect();
      const mouseXFrac = (ev.clientX - rect.left - frame.offX) / (frame.width || 1);
      const mouseYFrac = (ev.clientY - rect.top - frame.offY) / (frame.height || 1);
      const dxFrac = mouseXFrac - anchorX;
      const newScale = Math.max(MIN_OV_SCALE, Math.min(MAX_OV_SCALE, Math.abs(dxFrac)));
      const newHeightFrac = newScale * hFracPerScale;
      const newX = dxFrac >= 0 ? anchorX : anchorX - newScale;
      const newY = mouseYFrac >= anchorY ? anchorY : anchorY - newHeightFrac;
      Editor.overlayTransformLive(id, { x: newX, y: newY, scale: newScale });
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      this._dragging = false;
      Editor.commitOverlayEdit("Resize overlay");
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },
};

window.EditorUI.overlaybox = OverlayBox;
