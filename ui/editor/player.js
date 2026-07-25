/* Virtual EDL playback + rendered-preview playback (spec v4 §3):

   - Draft mode (existing): plays the ordered segment list by seeking the
     underlying clip <video> and auto-advancing at each segment's out point —
     no render needed. Two <video> elements are kept in the DOM ("active"
     visible/playing, "idle" hidden); whenever the *next* segment comes from
     a different clip file we preload it into the idle element ahead of time
     and swap active<->idle at the junction instead of reloading. At a
     junction whose *incoming* transition (Editor.segments[i].transition —
     "the transition INTO that segment") is "fade", a to-black overlay div
     quick-fades; "crossfade" cross-fades the two stacked <video> elements'
     opacity over the configured duration (approximating what render.py
     actually bakes in with ffmpeg).
   - Preview mode (new): plays <project>/preview/preview.mp4 (the last
     preview_render job's output) via the generic media/file endpoint. Auto-
     selected over Draft whenever Editor.previewIsStale() says the current
     edit state matches project.preview.manifest; falls back to Draft
     otherwise. A toggle button lets the user override for the current view.

   Subtitle overlay (spec v4 §6): a DOM div fetches /subtitles/cues once per
   EDL change (also on onProjectRefreshed — color/audio/subtitles panels call
   refreshProject() after saving, per ui/editor/inspector.js's pattern) and
   shows the current cue, styled per GET /subtitles config, live in both
   modes — reading the "effective EDL time" (draft: the virtual player's
   position; preview: the rendered file's own currentTime, since it was
   encoded from the same EDL 1:1... except when a crossfade transition
   shortens the render at a junction, which this cannot see — a documented
   approximation, see the final report).

   ---------- v5.14 FIX: "playback dies after editing colors" ----------
   Root cause #1 (this file, the one owned bug): refreshProject() fires on
   EVERY project refresh — not just user saves — including the moment a
   background preview_render job (auto-enqueued by a debounced color/EDL/
   subtitles/audio edit) finishes, which lands here via onProjectRefreshed()
   at a completely arbitrary instant, possibly mid-playback. The old code
   called _autoSelectMode() unconditionally from there, which could flip
   Draft<->Preview WHILE the user was actively watching either video — and
   if it flipped into Preview at the exact moment preview.mp4 was still
   being rewritten in place, the <video> loaded a truncated file and stalled
   forever (readyState stuck, no error event). Root causes #2 (atomic
   preview.tmp.mp4 -> os.replace()) and the manifest-only-after-rename
   ordering are pipeline/render.py's fix (OVERLAY-BACKEND agent), not ours.
   This file's half of the fix policy:
     1. NEVER call setMode() while `this.playing` is true, from any AUTO
        path (_autoSelectMode). A manual click (setMode(mode,{manual:true}))
        always still works instantly — the user's explicit action always
        wins, playing or not.
     2. Auto-select only runs for real on project open (mount() -> the
        initial onSegmentsChanged() call, where playing is always false)
        and whenever the player is paused (checked again right when pause()
        fires, so the mode settles the instant it's safe to touch).
     3. While playing, if a fresh non-stale preview lands, we don't switch —
        we light up a "Preview ready" glow on the mode-toggle button
        instead (_setReadyGlow) so the user can switch by hand.
     4. The preview <video> src is cache-busted with `?v=<manifest hash>`
        (_previewSrc) so a previously half-loaded/stale preview.mp4 is never
        reused just because the path string didn't change.
   Regression-checked by code walkthrough — see this task's final report.

   ---------- v7 §7.1: Source mode ----------
   Clicking (or double-clicking) a clip in the media bin (ui/editor/
   mediabin.js calls Player.enterSourceMode(clipId)) plays that clip's own
   preview proxy from 0 in a THIRD, independent <video id="video-source">
   (draft/preview videos hidden underneath). A "Source: <name>" chip appears
   in the player controls with an X to return to Edit mode; Esc (wired in
   ui/core.js) and clicking anywhere in the timeline OTHER than the ruler
   also exit (wired below via a capture-phase listener on #timeline-content
   -- ui/editor/timeline.js is owned by another agent this phase, so this
   file adds its own listener rather than editing that one, per the task
   brief's fallback instruction). While in Source mode the EDL track is
   dimmed AND inert (a CSS class toggled on #timeline-content, since there is
   no window event to hook into for this); the ruler stays interactive but is
   repurposed to scrub the FULL SOURCE CLIP instead of the EDL (mapped 0..1
   across the ruler's on-screen width -- a deliberate simplification: the
   ruler's ticks still read the EDL's own scale/labels underneath, since only
   ui/editor/timeline.js owns what's drawn there; only the scrub gesture
   itself is repurposed). Transport keys (play/pause/J/K/L) operate on the
   source video while active; the EDL playhead and subtitle overlay are left
   alone (both are meaningless for a raw, un-cut clip).

   ---------- v7 §7.6: Subtitle inline edit on the player ----------
   While paused and subtitles are enabled, the overlay becomes interactive
   (pointer-events flips on): double-click starts a contentEditable inline
   edit of the CURRENT cue (Enter or blur commits, Esc cancels) AND opens the
   Subs inspector tab scrolled/focused to that cue's row (ui/editor/
   inspector.js's Inspector.switchTab + the new window.SubtitlesPanel.
   focusCue, ui/panels/subtitles.js -- both read as globals here, not
   imported, per this app's plain-script-tag convention). A commit saves via
   window.SubtitlesPanel.setCueOverride(project, index, text), which PUTs the
   FULL subtitles config (project["subtitles"]["cue_overrides"], keyed by
   cue_list()'s GLOBAL index -- see magic_video_editor/pipeline/subtitles.py).
   Vertical drag on the (paused, non-editing) overlay nudges the subtitle's
   vertical position live (translateY, no network calls mid-drag) and
   persists the result on release via SubtitlesPanel.saveStyleField --
   snapping back to whichever of "bottom"/"center" it ends up closest to
   (spec: "snaps to bottom/center presets when close"), otherwise keeping the
   free offset in the NEW cfg.vpos field. While playing, the overlay is
   fully non-interactive (pointer-events off) — untouched by any of this. */

window.EditorUI = window.EditorUI || {};

const Player = {
  videos: null,
  activeIdx: 0,
  playing: false,
  epsilon: 0.05,
  _currentIndex: 0,
  _rafId: null,
  _rate: 1,

  mode: "draft",         // "draft" | "preview"
  _cues: [],
  _subtitleCfg: null,
  _previewVideo: null,
  _fadeOverlay: null,
  _subtitleEl: null,
  _readyGlow: false,     // v5.14: "a fresh preview landed but we're playing — glow, don't switch"

  // ---- v7 §7.1 Source mode ----
  sourceClipId: null,    // set while previewing a media-bin clip standalone
  _sourceVideo: null,
  _sourceDuration: 0,
  _sourceChip: null,
  _tlCaptureHandler: null,

  // ---- v7 §7.6 subtitle inline edit / vertical drag ----
  _editingCue: null,     // {index} while the overlay is contentEditable
  _subDrag: null,        // {startY, startVpos, moved} while dragging the overlay vertically

  mount() {
    this.videos = [document.getElementById("video-a"), document.getElementById("video-b")];
    if (!this.videos[0] || !this.videos[1]) return;
    this.activeIdx = 0;
    this._currentIndex = 0;
    this.videos.forEach((v, i) => {
      v.classList.toggle("active", i === 0);
      v.onerror = () => console.error("player video error", v.dataset.clipId, v.error);
    });
    this._active().ontimeupdate = () => this._onTimeUpdate();

    this._ensureChrome();

    const playBtn = document.getElementById("pp-playpause");
    const fsBtn = document.getElementById("pp-fullscreen");
    if (playBtn) playBtn.onclick = () => this.togglePlay();
    if (fsBtn) fsBtn.onclick = () => this.toggleFullscreen();

    this._wireSourceMode();
    this._wireSubtitleOverlayInteraction();

    this.onSegmentsChanged();
    if (!this._rafId) this._startLoop();
  },

  /* ---------- v7 §7.1 Source mode: timeline hookup ----------
     ui/editor/timeline.js is owned by another agent this phase and exposes
     no event/hook for "something else wants the ruler + wants clicks
     elsewhere in the timeline to mean something different" — so this file
     adds its OWN capture-phase listener on the shared #timeline-content
     element instead of editing that one. Capture phase means this runs
     BEFORE any of timeline.js's own bubble-phase handlers (block drag,
     background-click-seek, chip click, …), and stopPropagation() here stops
     those from ALSO firing — exactly "the EDL track stays untouched" while
     in Source mode. Wired once; harmlessly re-wired if mount() ever runs
     twice (removeEventListener first). */
  _wireSourceMode() {
    const content = document.getElementById("timeline-content");
    if (!content) return;
    if (this._tlCaptureHandler) content.removeEventListener("pointerdown", this._tlCaptureHandler, true);
    this._tlCaptureHandler = (e) => {
      if (!this.sourceClipId) return;
      if (e.target.closest("#timeline-ruler")) {
        e.stopPropagation();
        this._onSourceRulerPointerDown(e);
        return;
      }
      e.stopPropagation();
      this.exitSourceMode();
    };
    content.addEventListener("pointerdown", this._tlCaptureHandler, true);

    const exitBtn = document.getElementById("pp-source-exit");
    if (exitBtn) exitBtn.onclick = () => this.exitSourceMode();
  },

  enterSourceMode(clipId) {
    const clip = Editor?.clip?.(clipId);
    if (!clip || !Editor?.pid) return;
    if (this.sourceClipId === clipId) return; // already the active source clip
    this.pause({ auto: false });
    this.sourceClipId = clipId;
    this._sourceDuration = clip.info?.duration || 0;
    this.videos?.forEach((v) => (v.style.visibility = "hidden"));
    if (this._previewVideo) this._previewVideo.style.visibility = "hidden";
    if (this._subtitleEl) this._subtitleEl.style.display = "none";
    const empty = document.getElementById("player-empty");
    if (empty) empty.style.display = "none";

    const v = this._sourceVideo;
    if (v) {
      v.style.visibility = "visible";
      const start = () => { try { v.currentTime = 0; } catch (_e) { /* not ready */ } v.play().catch(() => {}); };
      if (v.dataset.clipId === clipId) start();
      else {
        v.dataset.clipId = clipId;
        v.src = `/api/projects/${Editor.pid}/media/preview/${clipId}`;
        v.onloadedmetadata = start;
      }
    }
    if (this._sourceChip) {
      this._sourceChip.hidden = false;
      const nameEl = document.getElementById("pp-source-name");
      if (nameEl) nameEl.textContent = clip.filename || clipId;
    }
    document.getElementById("timeline-content")?.classList.add("tl-source-dim");

    this.playing = true;
    const btn = document.getElementById("pp-playpause");
    if (btn) { btn.innerHTML = '<i data-lucide="pause"></i>'; refreshIcons(); }
    this._updateSubtitleInteractivity();
  },

  exitSourceMode() {
    if (!this.sourceClipId) return;
    this.sourceClipId = null;
    if (this._sourceVideo) { this._sourceVideo.pause(); this._sourceVideo.style.visibility = "hidden"; }
    if (this._sourceChip) this._sourceChip.hidden = true;
    document.getElementById("timeline-content")?.classList.remove("tl-source-dim");
    this.playing = false;

    if (this.mode === "preview" && this._previewVideo) this._previewVideo.style.visibility = "visible";
    this._showEmpty(!Editor.segments?.length);
    if (this.mode === "draft" && Editor.segments?.length) {
      const i = Math.min(Editor.selected, Editor.segments.length - 1);
      this._loadSegment(i, Editor.segments[i].start, { andPlay: false });
    }
    const btn = document.getElementById("pp-playpause");
    if (btn) { btn.innerHTML = '<i data-lucide="play"></i>'; refreshIcons(); }
    this._updateSubtitleInteractivity();
  },

  // Ruler is repurposed, while in Source mode, into a full-clip scrub strip
  // mapped 0..1 across its own rendered (scroll-independent) width — see the
  // module docstring for why this is a deliberate simplification rather than
  // a real clip-duration ruler.
  _onSourceRulerPointerDown(e) {
    const ruler = document.getElementById("timeline-ruler");
    const dur = this._sourceDuration;
    if (!ruler || !dur) return;
    const rect = ruler.getBoundingClientRect();
    const scrub = (clientX) => {
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
      if (this._sourceVideo) { try { this._sourceVideo.currentTime = frac * dur; } catch (_e) { /* ignore */ } }
    };
    scrub(e.clientX);
    const onMove = (ev) => scrub(ev.clientX);
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  /* ---------- injected chrome: mode toggle, subtitle overlay, fade overlay
     (ui/index.html/ui/style.css belong to other agents this phase) ---------- */
  _ensureChrome() {
    if (!document.getElementById("player-pro-styles")) {
      const style = document.createElement("style");
      style.id = "player-pro-styles";
      style.textContent = `
        #subtitle-overlay { position: absolute; left: 6%; right: 6%; z-index: 5; text-align: center;
          pointer-events: none; font-weight: 700; line-height: 1.25; white-space: pre-wrap;
          text-shadow: 0 0 6px rgba(0,0,0,.85), 0 2px 4px rgba(0,0,0,.85); }
        #subtitle-overlay.pos-bottom { bottom: 8%; }
        #subtitle-overlay.pos-center { top: 50%; transform: translateY(-50%); }
        #draft-fade-overlay { position: absolute; inset: 0; z-index: 6; background: #000; opacity: 0;
          pointer-events: none; }
        .pp-mode-btn.active { border-color: var(--accent2); color: var(--accent2); }
        /* v5.14: "Preview ready" affordance — a fresh preview landed while
           playing (or before a boundary pause), shown instead of auto-
           switching mid-playback. Purely a hint; clicking still just does
           what the button always does (toggle mode). */
        .pp-mode-btn.ready-glow { border-color: var(--accent2); box-shadow: 0 0 0 1px var(--accent2),
          0 0 10px rgba(53,194,143,.65); animation: pp-ready-pulse 1.6s ease-in-out infinite; }
        @keyframes pp-ready-pulse {
          0%, 100% { box-shadow: 0 0 0 1px var(--accent2), 0 0 6px rgba(53,194,143,.4); }
          50% { box-shadow: 0 0 0 1px var(--accent2), 0 0 14px rgba(53,194,143,.85); }
        }

        /* ---- v7 §7.1 Source mode ---- */
        #pp-source-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px 4px 10px;
          border-radius: 999px; border: 1px solid var(--accent); background: var(--panel2);
          color: var(--text); font-size: 12px; line-height: 1; }
        #pp-source-chip i:first-child { width: 13px; height: 13px; color: var(--accent-hover); }
        #pp-source-name { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        #pp-source-exit { padding: 2px; }
        /* dims + disables the EDL track (NOT the ruler, which is repurposed
           for source-clip scrubbing) while Source mode is active — this file
           toggles the class; the rule itself lives here since ui/style.css
           belongs to another agent this phase. */
        #timeline-content.tl-source-dim #timeline-track,
        #timeline-content.tl-source-dim #timeline-playhead,
        #timeline-content.tl-source-dim #timeline-markers,
        #timeline-content.tl-source-dim #timeline-overlay-track { opacity: .3; pointer-events: none; }

        /* ---- v7 §7.6 subtitle inline edit / drag ---- */
        #subtitle-overlay.pp-subs-interactive { pointer-events: auto; cursor: grab; }
        #subtitle-overlay.pp-subs-editing { pointer-events: auto; cursor: text; outline: 1px dashed var(--accent2);
          outline-offset: 4px; background: rgba(0,0,0,.35); border-radius: 6px; }
      `;
      document.head.appendChild(style);
    }
    const stage = document.getElementById("player-stage");
    if (stage && !document.getElementById("subtitle-overlay")) {
      const sub = document.createElement("div");
      sub.id = "subtitle-overlay";
      sub.className = "pos-bottom";
      stage.appendChild(sub);
      this._subtitleEl = sub;
    } else {
      this._subtitleEl = document.getElementById("subtitle-overlay");
    }
    if (stage && !document.getElementById("draft-fade-overlay")) {
      const fade = document.createElement("div");
      fade.id = "draft-fade-overlay";
      stage.appendChild(fade);
      this._fadeOverlay = fade;
    } else {
      this._fadeOverlay = document.getElementById("draft-fade-overlay");
    }
    if (stage && !document.getElementById("video-preview")) {
      const v = document.createElement("video");
      v.id = "video-preview";
      v.className = "player-video";
      v.preload = "auto";
      v.playsInline = true;
      v.style.visibility = "hidden";
      v.onerror = () => console.error("preview video error", v.error);
      stage.insertBefore(v, this._subtitleEl || null);
      this._previewVideo = v;
    } else {
      this._previewVideo = document.getElementById("video-preview");
    }
    // v7 §7.1: a fourth, fully independent <video> for Source mode — kept
    // separate from the draft/preview elements so entering/exiting it never
    // disturbs their src/currentTime/ontimeupdate state.
    if (stage && !document.getElementById("video-source")) {
      const v = document.createElement("video");
      v.id = "video-source";
      v.className = "player-video";
      v.preload = "auto";
      v.playsInline = true;
      v.style.visibility = "hidden";
      v.onerror = () => console.error("source video error", v.error);
      stage.insertBefore(v, this._subtitleEl || null);
      this._sourceVideo = v;
    } else {
      this._sourceVideo = document.getElementById("video-source");
    }
    const controls = document.querySelector(".player-controls");
    if (controls && !document.getElementById("pp-mode-toggle")) {
      const btn = document.createElement("button");
      btn.id = "pp-mode-toggle";
      btn.className = "btn small pp-mode-btn";
      btn.title = "Draft = live virtual cut, no render needed. Preview = last rendered preview.mp4.";
      btn.onclick = () => this.setMode(this.mode === "draft" ? "preview" : "draft", { manual: true });
      const fsBtn = document.getElementById("pp-fullscreen");
      controls.insertBefore(btn, fsBtn || null);
    }
    // v7 §7.1: "Source: <name>" chip, X to return to Edit mode.
    if (controls && !document.getElementById("pp-source-chip")) {
      const chip = document.createElement("span");
      chip.id = "pp-source-chip";
      chip.hidden = true;
      chip.innerHTML = '<i data-lucide="film"></i><span id="pp-source-name"></span>'
        + '<button id="pp-source-exit" class="icon-btn" title="Back to Edit (Esc)"><i data-lucide="x"></i></button>';
      const playBtn = document.getElementById("pp-playpause");
      controls.insertBefore(chip, playBtn ? playBtn.nextSibling : controls.firstChild);
      this._sourceChip = chip;
    } else {
      this._sourceChip = document.getElementById("pp-source-chip");
    }
    this._updateModeButton();
  },

  _updateModeButton() {
    const btn = document.getElementById("pp-mode-toggle");
    if (!btn) return;
    btn.innerHTML = this.mode === "preview"
      ? '<i data-lucide="clapperboard"></i> Preview'
      : '<i data-lucide="pencil"></i> Draft';
    btn.classList.toggle("active", this.mode === "preview");
    // Switching TO preview always resolves whatever the glow was inviting.
    if (this.mode === "preview") this._readyGlow = false;
    btn.classList.toggle("ready-glow", this._readyGlow && this.mode === "draft");
    refreshIcons();
  },

  // v5.14: subtle "a fresh preview landed" affordance instead of an auto
  // mode-flip while the user might be mid-playback/mid-edit-gesture.
  _setReadyGlow(on) {
    on = !!on && this.mode === "draft";
    if (this._readyGlow === on) return;
    this._readyGlow = on;
    document.getElementById("pp-mode-toggle")?.classList.toggle("ready-glow", on);
  },

  /* ---------- mode switching (spec v4 §3) ---------- */
  setMode(mode, { manual = false } = {}) {
    if (mode === this.mode) { this._updateModeButton(); return; }
    const wasPlaying = this.playing;
    const t = this.currentEdlTime();
    this.pause({ auto: false });
    this.mode = mode;
    this._updateModeButton();
    if (mode === "preview") {
      this.videos.forEach((v) => (v.style.visibility = "hidden"));
      if (this._previewVideo) this._previewVideo.style.visibility = "visible";
      this._loadPreviewVideo(() => this.seekToEdlTime(t, { andPlay: wasPlaying }));
    } else {
      if (this._previewVideo) this._previewVideo.style.visibility = "hidden";
      this._showEmpty(!Editor.segments?.length);
      this.seekToEdlTime(t, { andPlay: wasPlaying });
    }
    if (manual) return; // user's explicit click always wins for this instant
  },

  /* v5.14 fix policy: this is the ONLY function that decides an automatic
     mode switch, and it must never fire one while `this.playing` is true —
     that mid-playback flip (compounded by preview.mp4 being rewritten in
     place) was the actual crash. Safe moments to actually flip: project
     open (called from mount()'s onSegmentsChanged(), always paused then)
     and whenever the player is genuinely paused (checked again from
     pause() itself, so the mode settles the instant it becomes safe).
     While playing, a fresh non-stale preview only lights the "Preview
     ready" glow — the user's own toggle click always still works. */
  async _autoSelectMode() {
    if (!state.project?.preview?.path) {
      this._setReadyGlow(false);
      if (!this.playing && this.mode === "preview") this.setMode("draft");
      return;
    }
    const token = (this._autoSelectToken = (this._autoSelectToken || 0) + 1);
    let stale = true;
    try { stale = await Editor.previewIsStale(); } catch (_e) { stale = true; }
    if (token !== this._autoSelectToken) return; // a newer check finished first — drop this one

    if (stale) {
      this._setReadyGlow(false);
      if (!this.playing && this.mode === "preview") this.setMode("draft");
      return;
    }
    // A fresh, matching preview exists.
    if (this.mode === "preview") { this._setReadyGlow(false); return; }
    if (this.playing) { this._setReadyGlow(true); return; } // never interrupt playback
    this._setReadyGlow(false);
    this.setMode("preview");
  },

  // v5.14 fix #3 (cache-busting): append the manifest hash so a stale/half-
  // loaded preview.mp4 is never reused just because the path string is
  // unchanged — a genuinely new render always gets a genuinely new URL.
  _previewSrc() {
    const path = state.project?.preview?.path;
    if (!path || !Editor.pid) return null;
    const manifest = state.project?.preview?.manifest;
    const bust = manifest ? `&v=${encodeURIComponent(manifest)}` : "";
    return `/api/projects/${Editor.pid}/media/file?path=${encodeURIComponent(path)}${bust}`;
  },
  _loadPreviewVideo(onReady) {
    const src = this._previewSrc();
    const v = this._previewVideo;
    if (!v || !src) return;
    if (v.dataset.src === src) { onReady?.(); return; }
    v.dataset.src = src;
    v.src = src;
    v.onloadedmetadata = () => onReady?.();
  },

  onSegmentsChanged() {
    if (!this.videos) return;
    if (!Editor.segments || !Editor.segments.length) {
      this.pause();
      this._showEmpty(true);
    } else {
      this._showEmpty(false);
      if (this.mode === "draft") {
        const i = Math.min(Editor.selected, Editor.segments.length - 1);
        this._loadSegment(i, Editor.segments[i].start, { andPlay: false });
      }
    }
    this.reloadSubtitles();
    this._autoSelectMode();
  },

  onProjectRefreshed() {
    // color/audio/subtitles panels + a finished preview_render job all land
    // here (via refreshProject() -> EditorUI.onProjectRefreshed in state.js).
    this.reloadSubtitles();
    this._autoSelectMode();
  },

  /* ---------- subtitles (spec v4 §6) ---------- */
  async reloadSubtitles() {
    await Promise.all([this._reloadSubtitleCues(), this._reloadSubtitleConfig()]);
  },
  async _reloadSubtitleCues() {
    if (!Editor.pid) return;
    try {
      const { cues } = await api(`/projects/${Editor.pid}/subtitles/cues`);
      this._cues = cues || [];
    } catch (_e) {
      this._cues = [];
    }
  },
  async _reloadSubtitleConfig() {
    if (!Editor.pid) return;
    try {
      this._subtitleCfg = await api(`/projects/${Editor.pid}/subtitles`);
    } catch (_e) {
      this._subtitleCfg = null;
    }
    this._applySubtitleStyle();
  },
  _applySubtitleStyle() {
    const el = this._subtitleEl;
    const cfg = this._subtitleCfg;
    if (!el || !cfg) return;
    const sizePx = { S: 16, M: 22, L: 30 }[cfg.size] || 22;
    el.style.fontFamily = `"${cfg.font || "Helvetica Neue"}", -apple-system, sans-serif`;
    el.style.fontSize = `${sizePx}px`;
    el.style.color = cfg.color || "#FFFFFF";
    el.style.webkitTextStroke = `1px ${cfg.outline_color || "#000000"}`;
    el.classList.toggle("pos-bottom", cfg.position !== "center");
    el.classList.toggle("pos-center", cfg.position === "center");
    this._applySubtitlePosition();
    this._updateSubtitleInteractivity();
  },
  // v7 §7.6: the live vertical-drag nudge (cfg.vpos, fraction of the stage's
  // height) layered on top of whichever preset's CSS class positions the
  // overlay — translateY works uniformly for both "bottom" (no base
  // transform) and "center" (base transform already centers it).
  _applySubtitlePosition() {
    const el = this._subtitleEl;
    const cfg = this._subtitleCfg;
    if (!el || !cfg) return;
    const stage = document.getElementById("player-stage");
    const stageH = stage?.clientHeight || 0;
    const px = Math.round((Number(cfg.vpos) || 0) * stageH);
    const base = cfg.position === "center" ? "translateY(-50%)" : "";
    el.style.transform = px ? `${base} translateY(${px}px)`.trim() : base;
  },
  _cueAt(t) {
    if (!this._cues?.length) return null;
    return this._cues.find((c) => t >= c.edl_t_start && t < c.edl_t_end) || null;
  },
  _currentSubtitleText(t) {
    if (!this._subtitleCfg?.enabled) return "";
    return this._cueAt(t)?.text || "";
  },
  _updateSubtitleOverlay() {
    if (!this._subtitleEl || this._editingCue) return; // never clobber an in-progress edit
    const text = this._currentSubtitleText(this.currentEdlTime());
    if (this._subtitleEl.textContent !== text) this._subtitleEl.textContent = text;
    this._subtitleEl.style.display = text ? "block" : "none";
  },

  /* ---------- v7 §7.6 subtitle inline edit + vertical drag ---------- */

  // Paused + enabled + not in Source mode + not already editing = the
  // overlay accepts pointer events (dblclick to edit, drag to nudge
  // position); otherwise it stays click-through, exactly as before.
  _updateSubtitleInteractivity() {
    const el = this._subtitleEl;
    if (!el) return;
    const canInteract = !this.playing && !this.sourceClipId && !!this._subtitleCfg?.enabled;
    el.classList.toggle("pp-subs-interactive", canInteract && !this._editingCue);
    if (!canInteract && this._editingCue) this._cancelCueEdit(); // e.g. playback resumed mid-edit
  },

  _wireSubtitleOverlayInteraction() {
    const el = this._subtitleEl;
    if (!el || el.dataset.wiredEdit) return;
    el.dataset.wiredEdit = "1";
    el.addEventListener("dblclick", (e) => this._onSubtitleDblClick(e));
    el.addEventListener("pointerdown", (e) => this._onSubtitlePointerDown(e));
    el.addEventListener("keydown", (e) => this._onSubtitleKeydown(e));
    el.addEventListener("blur", () => this._onSubtitleBlur());
  },

  _onSubtitleDblClick(e) {
    if (this.playing || this.sourceClipId || !this._subtitleCfg?.enabled) return;
    const cue = this._cueAt(this.currentEdlTime());
    if (!cue) return;
    e.preventDefault();
    this._startCueEdit(cue);
  },

  _startCueEdit(cue) {
    const el = this._subtitleEl;
    if (!el) return;
    this._editingCue = { index: cue.index, cancelled: false };
    el.classList.add("pp-subs-editing");
    el.contentEditable = "true";
    el.style.display = "block";
    el.textContent = cue.text;
    el.focus();
    try {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = document.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
    } catch (_e) { /* selection API quirks — non-fatal, edit still works */ }
    // Simultaneously scroll/focus the Subs inspector tab to this cue (spec
    // v7 §7.6) — both globals are owned by other files this phase
    // (ui/editor/inspector.js, ui/panels/subtitles.js) but are read here the
    // same plain-script-tag way every other cross-module call in this app is.
    try {
      window.EditorUI.inspector?.switchTab?.("subs");
      window.SubtitlesPanel?.focusCue?.(cue.index);
    } catch (err) {
      console.error("Failed to focus the Subs tab for this cue", err);
    }
  },

  _onSubtitleKeydown(e) {
    if (!this._editingCue) return;
    if (e.key === "Enter") { e.preventDefault(); this._subtitleEl?.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); this._editingCue.cancelled = true; this._subtitleEl?.blur(); }
  },

  _onSubtitleBlur() {
    if (!this._editingCue) return;
    if (this._editingCue.cancelled) { this._endCueEdit(); this._updateSubtitleOverlay(); return; }
    this._commitCueEdit();
  },

  _commitCueEdit() {
    const editing = this._editingCue;
    if (!editing) return;
    const el = this._subtitleEl;
    const text = (el?.textContent || "").trim();
    this._endCueEdit();
    const cue = this._cues.find((c) => c.index === editing.index);
    if (cue) cue.text = text; // optimistic local update — no round trip needed to reflect it
    Promise.resolve(window.SubtitlesPanel?.setCueOverride?.(state.project, editing.index, text))
      .catch((err) => console.error("Failed to save subtitle edit", err));
  },

  _cancelCueEdit() { this._endCueEdit(); },

  _endCueEdit() {
    const el = this._subtitleEl;
    this._editingCue = null;
    if (!el) return;
    el.contentEditable = "false";
    el.classList.remove("pp-subs-editing");
    this._updateSubtitleInteractivity();
  },

  // Vertical drag on the (interactive) overlay nudges cfg.vpos live via CSS
  // only; a plain click (no meaningful movement) does nothing here — dblclick
  // (separate listener above) owns entering edit mode.
  _onSubtitlePointerDown(e) {
    if (this._editingCue) return; // let contentEditable own its own clicks/selection
    if (this.playing || this.sourceClipId || !this._subtitleCfg?.enabled) return;
    const stage = document.getElementById("player-stage");
    if (!stage) return;
    const startY = e.clientY;
    const startVpos = Number(this._subtitleCfg.vpos) || 0;
    const stageH = stage.clientHeight || 1;
    let moved = false;
    const onMove = (ev) => {
      const dy = ev.clientY - startY;
      if (Math.abs(dy) > 3) moved = true;
      if (!moved) return;
      this._subtitleCfg.vpos = Math.max(-0.35, Math.min(0.35, startVpos + dy / stageH));
      this._applySubtitlePosition();
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (moved) this._snapAndSaveSubtitlePosition();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  // "snaps to bottom/center presets when close" (spec v7 §7.6): unify both
  // presets onto one "fraction down the stage" axis using the same
  // approximate baselines the CSS presets place them at (8% from the bottom
  // / dead center — see #subtitle-overlay.pos-bottom/.pos-center), so a drag
  // that started on one preset but ends up near the OTHER one switches
  // presets outright (vpos resets to 0); otherwise the free offset from
  // whichever preset is now closer is kept.
  _snapAndSaveSubtitlePosition() {
    const cfg = this._subtitleCfg;
    if (!cfg) return;
    const BASE = { bottom: 0.92, center: 0.5 };
    const SNAP = 0.06;
    const liveFrac = Math.max(0.05, Math.min(0.97, BASE[cfg.position] + (Number(cfg.vpos) || 0)));
    let position = cfg.position;
    let vpos = liveFrac - BASE[cfg.position];
    const closer = Math.abs(liveFrac - BASE.bottom) <= Math.abs(liveFrac - BASE.center) ? "bottom" : "center";
    if (Math.abs(liveFrac - BASE[closer]) < SNAP) { position = closer; vpos = 0; }
    else if (closer !== position) { position = closer; vpos = liveFrac - BASE[closer]; }
    cfg.position = position;
    cfg.vpos = Math.round(vpos * 1000) / 1000;
    this._applySubtitleStyle();
    Promise.resolve(window.SubtitlesPanel?.saveStyleField?.({ position: cfg.position, vpos: cfg.vpos }))
      .catch((err) => console.error("Failed to save subtitle position", err));
  },

  _showEmpty(show) {
    const el = document.getElementById("player-empty");
    if (el) el.style.display = show ? "flex" : "none";
    this.videos?.forEach((v) => (v.style.visibility = show ? "hidden" : (this.mode === "draft" ? "visible" : "hidden")));
  },

  _active() { return this.videos[this.activeIdx]; },
  _idle() { return this.videos[1 - this.activeIdx]; },

  _loadSegment(index, atClipTime, { andPlay }) {
    const seg = Editor.segments?.[index];
    if (!seg) return;
    this._currentIndex = index;
    const video = this._active();
    const target = atClipTime ?? seg.start;
    const doSeek = () => {
      try { video.currentTime = target; } catch (_e) { /* metadata not ready */ }
      video.playbackRate = this._rate;
      if (andPlay) video.play().catch(() => {});
    };
    if (video.dataset.clipId === seg.clip_id) {
      doSeek();
    } else {
      video.dataset.clipId = seg.clip_id;
      video.src = `/api/projects/${Editor.pid}/media/preview/${seg.clip_id}`;
      video.onloadedmetadata = doSeek;
    }
    this._preloadNext(index);
  },

  _preloadNext(index) {
    const next = Editor.segments?.[index + 1];
    const idle = this._idle();
    if (!idle) return;
    if (!next) {
      idle.removeAttribute("src");
      idle.dataset.clipId = "";
      return;
    }
    if (idle.dataset.clipId === next.clip_id) {
      try { idle.currentTime = next.start; } catch (_e) { /* not ready yet, fine */ }
      return;
    }
    idle.dataset.clipId = next.clip_id;
    idle.src = `/api/projects/${Editor.pid}/media/preview/${next.clip_id}`;
    idle.onloadedmetadata = () => { try { idle.currentTime = next.start; } catch (_e) { /* ignore */ } };
  },

  play() {
    if (this.sourceClipId) {
      // v7 §7.1: "Transport keys work on the source clip."
      this.playing = true;
      this._sourceVideo?.play().catch(() => {});
    } else if (this.mode === "preview") {
      if (!this._previewVideo?.src) return;
      this.playing = true;
      this._previewVideo.play().catch(() => {});
    } else {
      if (!Editor.segments?.length || !this.videos) return;
      this.playing = true;
      this._active().play().catch(() => {});
    }
    const btn = document.getElementById("pp-playpause");
    if (btn) { btn.innerHTML = '<i data-lucide="pause"></i>'; refreshIcons(); }
    this._updateSubtitleInteractivity(); // v7 §7.6: non-interactive again while playing
  },
  // `auto` guards against the internal pause() that setMode() itself does
  // right before flipping this.mode — re-running _autoSelectMode() there
  // would race the manual switch it's still in the middle of (e.g. flip
  // straight back to Preview a beat after the user manually chose Draft).
  // Real user-driven pauses (play/pause button, K, video ending) all go
  // through the default auto=true path.
  pause({ auto = true } = {}) {
    this.playing = false;
    if (this.sourceClipId) this._sourceVideo?.pause();
    else if (this.mode === "preview") this._previewVideo?.pause();
    else this._active()?.pause();
    const btn = document.getElementById("pp-playpause");
    if (btn) { btn.innerHTML = '<i data-lucide="play"></i>'; refreshIcons(); }
    // v5.14: "auto-select ... when paused" — the instant it's safe (no
    // active playback to interrupt), let a fresh preview settle in on its
    // own instead of leaving the user staring at a glowing toggle forever.
    // Source mode (v7 §7.1) is orthogonal to Draft/Preview and must never
    // trigger an auto mode-flip underneath it.
    if (auto && !this.sourceClipId) this._autoSelectMode();
    this._updateSubtitleInteractivity(); // v7 §7.6: interactive again now that we're paused
  },
  togglePlay() { this.playing ? this.pause() : this.play(); },

  /* ---------- J/K/L shuttle (spec v4 §4) ---------- */
  jump(deltaSec) {
    if (this.sourceClipId) {
      const v = this._sourceVideo;
      if (v) { try { v.currentTime = Math.max(0, Math.min((v.currentTime || 0) + deltaSec, this._sourceDuration || v.duration || 0)); } catch (_e) { /* ignore */ } }
      return;
    }
    const t = Math.max(0, this.currentEdlTime() + deltaSec);
    this.seekToEdlTime(t, { andPlay: this.playing });
  },
  handleK() { this._rate = 1; this.pause(); },
  handleL() {
    if (!this.playing) {
      this._rate = 1;
      this.play();
    } else {
      this._rate = this._rate === 1 ? 2 : 1;
      const v = this.sourceClipId ? this._sourceVideo : (this.mode === "preview" ? this._previewVideo : this._active());
      if (v) v.playbackRate = this._rate;
    }
  },

  toggleFullscreen() {
    const stage = document.getElementById("player-stage");
    if (!stage) return;
    if (!document.fullscreenElement) stage.requestFullscreen?.().catch(() => {});
    else document.exitFullscreen?.().catch(() => {});
  },

  seekToEdlTime(t, { andPlay } = {}) {
    const play = andPlay ?? this.playing;
    if (this.mode === "preview") {
      if (this._previewVideo) {
        try { this._previewVideo.currentTime = Math.max(0, t); } catch (_e) { /* not ready */ }
        if (play) this._previewVideo.play().catch(() => {});
      }
      return;
    }
    const hit = Editor.segmentAtEdlTime(t);
    if (!hit) return;
    Editor.select(hit.index);
    this._loadSegment(hit.index, hit.local, { andPlay: play });
  },

  currentEdlTime() {
    if (this.mode === "preview") return this._previewVideo?.currentTime || 0;
    const segs = Editor.segments;
    if (!segs?.length || !this.videos) return 0;
    const cum = Editor.cumulative();
    const seg = segs[this._currentIndex];
    const row = cum[this._currentIndex];
    if (!seg || !row) return 0;
    const local = this._active().currentTime - seg.start;
    return row.start + Math.max(0, local);
  },

  _onTimeUpdate() {
    const seg = Editor.segments?.[this._currentIndex];
    if (!seg) return;
    const video = this._active();
    if (video.currentTime >= seg.end - this.epsilon) this._advance();
  },

  /* ---------- draft-mode transition approximation (spec v4 §3/§4) ----------
     The transition on Editor.segments[nextIndex] is the transition INTO
     that segment. "fade": a quick to-black overlay flash independent of the
     configured duration (real fade is baked by ffmpeg; this is just a live
     approximation cue). "crossfade": both stacked <video> elements' opacity
     cross-fades over the configured duration, overriding style.css's fast
     .08s default transition just for this swap. */
  _advance() {
    const nextIndex = this._currentIndex + 1;
    const next = Editor.segments?.[nextIndex];
    if (!next) { this.pause(); return; }
    const wasPlaying = this.playing;
    const transition = next.transition || { type: "none", duration: 0.5 };
    const idle = this._idle();
    const canSwap = idle.dataset.clipId === next.clip_id && idle.readyState >= 2;

    const doSwap = () => {
      this._active().pause();
      this._active().ontimeupdate = null;
      this.activeIdx = 1 - this.activeIdx;
      this._currentIndex = nextIndex;
      const nowActive = this._active();
      try { nowActive.currentTime = next.start; } catch (_e) { /* ignore */ }
      nowActive.playbackRate = this._rate;
      nowActive.ontimeupdate = () => this._onTimeUpdate();
      if (wasPlaying) nowActive.play().catch(() => {});
      this._preloadNext(nextIndex);
      Editor.select(nextIndex);
    };

    if (transition.type === "fade") {
      this._quickFadeThenSwap(doSwap, canSwap, nextIndex, next, wasPlaying);
    } else if (transition.type === "crossfade" && canSwap) {
      this._crossfadeSwap(next, transition.duration, wasPlaying, nextIndex);
    } else if (canSwap) {
      doSwap();
    } else {
      this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying });
      Editor.select(nextIndex);
    }
  },

  _quickFadeThenSwap(doSwap, canSwap, nextIndex, next, wasPlaying) {
    const overlay = this._fadeOverlay;
    const FADE_MS = 220;
    if (!overlay) { canSwap ? doSwap() : this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying }); return; }
    overlay.style.transition = `opacity ${FADE_MS}ms linear`;
    overlay.style.opacity = "1";
    setTimeout(() => {
      if (canSwap) doSwap();
      else { this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying }); Editor.select(nextIndex); }
      overlay.style.opacity = "0";
    }, FADE_MS);
  },

  _crossfadeSwap(next, duration, wasPlaying, nextIndex) {
    const outgoing = this._active();
    const incoming = this._idle();
    const durMs = Math.max(200, Math.min(1500, (duration || 0.5) * 1000));
    try { incoming.currentTime = next.start; } catch (_e) { /* ignore */ }
    incoming.playbackRate = this._rate;
    incoming.style.transition = `opacity ${durMs}ms linear`;
    outgoing.style.transition = `opacity ${durMs}ms linear`;
    if (wasPlaying) incoming.play().catch(() => {});
    incoming.classList.add("active");
    outgoing.classList.remove("active");
    this.activeIdx = 1 - this.activeIdx;
    this._currentIndex = nextIndex;
    outgoing.ontimeupdate = null;
    incoming.ontimeupdate = () => this._onTimeUpdate();
    Editor.select(nextIndex);
    this._preloadNext(nextIndex);
    setTimeout(() => {
      outgoing.pause();
      outgoing.style.transition = "";
      incoming.style.transition = "";
    }, durMs);
  },

  _updateTimeDisplay() {
    const el = document.getElementById("pp-time");
    if (!el) return;
    if (this.sourceClipId) {
      const v = this._sourceVideo;
      el.textContent = `${fmtT(v?.currentTime || 0)} / ${fmtT(this._sourceDuration || v?.duration || 0)}`;
      return;
    }
    el.textContent = `${fmtT(this.currentEdlTime())} / ${fmtT(Editor.totalDuration())}`;
  },

  _startLoop() {
    const tick = () => {
      try {
        this._updateTimeDisplay();
        // v7 §7.1: the EDL playhead + subtitle overlay are meaningless for a
        // raw source clip (no cue timings / cut position apply) — leave both
        // exactly where they were while Source mode is active.
        if (!this.sourceClipId) {
          this._updateSubtitleOverlay();
          if (this.videos) window.EditorUI.timeline?.updatePlayhead(this.currentEdlTime());
        }
      } catch (_e) { /* never let the raf loop die */ }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  },
};

window.EditorUI.player = Player;
