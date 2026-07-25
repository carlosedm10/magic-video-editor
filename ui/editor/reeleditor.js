/* Reel Editor (spec v5 + v5.8a/v5.8b UI, framing rebuilt for spec v7.11):
   full takeover of the editor area when a reel is opened for editing — fix
   framing (a {zoom, offset_x, offset_y} transform, directly manipulated ON
   the 9:16 preview stage), extend/trim the cut beyond the AI window
   (in_override/out_override, now per-segment), hand-edit subtitles
   (cue_overrides + subtitle_style), restyle, re-render THAT reel, and
   (v5.8b) build/edit multi-segment "podcast case" reels.

   Entry point: ui/tabs/reels.js's "Edit" button calls window.ReelEditor.open(rid).
   "← Back to project" / Esc calls window.ReelEditor.close().

   Server contract (magic_video_editor/api/reels.py): PATCH /api/projects/{pid}/reels/{rid}
   (in_override/out_override/transform/cue_overrides/subtitle_style/title/
   description/segments/transitions — all optional, partial; `segments` and
   `transitions` REPLACE the whole list wholesale — see that file's
   SegmentInput/TransitionInput docstrings), POST .../regenerate-copy, GET
   .../cues (GLOBAL index across every segment concatenated in order, text
   already reflects cue_overrides, each cue carries a `segment` field).
   Render itself reuses the existing POST .../render (magic_video_editor/api/pipeline.py),
   which enqueues through the queue (state.queue, kept fresh by core.js's
   global poll).

   Framing (spec v7.11 "Reel framing v2"): reel["transform"] =
   {zoom: 0.5..3.0, offset_x: -1..1, offset_y: -1..1} REPLACES the old
   {crop_x, fit_mode, fit_scale} trio -- zoom 1.0 is the classic full-height
   9:16 crop (unchanged geometry); >1 punches in; <1 opens the crop window
   wider than the frame, auto-revealing a blurred cover-fill background of
   THAT SAME crop window (never the original 16:9 frame -- that was the bug
   spec v7.11 fixes). The math (transform_crop_rect/transform_needs_blur) is
   mirrored client-side below in `_transformCropRect`/`_transformNeedsBlur`
   from magic_video_editor/pipeline/faces.py so the live CSS preview and the
   server's ffmpeg filter agree pixel-for-pixel on the geometry (mod integer
   rounding). GET /api/projects/{pid} does NOT run the server's read-time
   migration (that only happens via ensure_segments, invoked by the
   single-reel GET/PATCH/render/safety endpoints) -- so a reel this session
   hasn't touched yet may still arrive with only the legacy fields and no
   "transform" key; `_deriveTransformFromReel` mirrors that migration too
   (see pipeline/reels.py's `_normalize_transform` for the source of truth).
   No API exposes the actual face-detected crop center used at render time,
   so the live preview assumes the same (0.5, 0.45) frame-relative fallback
   `transform_crop_rect` uses when no face was found -- an approximation,
   not pixel-identical to a reel whose face detection found an off-center
   speaker, same spirit as the old fit_blur ghost-video preview this
   replaces.

   Multi-segment data model (spec v5.8b, magic_video_editor/pipeline/reels.py):
   reel.segments = [{clip_id, start, end, in_override, out_override}], one
   entry per segment (a pre-v5.8b single-window reel is migrated to a
   1-segment list server-side on read); reel.transitions = junction
   transitions between consecutive segments (len == segments.length - 1,
   default {type:"crossfade", duration:0.4}). reel.composed is true for
   reels the viral composer built from a combined pair (surfaced as a
   "Compuesto" badge both here and on the Reels tab grid).

   Everything in this file is wrapped in one IIFE and exposes exactly one
   global (window.ReelEditor) so it can never collide with a top-level
   `const`/`let` name declared by any other <script> tag sharing this page's
   script-global scope (a real hazard in this codebase: e.g. redeclaring
   `const Editor` in a second <script> would be a page-breaking SyntaxError).
   Everything else it needs from the rest of the app (api, esc, fmtT, $,
   state, pollQueue, closeDrawer, closeSettings) is read as a free variable
   resolved against that same shared script-global scope — the established
   pattern every other ui/editor/*.js file already relies on.

   DOM note: like ui/editor/timeline.js and ui/editor/inspector.js, this
   module builds its own container (appended into #content, a sibling of
   #project-view/#empty-state) and injects its own <style> tag at first open
   — ui/index.html only gets one new <script> line for this file, per this
   task's file-ownership brief. */

(function () {
  let _fontsCache = null; // GET /fonts is project-independent; fetch once per session

  function _effectiveWindowFor(reelLike, clipDuration, dragPreview) {
    if (dragPreview) return dragPreview;
    let duration = clipDuration;
    if (!duration || duration <= 0) duration = Math.max(reelLike.end || 0, reelLike.out_override || 0) || 1;
    let start = reelLike.in_override;
    start = start == null ? reelLike.start : Number(start);
    let end = reelLike.out_override;
    end = end == null ? reelLike.end : Number(end);
    start = Math.max(0, Math.min(start, duration));
    end = Math.max(start + 0.05, Math.min(end, duration));
    return { start, end };
  }

  /* Defensive against a real backend data bug observed live against project
     c7642fc7755e: magic_video_editor/pipeline/reels.py does `list(copy.get("hashtags") or [])`
     but copywriter.py's copy_for_reel returns "hashtags" as a space-joined
     STRING, not a list -- Python's list("#a #b") explodes it per-character.
     Not this file's bug to fix; just don't render 50+ one-letter pills. */
  function _validHashtags(tags) {
    return (tags || []).filter((h) => typeof h === "string" && h.replace(/^#/, "").trim().length > 1);
  }
  function _hashtagText(tags) {
    return _validHashtags(tags).map((h) => (h.startsWith("#") ? h : `#${h}`)).join(" ");
  }

  async function _copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_e) {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand("copy");
        ta.remove();
        return true;
      } catch (_e2) {
        return false;
      }
    }
  }

  /* ---------- transform model (spec v7.11) — client mirror of
     magic_video_editor/pipeline/faces.py's transform_crop_rect/
     transform_needs_blur. See this file's top docstring for why a mirror
     (rather than a server round-trip) is needed for a live preview. ---- */

  const TRANSFORM_ZOOM_MIN = 0.5, TRANSFORM_ZOOM_MAX = 3.0;
  const TRANSFORM_ASSUMED_CENTER = { x: 0.5, y: 0.45 }; // same fallback transform_crop_rect uses when no face was found

  function _clampNum(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  // (x, y, w, h) of the source-frame crop window in source pixels, plus
  // roomX/roomY (how far the window can still pan) and cx/cy (the assumed
  // center in source pixels) so callers can invert a dragged/zoomed pixel
  // position back into an offset_x/offset_y fraction. Mirrors
  // faces.transform_crop_rect bit-for-bit (see that function's docstring
  // for the derivation) — out_w/out_h default to the reel render's fixed
  // 1080x1920 (magic_video_editor/config.py's REEL_W/REEL_H), only the
  // 9:16 ratio matters here, not the absolute pixel size.
  function _transformCropRect(srcW, srcH, zoom, offsetX, offsetY, outW, outH) {
    outW = outW || 1080; outH = outH || 1920;
    const targetAr = outW / outH;
    let baseH = srcH, baseW = Math.round(baseH * targetAr);
    if (baseW > srcW) { baseW = srcW; baseH = Math.round(baseW / targetAr); }

    const z = _clampNum(Number(zoom) || 1, TRANSFORM_ZOOM_MIN, TRANSFORM_ZOOM_MAX);
    const cropW = _clampNum(Math.round(baseW / z), 2, srcW);
    const cropH = _clampNum(Math.round(baseH / z), 2, srcH);

    const cx = TRANSFORM_ASSUMED_CENTER.x * srcW, cy = TRANSFORM_ASSUMED_CENTER.y * srcH;
    const ox = _clampNum(Number(offsetX) || 0, -1, 1), oy = _clampNum(Number(offsetY) || 0, -1, 1);
    const roomX = Math.max(0, (srcW - cropW) / 2), roomY = Math.max(0, (srcH - cropH) / 2);

    let x = cx - cropW / 2 + ox * roomX;
    let y = cy - cropH / 2 + oy * roomY;
    x = _clampNum(x, 0, srcW - cropW);
    y = _clampNum(y, 0, srcH - cropH);
    return { x, y, w: cropW, h: cropH, roomX, roomY, cx, cy };
  }

  // zoom < 1.0 (the "cover threshold", spec v7.11) opens the crop window
  // wider than the 9:16 frame -> needs the blurred cover background. See
  // faces.transform_needs_blur's docstring for why this compares the raw
  // zoom rather than the rounded crop_w/crop_h ratio.
  function _transformNeedsBlur(zoom) { return Number(zoom) < 1 - 1e-9; }

  // Read-time migration mirror of pipeline/reels.py's `_normalize_transform`
  // (spec v7.11 "migration on read") — needed because GET /api/projects/
  // {pid} does not itself run that migration (see top docstring); once a
  // reel HAS a "transform" key (after any PATCH/render/safety-check this
  // session, or if it was created post-migration), that's used as-is,
  // clamped, and the legacy fields are never consulted again — same
  // idempotence guarantee the server-side function documents.
  function _deriveTransformFromReel(reel) {
    if (reel && reel.transform) {
      const t = reel.transform;
      return {
        zoom: _clampNum(Number(t.zoom != null ? t.zoom : 1), TRANSFORM_ZOOM_MIN, TRANSFORM_ZOOM_MAX),
        offset_x: _clampNum(Number(t.offset_x != null ? t.offset_x : 0), -1, 1),
        offset_y: _clampNum(Number(t.offset_y != null ? t.offset_y : 0), -1, 1),
      };
    }
    let offsetX = 0;
    if (reel && reel.crop_x != null) offsetX = _clampNum((Number(reel.crop_x) - 0.5) * 2, -1, 1);
    let zoom = 1.0;
    if (reel && reel.fit_mode === "fit_blur") {
      zoom = _clampNum(Number(reel.fit_scale) || 0.82, 0.6, 1.0);
    }
    return { zoom, offset_x: offsetX, offset_y: 0 };
  }

  const SUB_STYLES = [["clean", "Clean"], ["bold", "Bold"], ["karaoke", "Karaoke"]];
  const SUB_SIZES = [["S", "S"], ["M", "M"], ["L", "L"]];
  const SUB_POSITIONS = [["bottom", "Bottom"], ["center", "Center"]];
  const TRANSITION_ORDER = ["none", "fade", "crossfade"];

  const ReelEditor = {
    pid: null,
    rid: null,
    clip: null,
    isOpen: false,
    activeTab: "reel",
    playing: false,
    cues: [],
    _activeSeg: 0,          // index of the segment currently loaded in #re-video
    _segDragPreview: null,  // {idx, start, end} while dragging a segment edge, else null
    _segThumbs: {},         // clip_id -> {meta, stripUrl, failed} (one entry per distinct clip used by any segment)
    _transformPreview: null, // {zoom, offset_x, offset_y} while dragging/zooming the preview stage, else null
    _lastStageDown: null,   // {t, x, y} of the last stage pointerdown, for manual double-click detection
    _timers: {},
    _tickTimer: null,
    _wired: false,
    _exportSig: null,
    _subsBase: null,
    _fonts: null,

    /* ---------- lifecycle ---------- */

    open(rid) {
      try {
        this._openInner(rid);
      } catch (e) {
        console.error("ReelEditor failed to open", e);
        alert("Could not open the Reel Editor — see console.");
      }
    },

    _openInner(rid) {
      if (!state.pid || !state.project) return;
      const reel = (state.project.reels || []).find((r) => r.id === rid);
      if (!reel) {
        alert("Reel not found — it may have been re-suggested since this view loaded.");
        return;
      }
      this.pid = state.pid;
      this.rid = rid;
      this.clip = (state.project.clips || []).find((c) => c.id === reel.clip_id) || null;
      this._activeSeg = 0;
      this._segDragPreview = null;
      this._transformPreview = null;
      this._lastStageDown = null;
      this._exportSig = null;
      this.playing = false;
      this.activeTab = this.activeTab || "reel";

      this._ensureStyles();
      this._ensureDom();

      try { closeDrawer(); } catch (_e) { /* drawer may not be open */ }
      try { if (!$("#settings-overlay").hidden) closeSettings(); } catch (_e) { /* settings not open */ }

      // The Color-tab comparison overlay (rAF loop + hidden <video>) lives on
      // #player-stage, underneath #project-view -- if it's active when we
      // take over the view, hiding #project-view leaves it running with
      // nothing ever tearing it down (mirrors the guard Inspector.mount()
      // already applies for the same reason: "a previous surface may have
      // left the Color-tab comparison overlay mounted").
      try { window.EditorUI.compare?.deactivate(); } catch (e) { console.error(e); }

      const pv = document.getElementById("project-view");
      if (pv) pv.hidden = true;
      const view = document.getElementById("re-view");
      if (view) view.hidden = false;
      this.isOpen = true;

      const heading = document.getElementById("re-heading-title");
      if (heading) heading.textContent = reel.title || `Reel #${reel.rank}`;
      const sub = document.getElementById("re-heading-sub");
      if (sub) sub.textContent = this.clip?.filename || "(source clip not found)";
      this._setSaveState("");
      this._closeAddPicker();

      this._layoutFrame();
      this._mountVideo();
      this._loadThumbs();
      this._reloadCues();
      this._loadSubsBase();
      this.switchTab(this.activeTab);
      this._startTick();
      this._renderStage();

      try {
        if (typeof window.SafeZonesUI !== "undefined") {
          window.SafeZonesUI.setContext({
            pid: this.pid,
            rid: this.rid,
            getReel: () => this._reel(),
            getTotalDuration: () => this._totalDuration(),
            onSeek: (t) => this._seekToGlobalTime(t),
            onReelPatched: (updated) => {
              this._mergeReel(updated);
              this._renderStage();
              if (this.activeTab === "reel") this._renderReelTab();
            },
          });
        }
      } catch (e) { console.error("SafeZonesUI setContext failed", e); }
    },

    close() {
      this.isOpen = false;
      const v = document.getElementById("re-video");
      if (v) { try { v.pause(); } catch (_e) { /* ignore */ } }
      this.playing = false;
      const view = document.getElementById("re-view");
      if (view) view.hidden = true;
      const pv = document.getElementById("project-view");
      if (pv) pv.hidden = !state.pid;
      this._stopTick();
      try { window.SafeZonesUI?.reset(); } catch (_e) { /* ignore */ }
    },

    // Elapsed-time-on-the-reel's-own-concatenated-timeline -> (segment idx,
    // local time) -> _seekTo. Used by SafeZonesUI's interval click handler
    // (the safety endpoint reports intervals on that same 0=reel-start
    // timeline, mirroring pipeline/safezones.py's _sample_times).
    _seekToGlobalTime(t) {
      const segs = this._segments();
      if (!segs.length) return;
      let offset = 0;
      for (let i = 0; i < segs.length; i++) {
        const { start, end } = this._segEffectiveWindow(i);
        const dur = Math.max(0, end - start);
        const isLast = i === segs.length - 1;
        if (t <= offset + dur || isLast) {
          const local = Math.min(Math.max(t - offset, 0), dur);
          this._seekTo(i, start + local);
          return;
        }
        offset += dur;
      }
    },

    /* ---------- one-time DOM + styles ---------- */

    _ensureStyles() {
      if (document.getElementById("reel-editor-styles")) return;
      const style = document.createElement("style");
      style.id = "reel-editor-styles";
      style.textContent = `
        .reel-editor-view { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column;
          background: var(--bg); }
        .re-topbar { flex-shrink: 0; padding: 10px 16px; border-bottom: 1px solid var(--border); }
        .re-heading { display: flex; flex-direction: column; line-height: 1.3; min-width: 0; }
        .re-heading b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 40vw; }
        .re-heading span { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 40vw; }

        .re-body { flex: 1; min-height: 0; display: grid;
          grid-template-columns: 1fr 340px; grid-template-rows: 1fr 190px;
          grid-template-areas: "preview inspector" "timeline timeline"; gap: 1px; background: var(--border); }
        .re-area { background: var(--bg); min-width: 0; min-height: 0; }

        #re-preview-pane { grid-area: preview; display: flex; flex-direction: column; }
        #re-frame-wrap { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center;
          padding: 16px; overflow: hidden; }
        /* The preview STAGE (spec v7.11 "direct manipulation on the preview"):
           always the 9:16 OUTPUT rect (never the source clip's own aspect —
           that's the whole point, the transform pans/zooms a source window
           around INSIDE this fixed-aspect box). Sized in px by _layoutFrame
           (aspect-ratio alone can't flex-shrink reliably inside the grid).
           touch-action: none lets us own pinch/wheel/drag without the
           browser's default scroll/zoom stepping on it. */
        #re-frame { position: relative; background: #000; overflow: hidden; border-radius: 8px;
          cursor: grab; touch-action: none; }
        #re-frame.re-panning { cursor: grabbing; }
        /* This is also the element ui/editor/safezones-ui.js mounts its
           mockup/hatch overlay + face-box debug layer into (window.
           SafeZonesUI, "the crop-window host") — its rect must always equal
           the true 9:16 output rect, which is exactly what #re-frame now is,
           so this is a plain inset:0 child (no more independent sizing). */
        #re-crop-window { position: absolute; inset: 0; overflow: hidden; }
        /* Automatic blurred cover-fill background (spec v7.11) — shows
           THIS SAME crop window scaled to cover the stage, not the original
           16:9 frame (that was the pre-v7.11 bug). Hidden entirely above
           the zoom=1.0 cover threshold (see _renderStage). */
        #re-bg-layer { position: absolute; inset: 0; overflow: hidden; background: #000; }
        #re-bg-video { position: absolute; left: 0; top: 0; transform-origin: 0 0; pointer-events: none;
          filter: blur(22px) brightness(.55) saturate(1.05); }
        /* Foreground: the crop window fit to the stage's width, preserving
           its own aspect (== exactly 9:16, filling the whole stage, whenever
           zoom >= 1 — no blur, no letterbox) and centered vertically
           otherwise. width/height/left/top set in px by _renderStage. */
        #re-fg-box { position: absolute; left: 0; overflow: hidden; }
        #re-video { position: absolute; left: 0; top: 0; transform-origin: 0 0; background: #000;
          pointer-events: none; }
        #re-zoom-badge { position: absolute; right: 8px; bottom: 8px; z-index: 6; pointer-events: none;
          background: rgba(0,0,0,.55); color: #fff; border-radius: 999px; padding: 2px 8px; font-size: 11px;
          opacity: .85; }
        .re-transport { flex-shrink: 0; padding: 8px 16px; border-top: 1px solid var(--border); }

        /* Reel Safety UI (spec v7.7 items 1-2): thin bar above the 9:16
           preview hosting platform toggle chips + the safety message; the
           actual mockup/hatch overlay is mounted BY safezones-ui.js as a
           child of #re-crop-window (see _ensureDom below) so it always
           matches the true output rect regardless of layout size. Content
           of #re-safezone-bar and the overlay is owned entirely by
           ui/editor/safezones-ui.js (window.SafeZonesUI) -- this file only
           reserves the container and its own spacing. */
        #re-safezone-bar { flex-shrink: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
          padding: 6px 16px 0; }

        #re-inspector { grid-area: inspector; display: flex; flex-direction: column; overflow: hidden; }
        .re-tabs { display: flex; flex-shrink: 0; border-bottom: 1px solid var(--border); background: var(--panel2); }
        .re-tab { flex: 1; background: none; border: none; border-bottom: 2px solid transparent; color: var(--dim);
          cursor: pointer; padding: 9px 4px; font-size: 12px; transition: color .15s ease, border-color .15s ease; }
        .re-tab:hover { color: var(--text); }
        .re-tab.active { color: var(--text); border-bottom-color: var(--accent-hover); font-weight: 600; }
        .re-tabpanels { flex: 1; overflow-y: auto; padding: 12px; }
        .re-readonly-row { display: flex; align-items: center; gap: 14px; margin: 6px 0; font-size: 12px; color: var(--dim); }
        .re-readonly-row b { color: var(--text); }

        #re-timeline-pane { grid-area: timeline; display: flex; flex-direction: column; overflow: hidden;
          padding: 8px 12px 10px; }
        #re-segments-wrap { flex: 1; min-height: 0; position: relative; overflow: visible; border-radius: 8px;
          background: var(--panel2); border: 1px solid var(--border); }
        #re-segments-row { position: relative; height: 100%; display: flex; align-items: stretch; gap: 3px; padding: 0 2px; }
        .re-seg { position: relative; min-width: 36px; height: 100%; margin: 4px 0; border-radius: 6px; overflow: hidden;
          background: #05070d; border: 2px solid var(--accent-hover); cursor: pointer; box-sizing: border-box; }
        .re-seg-film { position: absolute; inset: 0; opacity: .75; background-repeat: no-repeat; pointer-events: none; }
        .re-seg-edge { position: absolute; top: 0; bottom: 0; width: 12px; cursor: ew-resize; z-index: 2; }
        .re-seg-edge-l { left: -5px; } .re-seg-edge-r { right: -5px; }
        .re-seg-del { position: absolute; top: 2px; right: 2px; z-index: 3; width: 16px; height: 16px; line-height: 14px;
          text-align: center; border-radius: 999px; background: rgba(0,0,0,.6); color: #fff; font-size: 11px;
          cursor: pointer; border: none; padding: 0; }
        .re-seg-del:hover { background: var(--accent-hover); }
        .re-seg-time { position: absolute; left: 4px; bottom: 2px; font-size: 10px; color: #fff;
          background: rgba(0,0,0,.5); border-radius: 4px; padding: 1px 4px; z-index: 2; pointer-events: none;
          white-space: nowrap; }
        .re-junction-chip { flex-shrink: 0; align-self: center; font-size: 10px; padding: 3px 7px; border-radius: 999px;
          background: var(--panel2); border: 1px solid var(--border); color: var(--dim); cursor: pointer;
          white-space: nowrap; z-index: 2; }
        .re-junction-chip.fade, .re-junction-chip.crossfade { color: var(--accent2); border-color: var(--accent2); }
        #re-playhead { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent-hover); z-index: 5;
          pointer-events: none; box-shadow: 0 0 8px var(--accent-hover); }
        .re-drag-tooltip { position: absolute; top: -18px; font-size: 10px; background: var(--accent-hover); color: #fff;
          padding: 1px 5px; border-radius: 4px; white-space: nowrap; z-index: 6; pointer-events: none;
          transform: translateX(-50%); }
        .re-tl-footer { flex-shrink: 0; padding-top: 8px; display: flex; align-items: center; gap: 10px; }

        #re-add-picker { flex-shrink: 0; margin-top: 8px; padding: 8px; border: 1px solid var(--border);
          border-radius: 8px; background: var(--panel2); }
        #re-add-strip-wrap { position: relative; height: 44px; border-radius: 6px; overflow: hidden;
          background: #05070d; border: 1px solid var(--border); cursor: crosshair; }
        #re-add-strip-film { position: absolute; inset: 0; opacity: .8; background-repeat: no-repeat; pointer-events: none; }

        #re-cue-list .field-row { align-items: center; }
        #re-cue-list .field-row label { width: 76px; flex-shrink: 0; }
        #re-cue-list .field-row input[type=text] { flex: 1; width: auto; }
      `;
      document.head.appendChild(style);
    },

    _ensureDom() {
      if (document.getElementById("re-view")) { this._wireOnce(); return; }
      const content = document.getElementById("content");
      if (!content) return;
      const div = document.createElement("div");
      div.id = "re-view";
      div.className = "reel-editor-view";
      div.hidden = true;
      div.innerHTML = `
        <div class="re-topbar row">
          <button id="re-back" class="btn small" title="Esc"><i data-lucide="arrow-left"></i> Back to project</button>
          <div class="re-heading">
            <b id="re-heading-title">Reel</b>
            <span id="re-heading-sub" class="dim"></span>
          </div>
          <span class="grow"></span>
          <span id="re-save-state" class="dim"></span>
        </div>
        <div class="re-body">
          <section id="re-preview-pane" class="re-area">
            <div id="re-safezone-bar"></div>
            <div id="re-frame-wrap">
              <div id="re-frame" title="Drag to pan · Scroll/pinch to zoom · Double-click to reset">
                <div id="re-crop-window">
                  <div id="re-bg-layer" hidden>
                    <video id="re-bg-video" muted playsinline preload="auto"></video>
                  </div>
                  <div id="re-fg-box">
                    <video id="re-video" playsinline preload="auto"></video>
                  </div>
                </div>
                <div id="re-zoom-badge" class="mono">100%</div>
              </div>
            </div>
            <div class="re-transport row">
              <button id="re-playpause" class="btn small" title="Space"><i data-lucide="play"></i></button>
              <span id="re-time" class="dim mono">0:00 / 0:00</span>
            </div>
          </section>

          <aside id="re-inspector" class="re-area">
            <nav class="re-tabs" id="re-tabs">
              <button class="re-tab" data-re-tab="reel">Reel</button>
              <button class="re-tab" data-re-tab="subs">Subs</button>
              <button class="re-tab" data-re-tab="export">Export</button>
            </nav>
            <div class="re-tabpanels">
              <div id="re-panel-reel" class="re-panel" data-panel="reel"></div>
              <div id="re-panel-subs" class="re-panel" data-panel="subs" hidden></div>
              <div id="re-panel-export" class="re-panel" data-panel="export" hidden></div>
            </div>
          </aside>

          <section id="re-timeline-pane" class="re-area">
            <div class="dim" style="margin-bottom:6px">Drag a segment's bright edges to trim (extends past its
              AI window); click/drag a segment to seek; click a junction chip to cycle its transition.</div>
            <div id="re-segments-wrap">
              <div id="re-segments-row"></div>
              <div id="re-playhead" hidden></div>
            </div>
            <div class="re-tl-footer">
              <button id="re-add-segment-btn" class="btn small"><i data-lucide="plus"></i> Add segment</button>
              <span class="dim mono" id="re-total-dur"></span>
            </div>
            <div id="re-add-picker" hidden>
              <div class="dim" style="margin-bottom:6px">Click a moment on the source clip to add a new segment there.</div>
              <div id="re-add-strip-wrap">
                <div id="re-add-strip-film"></div>
              </div>
              <div class="row" style="margin-top:6px">
                <button id="re-add-cancel" class="btn small">Cancel</button>
              </div>
            </div>
          </section>
        </div>`;
      content.appendChild(div);
      this._wireOnce();
      // safezones-ui.js owns everything inside #re-safezone-bar and the
      // mockup overlay it appends into #re-crop-window; guarded so a script
      // load-order hiccup or a JS error inside that module can never blank
      // this view (same defensive pattern as home.js's HomeView guard).
      try {
        if (typeof window.SafeZonesUI !== "undefined") {
          window.SafeZonesUI.mount(
            document.getElementById("re-safezone-bar"),
            document.getElementById("re-crop-window"),
          );
        }
      } catch (e) { console.error("SafeZonesUI mount failed", e); }
      refreshIcons();
    },

    _wireOnce() {
      if (this._wired) return;
      this._wired = true;
      const back = document.getElementById("re-back");
      if (back) back.onclick = () => this.close();
      const play = document.getElementById("re-playpause");
      if (play) play.onclick = () => this.togglePlay();
      document.getElementById("re-tabs")?.querySelectorAll("[data-re-tab]").forEach((b) =>
        b.onclick = () => this.switchTab(b.dataset.reTab));
      this._wireStageGestures();
      const addBtn = document.getElementById("re-add-segment-btn");
      if (addBtn) addBtn.onclick = () => this._openAddPicker();
      const cancelBtn = document.getElementById("re-add-cancel");
      if (cancelBtn) cancelBtn.onclick = () => this._closeAddPicker();
      const addStrip = document.getElementById("re-add-strip-wrap");
      if (addStrip) addStrip.addEventListener("click", (e) => {
        const rect = addStrip.getBoundingClientRect();
        if (!rect.width) return;
        const dur = this.clip?.info?.duration || 1;
        const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        this._addSegmentAt(frac * dur);
      });
      window.addEventListener("resize", () => { if (this.isOpen) { this._layoutFrame(); this._renderSegmentsRow(); } });
      // Capture phase, and BEFORE anything else: while the Reel Editor is a
      // full takeover, the main editor's timeline/player keydown handlers
      // (bound globally on `document`, bubble phase) must never fire — e.g.
      // pressing "x" to type in a title input would otherwise also trigger
      // the hidden main timeline's splitAtPlayhead(). stopPropagation() here
      // only blocks OTHER JS listeners from seeing the event; it does not
      // block the browser's own default text-insertion for whatever input
      // has focus, so typing in this view's fields is unaffected.
      document.addEventListener("keydown", (e) => this._onKeydown(e), true);
    },

    /* ---------- keyboard (spec: "same transport keys; Esc = back") ---------- */

    _onKeydown(e) {
      if (!this.isOpen) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        e.preventDefault();
        if (!document.getElementById("re-add-picker")?.hidden) { this._closeAddPicker(); return; }
        this.close();
        return;
      }
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      const typing = tag === "input" || tag === "textarea" || tag === "select" || document.activeElement?.isContentEditable;
      if (e.key === " " && !typing) {
        e.stopPropagation();
        e.preventDefault();
        this.togglePlay();
        return;
      }
      e.stopPropagation(); // swallow everything else too — see _wireOnce's comment
    },

    /* ---------- data access ---------- */

    _reel() {
      return (state.project?.reels || []).find((r) => r.id === this.rid) || {};
    },

    _segments() {
      return this._reel().segments || [];
    },

    _transitions() {
      return this._reel().transitions || [];
    },

    _clipFor(clipId) {
      return (state.project?.clips || []).find((c) => c.id === clipId) || null;
    },

    _segEffectiveWindow(idx) {
      const seg = this._segments()[idx];
      if (!seg) return { start: 0, end: 1 };
      const clip = this._clipFor(seg.clip_id);
      const duration = clip?.info?.duration;
      const dragPreview = (this._segDragPreview && this._segDragPreview.idx === idx)
        ? { start: this._segDragPreview.start, end: this._segDragPreview.end }
        : null;
      return _effectiveWindowFor(seg, duration, dragPreview);
    },

    _totalDuration() {
      const segs = this._segments();
      let total = 0;
      for (let i = 0; i < segs.length; i++) {
        const { start, end } = this._segEffectiveWindow(i);
        total += Math.max(0, end - start);
      }
      return total;
    },

    _mergeReel(updated) {
      const list = state.project?.reels;
      if (!list || !updated) return;
      const idx = list.findIndex((r) => r.id === this.rid);
      if (idx >= 0) list[idx] = updated;
    },

    /* ---------- save plumbing ---------- */

    _setSaveState(text, isError) {
      const el = document.getElementById("re-save-state");
      if (!el) return;
      el.textContent = text;
      el.style.color = isError ? "var(--danger)" : "";
    },

    async _patch(fields, opts = {}) {
      this._setSaveState("Saving…");
      try {
        const updated = await api(`/projects/${this.pid}/reels/${this.rid}`, { method: "PATCH", body: fields });
        this._mergeReel(updated);
        this._setSaveState("Saved");
        opts.afterSave?.(updated);
      } catch (e) {
        this._setSaveState(`Save failed: ${e.message}`, true);
      }
    },

    _debouncedPatch(key, fields, delay, afterSave) {
      clearTimeout(this._timers[key]);
      this._timers[key] = setTimeout(() => this._patch(fields, { afterSave }), delay);
    },

    /* ---------- segments/transitions PATCH plumbing (spec v5.8b) ---------- */

    _commitSegments(segments, transitions, afterSave) {
      const fields = { segments };
      if (transitions) fields.transitions = transitions;
      this._patch(fields, { afterSave });
    },

    // Structural edits (add/delete/edge-drag-release) commit immediately on
    // gesture-end, same "optimistic local state during the gesture, one PATCH
    // on release" pattern as the single-segment code this replaces — the
    // spec's "debounced" only needs to cover rapid repeat clicks (e.g. the
    // junction chip cycling quickly), handled via _timers below.
    _debouncedCommitSegments(segments, transitions, delay, afterSave) {
      clearTimeout(this._timers.segments);
      this._timers.segments = setTimeout(() => this._commitSegments(segments, transitions, afterSave), delay);
    },

    _afterSegmentsChange() {
      this._loadThumbs(); // covers any newly-referenced clip_id; ends in _renderSegmentsRow()
      this._reloadCues().then(() => { if (this.activeTab === "subs") this._renderSubsTab(); });
      if (this.activeTab === "reel") this._renderReelTab();
      if (this._activeSeg >= this._segments().length) this._activeSeg = Math.max(0, this._segments().length - 1);
      const v = document.getElementById("re-video");
      if (v) {
        const { start, end } = this._segEffectiveWindow(this._activeSeg);
        if (v.currentTime < start || v.currentTime > end) { try { v.currentTime = start; } catch (_e) { /* ignore */ } }
      }
      this._renderStage(); // active segment's clip (and its src pixel dims) may have changed
      // in/out (segment trims) changed -> re-run the safety check (spec
      // v7.7 item 2: "after in/out/crop/fit changes, debounced").
      try { window.SafeZonesUI?.notifyChange(); } catch (_e) { /* ignore */ }
    },

    /* ---------- framing: {zoom, offset_x, offset_y} transform (spec v7.11) ----------
       Direct manipulation ON the 9:16 preview stage (the main ask, replacing
       the old sweep/slider crop_x UX): drag pans, wheel/pinch zooms around
       the cursor, double-click resets to the AI/face-centered default. See
       this file's top docstring for the client-side math mirror this all
       leans on (_transformCropRect/_transformNeedsBlur/_deriveTransformFromReel). */

    _activeClipInfo() {
      const seg = this._segments()[this._activeSeg];
      const clip = seg ? this._clipFor(seg.clip_id) : null;
      return (clip || this.clip)?.info || { width: 16, height: 9 };
    },

    _currentTransform() {
      return this._transformPreview || _deriveTransformFromReel(this._reel());
    },

    _layoutFrame() {
      const wrap = document.getElementById("re-frame-wrap");
      const frame = document.getElementById("re-frame");
      if (!wrap || !frame) return;
      const cw = wrap.clientWidth, ch = wrap.clientHeight;
      // The stage is ALWAYS the 9:16 OUTPUT aspect (spec v7.11) -- never the
      // source clip's own aspect, unlike the old wide-frame + highlighted
      // crop-window UI this replaces.
      const ar = 9 / 16;
      let fw = cw, fh = cw / ar;
      if (fh > ch) { fh = ch; fw = ch * ar; }
      frame.style.width = `${Math.max(1, fw)}px`;
      frame.style.height = `${Math.max(1, fh)}px`;
      this._renderStage();
    },

    // Lays out the foreground (crop window fit to the stage's width,
    // preserving its own aspect) and, when zoomed out below the cover
    // threshold, the blurred background (the SAME crop window scaled to
    // COVER the stage -- spec v7.11's explicit bug fix: never the original
    // 16:9 frame). Mirrors faces.transform_vertical_crop_filter's fg/bg
    // filtergraph chains, just in CSS transforms instead of ffmpeg. Called
    // on layout/resize, on every drag/zoom tick (optimistic, via
    // _transformPreview), after a PATCH settles, and whenever the active
    // segment's clip (and therefore its source pixel dimensions) changes.
    _renderStage() {
      const frame = document.getElementById("re-frame");
      const bgLayer = document.getElementById("re-bg-layer");
      const bgVideo = document.getElementById("re-bg-video");
      const fgBox = document.getElementById("re-fg-box");
      const fgVideo = document.getElementById("re-video");
      const badge = document.getElementById("re-zoom-badge");
      if (!frame || !fgBox || !fgVideo) return;
      const stageW = frame.clientWidth || 1, stageH = frame.clientHeight || 1;
      const info = this._activeClipInfo();
      const srcW = info.width || 16, srcH = info.height || 9;
      const t = this._currentTransform();
      const rect = _transformCropRect(srcW, srcH, t.zoom, t.offset_x, t.offset_y);
      const blur = _transformNeedsBlur(t.zoom);

      if (badge) badge.textContent = `${Math.round(t.zoom * 100)}%`;

      const fgScale = stageW / rect.w;
      const fgH = rect.h * fgScale;
      fgBox.style.top = `${(stageH - fgH) / 2}px`;
      fgBox.style.width = `${stageW}px`;
      fgBox.style.height = `${fgH}px`;
      fgVideo.style.width = `${srcW * fgScale}px`;
      fgVideo.style.height = `${srcH * fgScale}px`;
      fgVideo.style.transform = `translate(${-rect.x * fgScale}px, ${-rect.y * fgScale}px)`;

      if (bgLayer) bgLayer.hidden = !blur;
      if (blur && bgVideo) {
        const bgScale = Math.max(stageW / rect.w, stageH / rect.h);
        const offX = (stageW - rect.w * bgScale) / 2, offY = (stageH - rect.h * bgScale) / 2;
        bgVideo.style.width = `${srcW * bgScale}px`;
        bgVideo.style.height = `${srcH * bgScale}px`;
        bgVideo.style.transform = `translate(${-rect.x * bgScale + offX}px, ${-rect.y * bgScale + offY}px)`;
      }
    },

    _wireStageGestures() {
      const frame = document.getElementById("re-frame");
      if (!frame) return;
      frame.addEventListener("pointerdown", (e) => this._onStagePointerDown(e));
      frame.addEventListener("wheel", (e) => this._onStageWheel(e), { passive: false });
      // NOT relied on as the primary mechanism: Chromium/Firefox/Safari all
      // suppress the synthesized "dblclick" compatibility event once a
      // "pointerdown" on the same target called preventDefault() (Pointer
      // Events spec) -- which _onStagePointerDown must do (to stop native
      // drag-image / text-selection while panning), so double-click reset
      // is detected manually there instead (see that method). Kept as a
      // harmless no-op-if-never-fires fallback for any environment that
      // doesn't suppress it.
      frame.addEventListener("dblclick", (e) => { e.preventDefault(); this._resetTransform(); });
    },

    // Drag-to-pan: the crop window's (x, y) moves opposite the drag delta,
    // in source pixels, using the foreground's own uniform "fit-width"
    // scale factor (screen px -> source px) computed once at drag start --
    // same optimistic-preview-then-patch-on-release pattern the old crop_x
    // drag used (see this section's docstring / _cropDragPreview's old
    // comment for why the value is kept until the PATCH confirms it).
    //
    // Also does its OWN double-click detection (two pointerdowns close in
    // time and position) rather than trusting the "dblclick" event -- see
    // _wireStageGestures' comment for why that event never reliably fires
    // here.
    _onStagePointerDown(e) {
      if (e.button != null && e.button !== 0) return;
      const frame = document.getElementById("re-frame");
      if (!frame) return;
      const now = performance.now();
      const last = this._lastStageDown;
      this._lastStageDown = { t: now, x: e.clientX, y: e.clientY };
      if (last && now - last.t < 400 && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 8) {
        this._lastStageDown = null; // consumed -- a 3rd rapid click starts fresh, not another reset
        e.preventDefault();
        this._resetTransform();
        return;
      }
      e.preventDefault();
      frame.classList.add("re-panning");
      try { frame.setPointerCapture(e.pointerId); } catch (_e) { /* ignore */ }
      const info = this._activeClipInfo();
      const srcW = info.width || 16, srcH = info.height || 9;
      const start = this._currentTransform();
      const rect0 = _transformCropRect(srcW, srcH, start.zoom, start.offset_x, start.offset_y);
      const stageRect = frame.getBoundingClientRect();
      const scale = stageRect.width / rect0.w || 1; // uniform fg source-px -> screen-px scale
      const startX = e.clientX, startY = e.clientY;
      let moved = false;

      const onMove = (ev) => {
        moved = true;
        const dx = ev.clientX - startX, dy = ev.clientY - startY;
        const x = _clampNum(rect0.x - dx / scale, 0, srcW - rect0.w);
        const y = _clampNum(rect0.y - dy / scale, 0, srcH - rect0.h);
        const ox = rect0.roomX > 0 ? _clampNum((x - rect0.cx + rect0.w / 2) / rect0.roomX, -1, 1) : start.offset_x;
        const oy = rect0.roomY > 0 ? _clampNum((y - rect0.cy + rect0.h / 2) / rect0.roomY, -1, 1) : start.offset_y;
        this._transformPreview = { zoom: start.zoom, offset_x: ox, offset_y: oy };
        this._renderStage();
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        frame.classList.remove("re-panning");
        if (moved && this._transformPreview) this._commitTransform(this._transformPreview);
        else this._transformPreview = null;
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    // Scroll-wheel / trackpad-pinch zoom, anchored on the cursor (spec:
    // "scroll-wheel/trackpad-pinch over the preview... to zoom") -- solves
    // for the new offset_x/offset_y that keeps the SOURCE point currently
    // under the cursor fixed on screen after the zoom changes the crop
    // window's size. Debounced PATCH (rapid wheel ticks shouldn't each
    // trigger a round-trip); the live preview itself is never debounced.
    _onStageWheel(e) {
      e.preventDefault();
      const frame = document.getElementById("re-frame");
      if (!frame) return;
      const info = this._activeClipInfo();
      const srcW = info.width || 16, srcH = info.height || 9;
      const start = this._currentTransform();
      const stageRect = frame.getBoundingClientRect();
      if (!stageRect.width || !stageRect.height) return;

      const rect0 = _transformCropRect(srcW, srcH, start.zoom, start.offset_x, start.offset_y);
      const scale0 = stageRect.width / rect0.w;
      const fgTop0 = (stageRect.height - rect0.h * scale0) / 2;
      const cxScreen = e.clientX - stageRect.left, cyScreen = e.clientY - stageRect.top;
      const sx = rect0.x + cxScreen / scale0;
      const sy = rect0.y + (cyScreen - fgTop0) / scale0;

      // deltaY < 0 (scroll up / pinch out) => zoom IN. Multiplicative step
      // so it feels consistent across mouse wheels and trackpad pinch.
      const factor = Math.exp(-e.deltaY * 0.0018);
      const newZoom = _clampNum(start.zoom * factor, TRANSFORM_ZOOM_MIN, TRANSFORM_ZOOM_MAX);
      if (newZoom === start.zoom) return;

      const rect1 = _transformCropRect(srcW, srcH, newZoom, start.offset_x, start.offset_y);
      const scale1 = stageRect.width / rect1.w;
      const fgTop1 = (stageRect.height - rect1.h * scale1) / 2;
      let x1 = sx - cxScreen / scale1;
      let y1 = sy - (cyScreen - fgTop1) / scale1;
      x1 = _clampNum(x1, 0, srcW - rect1.w);
      y1 = _clampNum(y1, 0, srcH - rect1.h);
      const ox1 = rect1.roomX > 0 ? _clampNum((x1 - rect1.cx + rect1.w / 2) / rect1.roomX, -1, 1) : 0;
      const oy1 = rect1.roomY > 0 ? _clampNum((y1 - rect1.cy + rect1.h / 2) / rect1.roomY, -1, 1) : 0;

      this._transformPreview = { zoom: newZoom, offset_x: ox1, offset_y: oy1 };
      this._renderStage();
      const val = document.getElementById("re-zoom-val");
      if (val) val.textContent = `${Math.round(newZoom * 100)}%`;
      const zoomInput = document.getElementById("re-zoom-input");
      if (zoomInput) zoomInput.value = newZoom;
      this._debouncedCommitTransform(this._transformPreview, 250);
    },

    // Double-click = reset to auto (spec): zoom 1.0, offset 0/0 -- the
    // server always recomputes the face-centered crop from THIS pair
    // (offset 0 has zero room to move at zoom 1.0 on the vertical axis for
    // the common wider-than-9:16 source, and simply re-centers on the
    // detected face horizontally -- see pipeline/faces.py's module note).
    _resetTransform() {
      this._transformPreview = { zoom: 1, offset_x: 0, offset_y: 0 };
      this._renderStage();
      this._commitTransform(this._transformPreview);
    },

    _commitTransform(t) {
      this._patch({ transform: { zoom: t.zoom, offset_x: t.offset_x, offset_y: t.offset_y } }, {
        afterSave: () => {
          // Keep showing the just-applied (optimistic) numbers until the
          // merged reel reflects them -- avoids a visible snap for the
          // round-trip's duration (same pattern the old crop_x drag used).
          this._transformPreview = null;
          this._renderStage();
          if (this.activeTab === "reel") this._renderFramingCard();
          try { window.SafeZonesUI?.notifyChange(); } catch (_e) { /* ignore */ }
        },
      });
    },

    _debouncedCommitTransform(t, delay) {
      clearTimeout(this._timers.transform);
      this._timers.transform = setTimeout(() => this._commitTransform(t), delay);
    },

    /* ---------- playback (virtual multi-segment EDL, loop) ---------- */

    _mountVideo() {
      const v = document.getElementById("re-video");
      if (!v) return;
      v.ontimeupdate = () => this._onTimeUpdate();
      v.onerror = () => console.error("Reel Editor video error", v.error);
      this._activeSeg = 0;
      if (this._segments().length) {
        const { start } = this._segEffectiveWindow(0);
        this._loadSegmentVideo(0, start, false);
      }
    },

    // Swaps <video> src only when the target segment's clip differs from
    // what's currently loaded (so consecutive same-clip segments don't
    // reload/flash); otherwise just jumps currentTime. `resumePlaying`
    // re-issues play() once metadata/seek settles, so playback continues
    // seamlessly across a segment (and clip) boundary.
    _loadSegmentVideo(idx, localTime, resumePlaying) {
      const seg = this._segments()[idx];
      const v = document.getElementById("re-video");
      if (!seg || !v) return;
      const src = `/api/projects/${this.pid}/media/preview/${seg.clip_id}`;
      const doSeek = () => {
        try { v.currentTime = localTime; } catch (_e) { /* not ready */ }
        if (resumePlaying) v.play().catch(() => {});
      };
      if (v.dataset.src !== src) {
        v.dataset.src = src;
        v.src = src;
        v.onloadedmetadata = doSeek;
      } else {
        doSeek();
      }
      this._syncBgGhost(src, localTime, resumePlaying);
      this._renderStage(); // the active segment's clip (and its src pixel dims) may differ
    },

    // Mirrors #re-video's src/currentTime into the muted <video> that
    // approximates the automatic blurred background (spec v7.11). Always
    // kept in sync (src/time) so crossing the zoom<1 threshold mid-drag
    // never needs a fresh load/seek/flash; only actually PLAYS while a blur
    // is currently visible -- otherwise left paused so a reel that's never
    // zoomed out doesn't burn decode time on a second video stream for
    // nothing.
    _syncBgGhost(src, localTime, resumePlaying) {
      const bg = document.getElementById("re-bg-video");
      if (!bg) return;
      const shouldPlay = resumePlaying && _transformNeedsBlur(this._currentTransform().zoom);
      const seekAndPlay = () => {
        try { bg.currentTime = localTime; } catch (_e) { /* not ready */ }
        if (shouldPlay) bg.play().catch(() => {}); else bg.pause();
      };
      if (bg.dataset.src !== src) {
        bg.dataset.src = src;
        bg.src = src;
        bg.onloadedmetadata = seekAndPlay;
      } else {
        seekAndPlay();
      }
    },

    _seekTo(idx, localTime) {
      const seg = this._segments()[idx];
      if (!seg) return;
      const wasPlaying = this.playing;
      this._activeSeg = idx;
      this._loadSegmentVideo(idx, localTime, wasPlaying);
      this._renderPlayhead();
      this._updateTimeDisplay();
    },

    _onTimeUpdate() {
      const v = document.getElementById("re-video");
      if (!v) return;
      const segs = this._segments();
      if (!segs.length) return;
      const idx = this._activeSeg;
      const { start, end } = this._segEffectiveWindow(idx);
      if (v.currentTime >= end - 0.05) {
        const next = (idx + 1) % segs.length;
        if (next !== idx) {
          const wasPlaying = this.playing;
          this._activeSeg = next;
          const nextWin = this._segEffectiveWindow(next);
          this._loadSegmentVideo(next, nextWin.start, wasPlaying);
        } else {
          try { v.currentTime = start; } catch (_e) { /* ignore */ }
          if (!this.playing) v.pause();
        }
      } else if (v.currentTime < start - 0.25) {
        try { v.currentTime = start; } catch (_e) { /* ignore */ }
      }
      this._updateTimeDisplay();
      this._renderPlayhead();
      this._resyncBgGhostDrift(v.currentTime);
    },

    // Cheap periodic drift correction (called from the same ~4/s timeupdate
    // tick as everything else above) rather than seeking the ghost on every
    // frame -- good enough for a live CSS approximation.
    _resyncBgGhostDrift(currentTime) {
      const bg = document.getElementById("re-bg-video");
      if (!bg || !bg.dataset.src) return;
      if (Math.abs(bg.currentTime - currentTime) > 0.35) {
        try { bg.currentTime = currentTime; } catch (_e) { /* not ready */ }
      }
    },

    play() {
      const v = document.getElementById("re-video");
      if (!v || !this._segments().length) return;
      const { start, end } = this._segEffectiveWindow(this._activeSeg);
      if (v.currentTime < start || v.currentTime >= end) { try { v.currentTime = start; } catch (_e) { /* ignore */ } }
      this.playing = true;
      v.play().catch(() => {});
      if (_transformNeedsBlur(this._currentTransform().zoom)) {
        const bg = document.getElementById("re-bg-video");
        if (bg?.dataset.src) bg.play().catch(() => {});
      }
      const btn = document.getElementById("re-playpause");
      if (btn) { btn.innerHTML = '<i data-lucide="pause"></i>'; refreshIcons(); }
    },
    pause() {
      this.playing = false;
      const v = document.getElementById("re-video");
      v?.pause();
      document.getElementById("re-bg-video")?.pause();
      const btn = document.getElementById("re-playpause");
      if (btn) { btn.innerHTML = '<i data-lucide="play"></i>'; refreshIcons(); }
    },
    togglePlay() { this.playing ? this.pause() : this.play(); },

    _updateTimeDisplay() {
      const el = document.getElementById("re-time");
      if (!el) return;
      const v = document.getElementById("re-video");
      const idx = this._activeSeg;
      const { start } = this._segEffectiveWindow(idx);
      let elapsed = 0;
      for (let i = 0; i < idx; i++) {
        const w = this._segEffectiveWindow(i);
        elapsed += Math.max(0, w.end - w.start);
      }
      elapsed += Math.max(0, (v?.currentTime || start) - start);
      el.textContent = `${fmtT(elapsed)} / ${fmtT(this._totalDuration())}`;
    },

    /* ---------- mini-timeline: segments row, playhead, junctions ---------- */

    async _ensureSegThumbs(clipId) {
      if (this._segThumbs[clipId]) return this._segThumbs[clipId];
      const entry = { meta: null, stripUrl: null, failed: false };
      this._segThumbs[clipId] = entry;
      try {
        entry.meta = await api(`/projects/${this.pid}/thumbs/${clipId}/meta`);
        entry.stripUrl = `/api/projects/${this.pid}/thumbs/${clipId}/strip`;
      } catch (_e) {
        entry.failed = true; // 404 — no filmstrip generated yet; plain block is fine
      }
      return entry;
    },

    async _loadThumbs() {
      const ids = new Set();
      if (this.clip) ids.add(this.clip.id);
      this._segments().forEach((s) => ids.add(s.clip_id));
      await Promise.all([...ids].map((id) => this._ensureSegThumbs(id)));
      this._renderSegmentsRow();
      if (!document.getElementById("re-add-picker")?.hidden) this._renderAddPickerStrip();
    },

    _renderSegmentsRow() {
      const row = document.getElementById("re-segments-row");
      if (!row) return;
      const segs = this._segments();
      const transitions = this._transitions();
      if (!segs.length) { row.innerHTML = ""; this._renderPlayhead(); return; }

      const parts = [];
      segs.forEach((seg, i) => {
        const { start, end } = this._segEffectiveWindow(i);
        const dur = Math.max(0.1, end - start);
        parts.push(`
          <div class="re-seg${i === this._activeSeg ? " active-seg" : ""}" data-seg="${i}" style="flex:${dur} 0 0px">
            <div class="re-seg-film" data-film="${i}"></div>
            <div class="re-seg-time" data-time="${i}">${fmtT(dur)}</div>
            ${segs.length > 1 ? `<button type="button" class="re-seg-del" data-del="${i}" title="Delete segment">✕</button>` : ""}
            <div class="re-seg-edge re-seg-edge-l" data-seg-edge="${i}" data-edge="start"></div>
            <div class="re-seg-edge re-seg-edge-r" data-seg-edge="${i}" data-edge="end"></div>
          </div>`);
        if (i < segs.length - 1) {
          const t = transitions[i] || { type: "crossfade", duration: 0.4 };
          parts.push(`<button type="button" class="re-junction-chip ${t.type}" data-junction="${i}"
            title="Click to change transition">${t.type}</button>`);
        }
      });
      row.innerHTML = parts.join("");

      // second pass: measure each block's rendered width, then paint its
      // filmstrip crop (bg-size/position depend on the actual px width,
      // which only exists post-layout with flex-grow-by-duration blocks).
      segs.forEach((seg, i) => {
        const el = row.querySelector(`.re-seg[data-seg="${i}"]`);
        const film = row.querySelector(`.re-seg-film[data-film="${i}"]`);
        if (!el || !film) return;
        const thumbs = this._segThumbs[seg.clip_id];
        const { start, end } = this._segEffectiveWindow(i);
        const dur = Math.max(0.1, end - start);
        const blockW = el.clientWidth || 1;
        if (thumbs?.meta && thumbs?.stripUrl) {
          const pxPerSec = blockW / dur;
          const bgW = Math.max(1, thumbs.meta.count * thumbs.meta.interval_s * pxPerSec);
          film.style.backgroundImage = `url('${thumbs.stripUrl}')`;
          film.style.backgroundSize = `${bgW.toFixed(1)}px 100%`;
          film.style.backgroundPosition = `${(-start * pxPerSec).toFixed(1)}px 0`;
        } else {
          film.style.backgroundImage = "";
        }
      });

      this._wireSegmentsRow();
      this._renderPlayhead();
      const totalEl = document.getElementById("re-total-dur");
      if (totalEl) totalEl.textContent = `Total ${fmtT(this._totalDuration())}`;
      refreshIcons();
    },

    _wireSegmentsRow() {
      const row = document.getElementById("re-segments-row");
      if (!row) return;
      row.querySelectorAll(".re-seg-edge").forEach((edge) => {
        edge.addEventListener("pointerdown", (e) => {
          e.stopPropagation();
          this._onSegEdgeDown(e, Number(edge.dataset.segEdge), edge.dataset.edge);
        });
      });
      row.querySelectorAll(".re-seg-del").forEach((btn) => {
        btn.onclick = (e) => { e.stopPropagation(); this._deleteSegment(Number(btn.dataset.del)); };
      });
      row.querySelectorAll(".re-junction-chip").forEach((chip) => {
        chip.onclick = (e) => { e.stopPropagation(); this._cycleTransition(Number(chip.dataset.junction)); };
      });
      row.querySelectorAll(".re-seg").forEach((el) => {
        el.addEventListener("pointerdown", (e) => {
          if (e.target.closest(".re-seg-edge") || e.target.closest(".re-seg-del")) return;
          this._onSegBackgroundDown(e, el, Number(el.dataset.seg));
        });
      });
    },

    // Click/drag on a segment's own filmstrip = seek within it (spec
    // v5.8a: "click/drag on the strip seeks").
    _onSegBackgroundDown(e, el, idx) {
      const rect = el.getBoundingClientRect();
      if (!rect.width) return;
      const seek = (clientX) => {
        const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        const { start, end } = this._segEffectiveWindow(idx);
        this._seekTo(idx, start + frac * (end - start));
      };
      seek(e.clientX);
      const onMove = (ev) => seek(ev.clientX);
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    _ensureDragTooltip() {
      let tip = document.getElementById("re-drag-tooltip");
      if (!tip) {
        tip = document.createElement("div");
        tip.id = "re-drag-tooltip";
        tip.className = "re-drag-tooltip";
        tip.hidden = true;
        document.getElementById("re-segments-wrap")?.appendChild(tip);
      }
      return tip;
    },

    _showDragTooltip(tip, timeValue, field, el) {
      const wrap = document.getElementById("re-segments-wrap");
      if (!tip || !wrap) return;
      const wrapRect = wrap.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const x = field === "start" ? (elRect.left - wrapRect.left) : (elRect.right - wrapRect.left);
      tip.hidden = false;
      tip.textContent = fmtT(timeValue);
      tip.style.left = `${x}px`;
    },

    // Drag a segment's in/out handle (spec v5.8a: "in/out handles show
    // timecodes while dragging"; extends past the segment's own AI window,
    // same clamp/snap behavior the pre-v5.8b single-window strip used).
    _onSegEdgeDown(e, idx, field) {
      const seg = this._segments()[idx];
      const el = document.querySelector(`.re-seg[data-seg="${idx}"]`);
      if (!seg || !el) return;
      const rect = el.getBoundingClientRect();
      if (!rect.width) return;
      const orig = this._segEffectiveWindow(idx);
      const clip = this._clipFor(seg.clip_id);
      const dur = clip?.info?.duration ?? Infinity;
      const startX = e.clientX;
      const pxPerSec = rect.width / Math.max(0.05, orig.end - orig.start);
      const tip = this._ensureDragTooltip();
      const onMove = (ev) => {
        const deltaSec = (ev.clientX - startX) / pxPerSec;
        let s = orig.start, en = orig.end;
        if (field === "start") {
          s = Math.round((orig.start + deltaSec) / 0.05) * 0.05;
          s = Math.max(0, Math.min(s, en - 0.2));
        } else {
          en = Math.round((orig.end + deltaSec) / 0.05) * 0.05;
          en = Math.max(s + 0.2, Math.min(en, dur));
        }
        this._segDragPreview = { idx, start: s, end: en };
        this._renderSegmentsRow();
        this._showDragTooltip(tip, field === "start" ? s : en, field, document.querySelector(`.re-seg[data-seg="${idx}"]`) || el);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        tip.hidden = true;
        const p = this._segDragPreview;
        if (p) this._commitSegmentWindow(p.idx, p.start, p.end);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    _commitSegmentWindow(idx, s, en) {
      const segs = this._segments().map((seg, i) => i === idx
        ? { ...seg, in_override: Math.round(s * 1000) / 1000, out_override: Math.round(en * 1000) / 1000 }
        : seg);
      // Kept as the optimistic value (not nulled) until the PATCH settles --
      // _segEffectiveWindow() prefers _segDragPreview when its idx matches,
      // so the block/playhead/video-loop bounds keep reflecting the
      // just-committed trim through the round-trip instead of snapping back.
      this._commitSegments(segs, null, () => {
        this._segDragPreview = null;
        this._afterSegmentsChange();
      });
    },

    _deleteSegment(idx) {
      const segs = this._segments();
      if (segs.length <= 1) return;
      const newSegs = segs.filter((_, i) => i !== idx);
      // Drop the junction attached to the removed segment: the one before it
      // if it had a predecessor, else the one after (segment 0 has none before).
      const trans = this._transitions();
      const dropAt = idx > 0 ? idx - 1 : 0;
      const newTrans = trans.filter((_, i) => i !== dropAt);
      this._commitSegments(newSegs, newTrans, () => this._afterSegmentsChange());
    },

    _cycleTransition(junctionIdx) {
      const trans = this._transitions().map((t) => ({ ...t }));
      const cur = trans[junctionIdx] || { type: "crossfade", duration: 0.4 };
      const next = TRANSITION_ORDER[(TRANSITION_ORDER.indexOf(cur.type) + 1) % TRANSITION_ORDER.length];
      // TransitionInput.duration is `gt=0.0` regardless of type (the server
      // zeroes it out internally for "none") -- always send a positive value.
      trans[junctionIdx] = { type: next, duration: cur.duration && cur.duration > 0 ? cur.duration : 0.4 };
      this._debouncedCommitSegments(this._segments(), trans, 250, () => this._renderSegmentsRow());
    },

    _renderPlayhead() {
      const row = document.getElementById("re-segments-row");
      const wrap = document.getElementById("re-segments-wrap");
      const ph = document.getElementById("re-playhead");
      if (!row || !wrap || !ph) return;
      const idx = this._activeSeg;
      const el = row.querySelector(`.re-seg[data-seg="${idx}"]`);
      if (!el) { ph.hidden = true; return; }
      const v = document.getElementById("re-video");
      const { start, end } = this._segEffectiveWindow(idx);
      const dur = Math.max(0.05, end - start);
      const frac = Math.max(0, Math.min(1, ((v?.currentTime || start) - start) / dur));
      const wrapRect = wrap.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      ph.hidden = false;
      ph.style.left = `${(elRect.left - wrapRect.left) + frac * elRect.width}px`;
    },

    /* ---- Add segment: pick a moment from the reel's own source clip strip
       (spec v5.8b: "'add segment' (pick any moment from the source clip
       strip)") ---- */

    _openAddPicker() {
      if (!this.clip) { alert("Source clip not found — can't add a segment."); return; }
      const picker = document.getElementById("re-add-picker");
      if (!picker) return;
      picker.hidden = false;
      this._renderAddPickerStrip();
    },

    _closeAddPicker() {
      const picker = document.getElementById("re-add-picker");
      if (picker) picker.hidden = true;
    },

    _renderAddPickerStrip() {
      const wrap = document.getElementById("re-add-strip-wrap");
      const film = document.getElementById("re-add-strip-film");
      if (!wrap || !film || !this.clip) return;
      const dur = this.clip.info?.duration || 1;
      const trackW = wrap.clientWidth || 600;
      const pxPerSec = trackW / dur;
      const thumbs = this._segThumbs[this.clip.id];
      if (thumbs?.meta && thumbs?.stripUrl) {
        const bgW = Math.max(1, thumbs.meta.count * thumbs.meta.interval_s * pxPerSec);
        film.style.backgroundImage = `url('${thumbs.stripUrl}')`;
        film.style.backgroundSize = `${bgW.toFixed(1)}px 100%`;
        film.style.backgroundPosition = "0 0";
      } else {
        film.style.backgroundImage = "";
      }
    },

    _addSegmentAt(t) {
      const dur = this.clip?.info?.duration || (t + 3);
      const start = Math.max(0, Math.min(t, Math.max(0, dur - 0.2)));
      const end = Math.min(dur, start + 3);
      const newSeg = {
        clip_id: this.clip.id,
        start: Math.round(start * 1000) / 1000,
        end: Math.round(end * 1000) / 1000,
        in_override: null,
        out_override: null,
      };
      const segs = [...this._segments(), newSeg];
      const trans = [...this._transitions(), { type: "crossfade", duration: 0.4 }];
      this._closeAddPicker();
      this._commitSegments(segs, trans, () => this._afterSegmentsChange());
    },

    /* ---------- tabs ---------- */

    switchTab(tab) {
      this.activeTab = tab;
      const insp = document.getElementById("re-inspector");
      if (!insp) return;
      insp.querySelectorAll("[data-re-tab]").forEach((b) => b.classList.toggle("active", b.dataset.reTab === tab));
      insp.querySelectorAll(".re-panel").forEach((p) => { p.hidden = p.dataset.panel !== tab; });
      if (tab === "reel") this._renderReelTab();
      else if (tab === "subs") this._renderSubsTab();
      else if (tab === "export") this._renderExportTab();
    },

    /* ---- Reel tab: title/description editable, hashtags + copy, regenerate,
       segment count/duration/score readonly, "Compuesto" badge for composer-
       combined reels (spec v5.8b) ---- */

    _renderReelTab() {
      const el = document.getElementById("re-panel-reel");
      if (!el) return;
      const reel = this._reel();
      const segs = this._segments();
      el.innerHTML = `
        <div class="card">
          <b>Reel</b>
          <div class="field-row"><label>Title</label>
            <input type="text" id="re-title-input" maxlength="80" value="${esc(reel.title || "")}"></div>
          <div class="field-row" style="align-items:flex-start"><label>Description</label>
            <textarea id="re-desc-input" rows="5" style="flex:1;resize:vertical">${esc(reel.description || "")}</textarea></div>
          <div class="chip-row">${(() => {
            const tags = _validHashtags(reel.hashtags);
            return tags.length
              ? tags.map((h) => `<span class="pill">${esc(h.startsWith("#") ? h : "#" + h)}</span>`).join("")
              : '<span class="dim">No hashtags yet.</span>';
          })()}</div>
          <div class="row" style="margin-top:6px">
            <button class="btn small" id="re-copy-btn"><i data-lucide="copy"></i> Copy</button>
            <button class="btn small" id="re-regen-btn"><i data-lucide="refresh-cw"></i> Regenerate copy</button>
          </div>
          <div class="re-readonly-row" style="margin-top:12px">
            <span>Segments <b>${segs.length}</b></span>
            <span>Duration <b>${fmtT(this._totalDuration())}</b></span>
            <span>Score <b class="score">${reel.score ?? "—"}</b></span>
            ${reel.composed ? '<span class="pill main">Compuesto</span>' : ""}
          </div>
          ${reel.composed && reel.composer_why ? `<div class="dim" style="margin-top:6px">${esc(reel.composer_why)}</div>` : ""}
        </div>
        <div class="card" id="re-framing-card">${this._framingCardHtml()}</div>`;

      const titleInput = el.querySelector("#re-title-input");
      if (titleInput) titleInput.oninput = () => this._debouncedPatch("title", { title: titleInput.value }, 600, () => {
        const heading = document.getElementById("re-heading-title");
        if (heading) heading.textContent = titleInput.value || `Reel #${reel.rank}`;
      });
      const descInput = el.querySelector("#re-desc-input");
      if (descInput) descInput.oninput = () => this._debouncedPatch("description", { description: descInput.value }, 600);

      const copyBtn = el.querySelector("#re-copy-btn");
      if (copyBtn) copyBtn.onclick = async () => {
        const text = [reel.title, "", reel.description, "", _hashtagText(reel.hashtags)].join("\n");
        const ok = await _copyToClipboard(text);
        copyBtn.innerHTML = ok ? '<i data-lucide="check"></i> Copied' : "Copy failed";
        refreshIcons();
        setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i> Copy'; refreshIcons(); }, 1500);
      };
      const regenBtn = el.querySelector("#re-regen-btn");
      if (regenBtn) regenBtn.onclick = async () => {
        regenBtn.disabled = true;
        regenBtn.textContent = "Regenerating…";
        try {
          const updated = await api(`/projects/${this.pid}/reels/${this.rid}/regenerate-copy`, { method: "POST" });
          this._mergeReel(updated);
          this._renderReelTab();
        } catch (e) {
          alert(`Regenerate failed: ${e.message}`);
          regenBtn.disabled = false;
          regenBtn.innerHTML = '<i data-lucide="refresh-cw"></i> Regenerate copy';
          refreshIcons();
        }
      };

      this._wireFramingCard(el.querySelector("#re-framing-card"));
      refreshIcons();
    },

    /* ---- Framing card (spec v7.11): secondary zoom slider + reset button
       alongside the primary direct-manipulation gesture on the preview
       stage itself (drag/wheel/dblclick, see the framing section above) --
       kept as its own small render/wire pair (rather than folded into
       _renderReelTab's one-shot innerHTML) so a drag/zoom-driven PATCH can
       refresh JUST this card's numbers without rebuilding the whole Reel
       tab (which would blow away focus/caret position in the title/
       description inputs mid-edit). ---- */

    _framingCardHtml() {
      const t = this._currentTransform();
      const pct = Math.round(t.zoom * 100);
      return `
        <b>Framing</b>
        <div class="hint">Drag the preview to pan, scroll/pinch to zoom, double-click to reset to the
          AI face-centered default. Zooming out below 100% reveals an automatic blurred background
          (the source's own aspect, never stretched to 9:16).</div>
        <div class="field-row" style="margin-top:8px">
          <label>Zoom</label>
          <input type="range" id="re-zoom-input" min="${TRANSFORM_ZOOM_MIN}" max="${TRANSFORM_ZOOM_MAX}" step="0.01"
            value="${t.zoom}" style="flex:1">
          <span class="dim mono" id="re-zoom-val">${pct}%</span>
        </div>
        <div class="row" style="margin-top:8px">
          <button type="button" class="btn small" id="re-reset-framing-btn"><i data-lucide="rotate-ccw"></i> Reset to auto</button>
        </div>`;
    },

    _renderFramingCard() {
      const card = document.getElementById("re-framing-card");
      if (!card) return;
      card.innerHTML = this._framingCardHtml();
      this._wireFramingCard(card);
      refreshIcons();
    },

    _wireFramingCard(card) {
      if (!card) return;
      const zoomInput = card.querySelector("#re-zoom-input");
      if (zoomInput) zoomInput.oninput = () => {
        const zoom = _clampNum(Number(zoomInput.value) || 1, TRANSFORM_ZOOM_MIN, TRANSFORM_ZOOM_MAX);
        const val = document.getElementById("re-zoom-val");
        if (val) val.textContent = `${Math.round(zoom * 100)}%`;
        const cur = this._currentTransform();
        this._transformPreview = { zoom, offset_x: cur.offset_x, offset_y: cur.offset_y };
        this._renderStage();
        this._debouncedCommitTransform(this._transformPreview, 250);
      };
      const resetBtn = card.querySelector("#re-reset-framing-btn");
      if (resetBtn) resetBtn.onclick = () => this._resetTransform();
    },

    /* ---- Subs tab: style overrides (partial over project defaults) + cue list ---- */

    async _loadSubsBase() {
      try {
        this._subsBase = await api(`/projects/${this.pid}/subtitles`);
      } catch (_e) {
        this._subsBase = null;
      }
      try {
        if (!_fontsCache) _fontsCache = (await api("/fonts")).fonts || [];
        this._fonts = _fontsCache;
      } catch (_e) {
        this._fonts = [];
      }
      if (this.activeTab === "subs") this._renderSubsTab();
    },

    async _reloadCues() {
      try {
        const { cues } = await api(`/projects/${this.pid}/reels/${this.rid}/cues`);
        this.cues = cues || [];
      } catch (_e) {
        this.cues = [];
      }
    },

    _renderSubsTab() {
      const el = document.getElementById("re-panel-subs");
      if (!el) return;
      if (!this._subsBase) {
        el.innerHTML = '<div class="card"><b>Subs</b><div class="hint">Loading…</div></div>';
        return;
      }
      const reel = this._reel();
      const cfg = { ...this._subsBase, ...(reel.subtitle_style || {}) };
      const fonts = this._fonts?.length ? this._fonts : [cfg.font];

      el.innerHTML = `
        <div class="card">
          <b>Style</b>
          <div class="hint">Overrides just this reel — unset fields fall back to the project's subtitles defaults.</div>
          <label class="dim" style="display:block;margin-bottom:2px">Style</label>
          <div class="transition-btns" id="re-sub-styles">
            ${SUB_STYLES.map(([k, l]) => `<button class="btn small ${cfg.style === k ? "active" : ""}" data-v="${k}">${l}</button>`).join("")}
          </div>
          <div class="field-row"><label>Font</label>
            <select id="re-sub-font" style="flex:1">
              ${fonts.map((f) => `<option value="${esc(f)}" ${cfg.font === f ? "selected" : ""}>${esc(f)}</option>`).join("")}
            </select></div>
          <label class="dim" style="display:block;margin:8px 0 2px">Size</label>
          <div class="transition-btns" id="re-sub-sizes">
            ${SUB_SIZES.map(([k, l]) => `<button class="btn small ${cfg.size === k ? "active" : ""}" data-v="${k}">${l}</button>`).join("")}
          </div>
          <label class="dim" style="display:block;margin:8px 0 2px">Position</label>
          <div class="transition-btns" id="re-sub-positions">
            ${SUB_POSITIONS.map(([k, l]) => `<button class="btn small ${cfg.position === k ? "active" : ""}" data-v="${k}">${l}</button>`).join("")}
          </div>
          <div class="swatch-row">
            <label>Text <input type="color" id="re-sub-color" value="${cfg.color}"></label>
            <label>Outline <input type="color" id="re-sub-outline" value="${cfg.outline_color}"></label>
          </div>
          <div class="field-row"><label>Words/cue</label>
            <input type="number" id="re-sub-wpc" min="1" max="12" step="1" value="${cfg.words_per_cue}"></div>
        </div>
        <div class="card">
          <b>Cues</b>
          <div class="hint">Typo fixes for this reel's burned-in captions (times are relative to each segment's trimmed window).</div>
          <div id="re-cue-list">${this._cueListHtml()}</div>
        </div>`;

      el.querySelectorAll("#re-sub-styles button").forEach((b) => b.onclick = () => this._patchSubtitleStyle("style", b.dataset.v));
      el.querySelectorAll("#re-sub-sizes button").forEach((b) => b.onclick = () => this._patchSubtitleStyle("size", b.dataset.v));
      el.querySelectorAll("#re-sub-positions button").forEach((b) => b.onclick = () => this._patchSubtitleStyle("position", b.dataset.v));
      const fontSel = el.querySelector("#re-sub-font");
      if (fontSel) fontSel.onchange = () => this._patchSubtitleStyle("font", fontSel.value);
      const colorInput = el.querySelector("#re-sub-color");
      if (colorInput) colorInput.oninput = () => this._debouncedPatchSubtitleStyle("color", colorInput.value);
      const outlineInput = el.querySelector("#re-sub-outline");
      if (outlineInput) outlineInput.oninput = () => this._debouncedPatchSubtitleStyle("outline_color", outlineInput.value);
      const wpcInput = el.querySelector("#re-sub-wpc");
      if (wpcInput) wpcInput.onchange = () =>
        this._patchSubtitleStyle("words_per_cue", Math.max(1, Math.min(12, Number(wpcInput.value) || 4)), { refetchCues: true });

      this._wireCueInputs(el);
    },

    _cueListHtml() {
      if (!this.cues.length) return '<div class="dim">No cues in this window yet.</div>';
      const multi = this._segments().length > 1;
      return this.cues.map((c) => `
        <div class="field-row" data-cue="${c.index}">
          <label class="mono">${multi ? `S${(c.segment ?? 0) + 1} · ` : ""}${fmtT(c.start)}</label>
          <input type="text" class="re-cue-input" data-idx="${c.index}" value="${esc(c.text)}">
        </div>`).join("");
    },

    _wireCueInputs(el) {
      el.querySelectorAll(".re-cue-input").forEach((inp) => {
        inp.oninput = () => {
          const idx = inp.dataset.idx;
          this._debouncedPatch(`cue-${idx}`, { cue_overrides: { [idx]: inp.value } }, 500);
        };
      });
    },

    _patchSubtitleStyle(field, value, opts = {}) {
      this._patch({ subtitle_style: { [field]: value } }, {
        afterSave: () => {
          this._renderSubsTab();
          if (opts.refetchCues || field === "words_per_cue") this._reloadCues().then(() => this._renderSubsTab());
        },
      });
    },

    _debouncedPatchSubtitleStyle(field, value) {
      clearTimeout(this._timers[`style-${field}`]);
      this._timers[`style-${field}`] = setTimeout(() => this._patchSubtitleStyle(field, value), 300);
    },

    /* ---- Export tab: render -> queue, status, last file + Open in Finder ---- */

    _renderExportTab() {
      const el = document.getElementById("re-panel-export");
      if (!el) return;
      const reel = this._reel();
      const kind = `reel_render:${this.rid}`;
      const active = (state.queue || []).find((i) => i.kind === kind && (i.status === "pending" || i.status === "running"));
      const last = [...(state.queue || [])].reverse().find((i) => i.kind === kind);
      const busy = !!active;
      const statusText = active
        ? (active.status === "running" ? "Rendering…" : "Queued…")
        : reel.status === "rendered" ? "Rendered"
        : reel.status === "edited" ? "Edited — not yet re-rendered"
        : "Not rendered yet";

      const sig = `${busy}|${reel.path || ""}|${reel.status}|${last?.status || ""}`;
      if (this._exportSig === sig) return; // avoid reloading the <video> on every 1s tick for no reason
      this._exportSig = sig;

      el.innerHTML = `
        <div class="card">
          <b>Export</b>
          <div class="row">
            <button class="btn primary small" id="re-render-btn" ${busy ? "disabled" : ""}>${busy ? "Rendering…" : '<i data-lucide="play"></i> Render'}</button>
            <span class="dim">${esc(statusText)}</span>
          </div>
          ${last?.status === "error" ? `<div class="dim" style="color:var(--danger);margin-top:6px">${esc(last.error || "Render failed")}</div>` : ""}
          ${reel.path ? `
            <div style="margin-top:10px">
              <video controls preload="metadata" src="/api/projects/${this.pid}/media/reel-preview/${this.rid}"></video>
              <div class="row" style="margin-top:6px">
                <button class="btn small" id="re-open-folder"><i data-lucide="folder-open"></i> Open in Finder</button>
              </div>
            </div>` : '<div class="hint" style="margin-top:8px">No render yet — click Render to produce the 9:16 file.</div>'}
        </div>`;

      const renderBtn = el.querySelector("#re-render-btn");
      if (renderBtn) renderBtn.onclick = async () => {
        try {
          await api(`/projects/${this.pid}/reels/${this.rid}/render`, { method: "POST" });
          this._exportSig = null;
          await pollQueue();
          this._renderExportTab();
        } catch (e) {
          alert(`Couldn't queue render: ${e.message}`);
        }
      };
      const openBtn = el.querySelector("#re-open-folder");
      if (openBtn) openBtn.onclick = async () => {
        const dir = (reel.path || "").replace(/\/[^/]*$/, "");
        if (!dir) return;
        try { await api("/open-folder", { method: "POST", body: { path: dir } }); }
        catch (e) { alert(`Couldn't open folder: ${e.message}`); }
      };
      refreshIcons();
    },

    /* ---------- 1s tick while open: export status + auto-close if the
       project switcher moved away from the project this reel belongs to
       (that switcher lives in core.js's topbar and isn't ours to lock) ---------- */

    _startTick() {
      this._stopTick();
      this._tickTimer = setInterval(() => {
        if (!this.isOpen) return;
        if (state.pid !== this.pid) { this.close(); return; }
        this._renderExportTab();
      }, 1000);
    },
    _stopTick() {
      clearInterval(this._tickTimer);
      this._tickTimer = null;
    },
  };

  window.ReelEditor = ReelEditor;
})();
