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
   fully non-interactive (pointer-events off) — untouched by any of this.

   ---------- boundary-freeze fix: "playback freezes crossing clip/segment
   boundaries" ----------
   Root causes (all in this file):
     1. `_loadSegment`/`_preloadNext`/`enterSourceMode` loaded a new clip src
        and armed the seek+play via a single-property `video.onloadedmetadata
        = doSeek`. That silently never fires (so `doSeek` — the only thing
        that seeks and calls play() — never runs, and playback just sits
        there) whenever metadata is already available by attach time, or the
        browser only emits loadeddata/canplay for that load. Fixed by
        `_whenSeekable(video, cb)`: fires immediately if already seekable
        (readyState >= 1), otherwise listens for whichever of
        loadedmetadata/loadeddata/canplay comes first and tears the rest
        down — so the seek+play is guaranteed to run exactly once.
     2. `_advance()`'s `canSwap` check could be falsely false for one frame
        right at a junction (a <video> mid-seek transiently reports
        readyState < 2 even with the right clip already loaded), forcing
        the fragile full-reload path when the idle buffer was basically
        fine. Fixed with `_advanceWithGrace`: when the idle buffer's
        clip_id already matches, give it a short (~200ms, rAF-polled)
        grace window before falling back to `_loadSegment`.
     3. During the async fade/crossfade transition window, the still-active
        video kept playing past its segment end, so `ontimeupdate` kept
        re-firing `_advance()` on every frame, stacking duplicate
        swap/reload decisions. Fixed with the `_advancing` re-entrancy
        guard (set the instant a junction decision starts, cleared only
        once it has actually landed via `_doAdvance`/`doSwap`/reload).
     4. `<video>.play()`'s promise can reject (autoplay policy, "interrupted
        by a new load request", …); the old code swallowed every rejection
        with `.catch(() => {})`, leaving `this.playing` true with nothing
        left to resume the element. Fixed with `_playWithRetry`: a bounded
        (4 attempts, short backoff) retry, abandoned early if the user
        paused in the meantime.
     5. Belt-and-suspenders: `_checkStallWatchdog` (run every rAF tick from
        `_startLoop`) detects the case none of the above anticipated —
        `this.playing` true but the active video's currentTime frozen on
        the same segment for >400ms — and self-heals via `_recoverStall`
        (reload the current segment from its start and resume).
   Transition swap paths (fade/crossfade/dissolve approximation) are
   unchanged in shape, only hardened the same way (loader + retry). Segment/
   EDL production is untouched. See this task's final report for the live
   playback verification (Patrimonest RAW, 6 clips/14 segments). */

window.EditorUI = window.EditorUI || {};

const Player = {
  videos: null,
  activeIdx: 0,
  playing: false,
  epsilon: 0.05,
  _currentIndex: 0,
  _rafId: null,
  _rate: 1,

  // ---- boundary-freeze hardening (see module docstring addendum below) ----
  _advancing: false,   // re-entrancy guard: true from the moment _advance() starts
                        // deciding a junction until the swap/reload actually lands,
                        // so repeated ontimeupdate firings during a fade/grace delay
                        // can't stack duplicate advances on top of each other.
  _wdLastT: undefined,  // stall watchdog: last-seen currentTime of the active video
  _wdIndex: undefined,  // stall watchdog: segment index that currentTime was sampled at
  _wdSince: 0,          // stall watchdog: performance.now() when _wdLastT last changed

  // ---- boundary-freeze hardening v2: intent-tracked play state ----
  // `this.playing` is meant to always mirror "is the user's play/pause button
  // in the pause state", but a handful of code paths (seekToEdlTime after a
  // setMode()-triggered pause, an async swap that never got a chance to
  // reconcile it) could historically leave it false while a swap/seek was
  // still issuing a real play() underneath. Since `_checkStallWatchdog` only
  // ever ran when `this.playing` was true, ANY such desync meant a truly
  // stuck-after-boundary video would never be recovered — the watchdog guard
  // gap. `_intendPlaying` is set/cleared at the exact same moments as a
  // deliberate user play/pause action (play(), pause(), the reconciled
  // seek/segments-changed/video-error paths) and is deliberately allowed to
  // diverge from `this.playing` if something else desyncs it — the watchdog
  // below checks BOTH, so an intended-playing session that silently dropped
  // `this.playing` still gets recovered.
  _intendPlaying: false,
  _lastEdlTime: 0,      // last successfully-computed currentEdlTime(); see currentEdlTime()'s
                          // fallback and onSegmentsChanged's EDL-mutation continuity logic below.
  _lastEdlIndex: undefined,  // _currentIndex the last time currentEdlTime() actually recomputed
  _lastEdlVt: undefined,     // active <video>.currentTime the last time currentEdlTime() actually
                              // recomputed — see currentEdlTime()'s frozen-video guard below.
  _recoverIdx: undefined,    // segment index the last _recoverStall() escalation targeted
  _recoverAttempts: 0,       // consecutive _recoverStall() calls at that same index with no
                              // real progress — escalates from a plain seek+play to a full
                              // src reload once plain retries prove the element is wedged

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
      v.onerror = () => this._onVideoError(v);
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
      const start = () => { try { v.currentTime = 0; } catch (_e) { /* not ready */ } this._playWithRetry(v); };
      if (v.dataset.clipId === clipId) start();
      else {
        v.dataset.clipId = clipId;
        v.src = `/api/projects/${Editor.pid}/media/preview/${clipId}`;
        this._whenSeekable(v, start);
      }
    }
    if (this._sourceChip) {
      this._sourceChip.hidden = false;
      const nameEl = document.getElementById("pp-source-name");
      if (nameEl) nameEl.textContent = clip.filename || clipId;
    }
    document.getElementById("timeline-content")?.classList.add("tl-source-dim");

    this.playing = true;
    this._intendPlaying = true;
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
    this._intendPlaying = false;

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
        // v_FIX §9: any EDL edit while playing (add/split/delete/reorder a
        // segment, etc.) lands here mid-playback, synchronously from
        // state.js's _notify() on commit. Thread the REAL desired play state
        // through (capture `this.playing` before reloading, reconcile
        // afterwards) instead of unconditionally pausing the underlying
        // <video> while leaving `this.playing`/the button lying about it.
        //
        // ---- EDL-mutation freeze hardening (v3) ----
        // Two further hazards showed up specifically on split/delete (NOT
        // plain boundary-crossing, which was already fixed): see
        // `_reloadAfterEdlEdit` for the continuity fix (don't blindly jump to
        // `Editor.selected` — that's the index the EDIT happened to leave
        // selected, e.g. a split's second half or whatever a delete shifted
        // focus to, not where the playhead actually was) and the hard index
        // clamp (an old `Editor.segments[i].start` on an out-of-range/NaN
        // `Editor.selected` — very possible right after a delete — used to
        // THROW synchronously out of this function, aborting the reload with
        // the video frozen and no recovery). Wrapped in try/catch so NO edit
        // shape can ever throw out of here; on any unanticipated failure we
        // force a clean, truthful stop rather than a frozen "still playing".
        const wasPlaying = this.playing;
        const playheadT = this.currentEdlTime();
        try {
          this._reloadAfterEdlEdit(wasPlaying, playheadT);
        } catch (err) {
          console.error("[player] onSegmentsChanged failed to reload after an EDL edit — stopping cleanly instead of freezing", err);
          this._advancing = false;
          this._reconcilePlayState(false);
        }
      }
    }
    this.reloadSubtitles();
    this._autoSelectMode();
  },

  // Resolve where playback should land after an EDL mutation (split/delete/
  // reorder/add) and reload there. Split out of onSegmentsChanged so the
  // whole decision is covered by one try/catch without obscuring its shape.
  //   - While PLAYING: continuity wins. Re-derive the segment from the
  //     PRE-edit playhead time (`playheadT`, captured before this function
  //     runs) via Editor.segmentAtEdlTime() — not from `Editor.selected`,
  //     which reflects the EDIT's own selection side-effect, not the
  //     playhead. If the exact time no longer falls inside any segment (its
  //     own segment got deleted, or it now exceeds the shortened timeline),
  //     retry clamped to the new total duration; if that still comes up
  //     empty, land on the nearest valid segment (the last one) rather than
  //     ever falling through to `Editor.selected`.
  //   - While PAUSED: honor `Editor.selected`, hard-clamped into
  //     [0, segments.length - 1] (it can be -1/undefined/NaN right after a
  //     delete) so this can never index into `undefined`.
  _reloadAfterEdlEdit(wasPlaying, playheadT) {
    const segs = Editor.segments;
    let index;
    let localTime;
    if (wasPlaying) {
      let hit = Editor.segmentAtEdlTime?.(playheadT);
      if (!hit) {
        const total = Editor.totalDuration?.() ?? 0;
        const clampedT = Math.max(0, Math.min(playheadT, Math.max(0, total - this.epsilon)));
        hit = Editor.segmentAtEdlTime?.(clampedT);
      }
      if (hit && segs[hit.index]) {
        index = hit.index;
        localTime = hit.local;
      } else {
        index = segs.length - 1; // last-resort nearest segment — never Editor.selected while playing
      }
    } else {
      const sel = Number.isInteger(Editor.selected) ? Editor.selected : 0;
      index = sel;
    }
    // Belt-and-suspenders hard clamp regardless of which branch set `index`
    // — this is the line that used to throw (`Editor.segments[i]` on an
    // out-of-range `i`) and freeze the video with no way back.
    index = Math.min(Math.max(index, 0), segs.length - 1);
    const seg = segs[index];
    if (!seg) { this._reconcilePlayState(false); return; } // defensive only; segs is non-empty here
    if (localTime === undefined) localTime = seg.start;
    Editor.select(index);
    this._loadSegment(index, localTime, { andPlay: wasPlaying });
    this._reconcilePlayState(wasPlaying);
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

  /* ---------- boundary-freeze fix, part 1: a reliable "seek once ready"
     primitive ----------
     The old code did `video.onloadedmetadata = doSeek` and nothing else.
     That single-property handler silently never fires (so `doSeek` — which
     is what actually calls seek + play() — never runs, and playback just
     freezes) whenever: the metadata was already loaded by the time we
     attach the handler (a `readyState >= 1` video does not re-fire
     `loadedmetadata`), or the browser only surfaces `loadeddata`/`canplay`
     for this particular load path. This helper covers both: if the video
     is already seekable it runs `cb` immediately; otherwise it listens for
     whichever of loadedmetadata/loadeddata/canplay fires first, and tears
     down the others so nothing leaks or double-fires. */
  _whenSeekable(video, cb) {
    if (video.readyState >= 1) { cb(); return; }
    const events = ["loadedmetadata", "loadeddata", "canplay"];
    const handler = () => {
      events.forEach((ev) => video.removeEventListener(ev, handler));
      cb();
    };
    events.forEach((ev) => video.addEventListener(ev, handler, { once: true }));
  },

  /* ---------- boundary-freeze fix, part 2: bounded play() retry ----------
     `<video>.play()` returns a promise that can reject (autoplay policy,
     "interrupted by a new load request", etc.) — silently swallowing that
     rejection (the old `.catch(() => {})` everywhere) leaves `this.playing`
     true while the element itself is actually paused, with nothing left to
     ever resume it. Retry a few times on a short backoff; give up quietly
     if the user paused in the meantime (don't fight their intent).

     ---------- real-WKWebView hardening: verify, don't just hope ----------
     The rejection-only retry above is not enough: in real WKWebView (NOT the
     mock <video> harness used to "verify" earlier fixes), `play()` can
     resolve — or simply never reject — while the element is still, in
     fact, paused (most often right after a same-element seek at a segment
     boundary, before the seek has actually settled). Nothing about a
     resolved promise proves playback actually resumed. So every attempt is
     now followed by an explicit `.paused` check a beat later: if it's still
     paused and we still intend to be playing, that counts as a failed
     attempt exactly like a rejection would, and retries the same way. Once
     attempts are exhausted and the element STILL won't stick, this escalates
     past "try play() again" to `_escalateStuckPlayback`, which forces a real
     segment reload (a fresh src/seek/play sequence resets WebKit's decode
     pipeline far more reliably than hammering play() on an already-wedged
     element). */
  _playWithRetry(video, attempt = 0) {
    if (!video) return;
    const result = video.play();
    if (result && typeof result.catch === "function") result.catch(() => { /* verified below regardless */ });
    setTimeout(() => {
      const wantPlaying = this.playing || this._intendPlaying;
      if (!wantPlaying) return; // user paused meanwhile — don't fight their intent
      if (!video.paused) return; // it actually took (whether or not the promise ever settled)
      if (attempt >= 4) { this._escalateStuckPlayback(video); return; }
      this._playWithRetry(video, attempt + 1);
    }, 120 + attempt * 100);
  },

  // Reached only once `_playWithRetry` has proven, by direct observation of
  // `.paused`, that play() will not stick on this element no matter how many
  // times it's reissued in place. Only meaningful for the active draft video
  // (source/preview modes don't advance through segments); a bare
  // fire-and-forget reload here mirrors what `_recoverStall`'s escalation
  // does for the exact same underlying symptom.
  _escalateStuckPlayback(video) {
    if (this.mode !== "draft" || this.sourceClipId || video !== this._active()) return;
    if (!(this.playing || this._intendPlaying)) return;
    console.warn("[player] play() would not stick after retries on the active video — forcing a segment reload", video.dataset.clipId);
    this._forceReloadSegment(this._currentIndex);
  },

  // Escalation beyond a plain seek+play: force a genuine <video>.src
  // reassignment even when `dataset.clipId` already matches the target
  // segment's clip (the normal `_loadSegment` short-circuits straight to a
  // seek in that case). A bare currentTime seek + play() can fail to clear
  // whatever wedged WebKit's internal decode/seek state at a boundary; a
  // fresh src load resets that pipeline outright.
  _forceReloadSegment(index) {
    const seg = Editor.segments?.[index];
    if (!seg) return;
    const video = this._active();
    this._currentIndex = index;
    video.dataset.clipId = ""; // force the normal same-clip fast path below to reload for real
    video.src = `/api/projects/${Editor.pid}/media/preview/${seg.clip_id}`;
    video.dataset.clipId = seg.clip_id;
    this._whenSeekable(video, () => {
      try { video.currentTime = seg.start; } catch (_e) { /* metadata not ready */ }
      video.playbackRate = this._rate;
      this._playWithRetry(video);
    });
    this._preloadNext(index);
  },

  _loadSegment(index, atClipTime, { andPlay }) {
    const seg = Editor.segments?.[index];
    if (!seg) return;
    this._currentIndex = index;
    const video = this._active();
    const target = atClipTime ?? seg.start;
    const doSeek = () => {
      try { video.currentTime = target; } catch (_e) { /* metadata not ready */ }
      video.playbackRate = this._rate;
      if (andPlay) this._playWithRetry(video);
    };
    if (video.dataset.clipId === seg.clip_id) {
      doSeek();
    } else {
      video.dataset.clipId = seg.clip_id;
      video.src = `/api/projects/${Editor.pid}/media/preview/${seg.clip_id}`;
      this._whenSeekable(video, doSeek);
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
    this._whenSeekable(idle, () => { try { idle.currentTime = next.start; } catch (_e) { /* ignore */ } });
  },

  /* ---------- v_FIX §15: recover from a missing/corrupt clip file ----------
     Previously video.onerror only console.error'd -- the <video> element was
     left in its errored state forever: this.playing stayed true (if it was),
     the play/pause button kept showing "pause", and the raf loop's
     _onTimeUpdate() never fires again for a video that can't decode, so
     _advance() never runs either. Draft playback just silently stalled with
     no user-visible sign anything was wrong. Now: only react when the error
     is on the currently ACTIVE video in Draft/non-Source mode (a failed
     background preload on the idle element just means "can't cross-fade
     smoothly" -- not a playback stall -- so that path is left to degrade on
     its own via _advance()'s existing canSwap fallback). Reset the play
     state + button, toast the failure, and skip forward to the next segment
     (if any) preserving whatever the real play intent was, so a single bad
     clip doesn't hang the whole cut. */
  _onVideoError(video) {
    console.error("player video error", video.dataset.clipId, video.error);
    if (this.mode !== "draft" || this.sourceClipId || video !== this._active()) return;
    const wasPlaying = this.playing;
    const clip = Editor?.clip?.(video.dataset.clipId);
    const name = clip?.filename || video.dataset.clipId || "this clip";
    try { showToast(`Couldn't play "${name}" — skipping to the next segment.`); } catch (_e) { /* toast is best-effort */ }
    const nextIndex = this._currentIndex + 1;
    const next = Editor.segments?.[nextIndex];
    if (next) {
      // Keep the real play intent alive across the skip -- _loadSegment's
      // `andPlay` re-issues play() on the next clip if we were playing, so
      // this.playing/the button stay truthful instead of freezing on
      // whatever they happened to say when the error fired.
      Editor.select(nextIndex);
      this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying });
      this._reconcilePlayState(wasPlaying);
    } else {
      // Last (or only) segment errored -- nothing to skip forward to; stop
      // cleanly and make sure the UI actually says "stopped" instead of
      // hanging forever under a stale "playing" button.
      this._reconcilePlayState(false);
    }
  },

  play() {
    // Belt-and-suspenders (see `_intendPlaying` above): recorded up front so
    // the watchdog can trust "the user wants to be playing" even if
    // `this.playing` itself later gets out of sync somehow.
    this._intendPlaying = true;
    if (this.sourceClipId) {
      // v7 §7.1: "Transport keys work on the source clip."
      this.playing = true;
      this._playWithRetry(this._sourceVideo);
    } else if (this.mode === "preview") {
      if (!this._previewVideo?.src) { this._intendPlaying = false; return; }
      this.playing = true;
      this._playWithRetry(this._previewVideo);
    } else {
      if (!Editor.segments?.length || !this.videos) { this._intendPlaying = false; return; }
      this.playing = true;
      // Boundary stall watchdog (see _startLoop): a fresh baseline every time
      // we (re)start playback, so a long preceding pause never reads as an
      // instant "stuck" the moment we resume.
      this._wdLastT = undefined;
      this._wdIndex = undefined;
      this._wdSince = performance.now();
      this._playWithRetry(this._active());
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
    this._intendPlaying = false;
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

  // ---- boundary-freeze hardening v2: play-state reconciliation ----
  // `setMode()` calls `this.pause({auto:false})` (forcing `this.playing`/
  // `_intendPlaying` false) BEFORE calling seekToEdlTime(t, {andPlay:
  // wasPlaying}) to resume on the new mode's element. seekToEdlTime used to
  // only ever issue the underlying `_playWithRetry()` call without ever
  // restoring `this.playing` — so after any mode switch made mid-playback,
  // the video could be genuinely playing again while `this.playing` (and
  // therefore the stall watchdog's guard, the play/pause button, and the
  // subtitle-overlay interactivity toggle) all still said "paused". Any
  // watchdog fallback keyed only on `this.playing` would then never engage
  // for a real freeze that happened afterwards. Centralize the reconciliation
  // here so every seek leaves `this.playing`/`_intendPlaying`/the button in
  // the truthful state that matches what was actually just requested.
  _reconcilePlayState(play) {
    this.playing = play;
    this._intendPlaying = play;
    const btn = document.getElementById("pp-playpause");
    if (btn) { btn.innerHTML = `<i data-lucide="${play ? "pause" : "play"}"></i>`; refreshIcons(); }
    this._updateSubtitleInteractivity();
  },

  seekToEdlTime(t, { andPlay } = {}) {
    const play = andPlay ?? this.playing;
    if (this.mode === "preview") {
      if (this._previewVideo) {
        try { this._previewVideo.currentTime = Math.max(0, t); } catch (_e) { /* not ready */ }
        if (play) this._playWithRetry(this._previewVideo);
      }
      this._reconcilePlayState(play);
      return;
    }
    const hit = Editor.segmentAtEdlTime(t);
    if (!hit) return;
    Editor.select(hit.index);
    this._loadSegment(hit.index, hit.local, { andPlay: play });
    this._reconcilePlayState(play);
  },

  currentEdlTime() {
    if (this.mode === "preview") return this._previewVideo?.currentTime || 0;
    const segs = Editor.segments;
    // EDL-mutation hardening: `_currentIndex` can transiently point at a
    // segment slot that no longer means what it used to (a split/delete/
    // reorder just landed, shifting the array, before onSegmentsChanged has
    // had a chance to reload) -- computing against a stale/undefined mapping
    // used to just return 0 (a visible jump to the start), and worse, was
    // the ONLY signal onSegmentsChanged had for "where was the playhead" —
    // so a fully-undefined mapping there would erase continuity entirely.
    // Cache the last successfully-computed time and fall back to it instead.
    if (!segs?.length || !this.videos) return this._lastEdlTime || 0;
    const cum = Editor.cumulative();
    const seg = segs[this._currentIndex];
    const row = cum[this._currentIndex];
    if (!seg || !row) return this._lastEdlTime || 0;
    const vt = this._active().currentTime;
    // Real-WKWebView hardening: if the segment index AND the active
    // element's own currentTime are byte-identical to the last time this
    // actually recomputed, the video has made NO real progress since — the
    // only truthful thing to do is hand back the exact same cached value,
    // never a freshly-recomputed one. This is what guarantees currentEdlTime
    // (and therefore anything sampling it, like a watchdog) can never appear
    // to advance while the real, active <video> element is frozen — no
    // matter what `Editor.cumulative()`/segment lookups do underneath.
    if (this._lastEdlIndex === this._currentIndex && this._lastEdlVt === vt) {
      return this._lastEdlTime || 0;
    }
    const local = vt - seg.start;
    const t = row.start + Math.max(0, local);
    this._lastEdlIndex = this._currentIndex;
    this._lastEdlVt = vt;
    this._lastEdlTime = t;
    return t;
  },

  _onTimeUpdate() {
    const seg = Editor.segments?.[this._currentIndex];
    if (!seg) return;
    const video = this._active();
    if (video.currentTime >= seg.end - this.epsilon) this._advance();
  },

  /* ---------- draft-mode transition approximation (spec v4 §3/§4, v7.5) ----
     The transition on Editor.segments[nextIndex] is the transition INTO
     that segment. "fade": a quick to-black overlay flash independent of the
     configured duration (real fade is baked by ffmpeg; this is just a live
     approximation cue). "crossfade" AND every other named xfade catalog type
     (wipeleft, circleopen, slideup, pixelize, dissolve, ... ~58 entries,
     spec v7.5 §7.5): both stacked <video> elements' opacity cross-fades over
     the configured duration, overriding style.css's fast .08s default
     transition just for this swap. The editor can't cheaply reproduce each
     exact GPU xfade shape, so every non-fade named type gets this generic
     dissolve approximation in draft mode — the exact styled transition (wipe/
     circle/pixelize/etc.) is only ever baked by ffmpeg at render/export time
     (see pipeline/render.py). Only "none" is a real hard cut. If the idle
     buffer isn't ready for a cross-fade, fall back to the quick to-black
     flash rather than a bare cut, so the user always SEES something happen
     at the junction.

     ---------- boundary-freeze fix, part 3: re-entrancy + readyState grace
     ----------
     Two extra hazards used to compound the freeze:
     (a) During the FADE_MS/durMs async window (_quickFadeThenSwap's
         setTimeout, or the tail of _crossfadeSwap), the video that is still
         "active" keeps playing and its currentTime keeps climbing past
         seg.end, so `ontimeupdate` -> `_onTimeUpdate` -> `_advance()` kept
         firing AGAIN on every frame of that window, stacking duplicate
         swap/reload decisions on top of each other. `_advancing` is now a
         re-entrancy guard: set the instant a junction decision starts, only
         cleared once that decision has actually landed (a real doSwap, a
         committed crossfade, or a fallback reload) — any `_advance()` call
         in between is a no-op.
     (b) A <video> mid-seek transiently reports `readyState` below 2 even
         though the idle buffer already has the right clip loaded — so
         `canSwap` could be falsely false for exactly one frame right at a
         junction, forcing the fragile full-reload path when a few more
         milliseconds would have made the fast swap available. `_advance`
         now gives that case (dataset.clipId already matches) a short grace
         window (`_advanceWithGrace`, ~200ms via rAF polling) to clear
         before falling back to `_loadSegment`. */
  _advance() {
    if (this._advancing) return; // (a) a decision for this junction is already in flight
    this._advancing = true;
    try {
      const nextIndex = this._currentIndex + 1;
      const next = Editor.segments?.[nextIndex];
      if (!next) { this._advancing = false; this.pause(); return; }
      const wasPlaying = this.playing;
      const transition = next.transition || { type: "none", duration: 0.5 };
      const idle = this._idle();
      const idleMatches = idle.dataset.clipId === next.clip_id;
      const canSwapNow = idleMatches && idle.readyState >= 2;
      const hasTransition = transition.type && transition.type !== "none";

      if (idleMatches && !canSwapNow) {
        // (b) right clip, just transiently not "ready" — give it a beat.
        this._advanceWithGrace(nextIndex, next, transition, hasTransition, wasPlaying);
        return;
      }
      this._doAdvance(nextIndex, next, transition, hasTransition, canSwapNow, wasPlaying);
    } catch (err) {
      // Boundary-freeze hardening v2 (hypothesis 2): a synchronous throw
      // anywhere in the decision above used to leave `_advancing` stuck
      // `true` forever with no try/finally — every future `_advance()` call
      // would then silently no-op on the reentrancy guard at the top of this
      // function, which is a textbook permanent-stuck-at-a-boundary bug.
      // Force the guard clear and let the stall watchdog's own recovery path
      // (which is itself now bulletproofed, see `_recoverStall`) take it from
      // here instead of leaving the junction wedged.
      console.error("[player] _advance() failed, forcing recovery", err);
      this._advancing = false;
      this._recoverStall();
    }
  },

  _advanceWithGrace(nextIndex, next, transition, hasTransition, wasPlaying) {
    const GRACE_MS = 200;
    const deadline = performance.now() + GRACE_MS;
    const check = () => {
      try {
        // Boundary-freeze hardening v2 (hypothesis 5's real dead-end): the
        // EDL can be edited (add/split/delete/reorder a segment) WHILE this
        // ~200ms grace poll is in flight. `nextIndex`/`next` are captured
        // from before the edit, so blindly committing to `_doAdvance` with
        // them once segments have shifted would desync `_currentIndex` from
        // the real (post-edit) array — and an out-of-range `_currentIndex`
        // used to make `_onTimeUpdate`/`_recoverStall` no-op forever (see
        // `_recoverStall`'s hardening below). Detect the mismatch and, instead
        // of committing stale state, drop this in-flight decision and
        // re-derive a fresh one from the CURRENT segments/`_currentIndex`.
        const segs = Editor.segments;
        if (!segs || nextIndex >= segs.length || segs[nextIndex]?.clip_id !== next.clip_id) {
          this._advancing = false;
          this._advance();
          return;
        }
        const idle = this._idle();
        if (idle.dataset.clipId !== next.clip_id) {
          // Changed out from under us (shouldn't normally happen) — reload reliably.
          this._doAdvance(nextIndex, next, transition, hasTransition, false, wasPlaying);
          return;
        }
        if (idle.readyState >= 2) {
          this._doAdvance(nextIndex, next, transition, hasTransition, true, wasPlaying);
          return;
        }
        if (performance.now() >= deadline) {
          this._doAdvance(nextIndex, next, transition, hasTransition, false, wasPlaying);
          return;
        }
        requestAnimationFrame(check);
      } catch (err) {
        console.error("[player] grace-window check failed, forcing recovery", err);
        this._advancing = false;
        this._recoverStall();
      }
    };
    requestAnimationFrame(check);
  },

  _doAdvance(nextIndex, next, transition, hasTransition, canSwap, wasPlaying) {
    // Every branch below guarantees `_advancing` clears via try/finally, not
    // just a plain assignment on the last line — so a throw partway through
    // (e.g. a segment/clip lookup that no longer resolves) can never leave
    // the re-entrancy guard stuck `true` forever (hypothesis 2).
    const doSwap = () => {
      try {
        const wasActive = this._active();
        wasActive.pause();
        wasActive.ontimeupdate = null;
        this.activeIdx = 1 - this.activeIdx;
        this._currentIndex = nextIndex;
        const nowActive = this._active();
        // Real-WKWebView hardening: this was the actual root cause of "the
        // hidden buffer plays [is the one that resumes] while the visible
        // one stays frozen" — `activeIdx` flipped LOGICALLY but the CSS
        // `.active` class (the only thing that controls which stacked
        // <video> is actually visible, see .player-video.active { opacity:
        // 1 } in style.css) never moved with it. `_crossfadeSwap` always
        // toggled this correctly; this hard-cut path never did. Keep the
        // visible element and the one we're about to seek/play in lockstep.
        wasActive.classList.remove("active");
        nowActive.classList.add("active");
        try { nowActive.currentTime = next.start; } catch (_e) { /* ignore */ }
        nowActive.playbackRate = this._rate;
        nowActive.ontimeupdate = () => this._onTimeUpdate();
        if (wasPlaying) this._playWithRetry(nowActive);
        this._preloadNext(nextIndex);
        Editor.select(nextIndex);
      } finally {
        this._advancing = false; // junction decision has landed
      }
    };

    try {
      if (transition.type === "fade") {
        this._quickFadeThenSwap(doSwap, canSwap, nextIndex, next, wasPlaying);
      } else if (hasTransition && canSwap) {
        // "crossfade" and every other named catalog type (circleopen, wipeleft,
        // slideup, pixelize, ...) all get the same generic dissolve preview.
        try {
          this._crossfadeSwap(next, transition.duration, wasPlaying, nextIndex);
        } finally {
          this._advancing = false; // the swap itself (ontimeupdate/activeIdx) is synchronous
        }
      } else if (hasTransition && !canSwap) {
        // Idle buffer not ready for a cross-fade — still show a visible
        // transition cue rather than silently hard-cutting.
        this._quickFadeThenSwap(doSwap, canSwap, nextIndex, next, wasPlaying);
      } else if (canSwap) {
        doSwap();
      } else {
        try {
          this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying });
          Editor.select(nextIndex);
        } finally {
          this._advancing = false;
        }
      }
    } catch (err) {
      console.error("[player] _doAdvance failed, forcing recovery", err);
      this._advancing = false;
      this._recoverStall();
    }
  },

  _quickFadeThenSwap(doSwap, canSwap, nextIndex, next, wasPlaying) {
    const overlay = this._fadeOverlay;
    const FADE_MS = 220;
    const reload = () => {
      try {
        this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying });
        Editor.select(nextIndex);
      } finally {
        this._advancing = false;
      }
    };
    if (!overlay) {
      if (canSwap) doSwap();
      else reload();
      return;
    }
    overlay.style.transition = `opacity ${FADE_MS}ms linear`;
    overlay.style.opacity = "1";
    setTimeout(() => {
      try {
        if (canSwap) doSwap();
        else reload();
      } catch (err) {
        console.error("[player] fade-swap timeout failed, forcing recovery", err);
        this._advancing = false;
        this._recoverStall();
      } finally {
        overlay.style.opacity = "0";
      }
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
    if (wasPlaying) this._playWithRetry(incoming);
    incoming.classList.add("active");
    outgoing.classList.remove("active");
    this.activeIdx = 1 - this.activeIdx;
    this._currentIndex = nextIndex;
    outgoing.ontimeupdate = null;
    incoming.ontimeupdate = () => this._onTimeUpdate();
    Editor.select(nextIndex);
    this._preloadNext(nextIndex);
    setTimeout(() => {
      try {
        outgoing.pause();
      } finally {
        outgoing.style.transition = "";
        incoming.style.transition = "";
      }
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

  /* ---------- boundary-freeze fix, part 4: stall watchdog ----------
     Belt-and-suspenders on top of the loader/re-entrancy/retry fixes above:
     if `this.playing` is true (Draft, not Source mode) but the active
     video's currentTime hasn't budged — AND we're still on the same
     segment index — for longer than STALL_MS, something got wedged (a
     loader edge case we didn't anticipate, a play() that silently never
     resolved, etc.). Force a clean recovery: reload the current segment
     from its start and resume, instead of leaving the user staring at a
     frozen frame forever. Runs every rAF tick from _startLoop; cheap. */
  _checkStallWatchdog() {
    const STALL_MS = 400;
    // Hypothesis-1 hardening: check `_intendPlaying` in addition to
    // `this.playing`. If some future path (or one we haven't found yet)
    // leaves `this.playing` false while the user still intended to be
    // playing, the watchdog must still be able to see the freeze and recover
    // — it must never depend SOLELY on `this.playing` bookkeeping being
    // perfectly consistent.
    if (!(this.playing || this._intendPlaying) || this.mode !== "draft" || this.sourceClipId || !this.videos) {
      this._wdLastT = undefined;
      return;
    }
    const video = this._active();
    const t = video ? video.currentTime : 0;
    if (this._wdLastT === undefined || t !== this._wdLastT || this._wdIndex !== this._currentIndex) {
      this._wdLastT = t;
      this._wdIndex = this._currentIndex;
      this._wdSince = performance.now();
      this._recoverAttempts = 0; // real progress happened — the element isn't wedged (any more)
      return;
    }
    if (performance.now() - this._wdSince > STALL_MS) {
      this._recoverStall();
      this._wdSince = performance.now(); // fresh grace window before we'd retrigger
    }
  },

  // Hardened so this can NEVER silently no-op: the old `if (!seg) return;`
  // meant that a `_currentIndex` desynced from the live `Editor.segments`
  // array (e.g. a stale index from an in-flight grace-window decision racing
  // a mid-playback EDL edit) turned every future watchdog tick into a dead
  // no-op forever — the watchdog would keep firing every ~400ms, "recover",
  // and do nothing, which is indistinguishable from permanently stuck. Now:
  // clamp to the nearest valid segment and recover into THAT, or, if there
  // are truly no segments left to play, come to a clean, truthfully-paused
  // stop instead of leaving the UI/this.playing lying about still playing.
  _recoverStall() {
    console.warn("[player] boundary stall watchdog: forcing recovery at segment", this._currentIndex);
    this._advancing = false; // whatever junction decision was stuck, abandon it
    const segs = Editor.segments;
    if (!segs || !segs.length) {
      this._reconcilePlayState(false);
      return;
    }
    const idx = Math.min(Math.max(this._currentIndex, 0), segs.length - 1);
    // Real-WKWebView hardening: track consecutive recoveries AT THE SAME
    // INDEX with no real progress in between (`_checkStallWatchdog` zeroes
    // this out the instant currentTime actually moves). A plain seek+play
    // (`_loadSegment`) is the cheap first response, but if it's already
    // failed to unstick this exact element more than a couple of times in a
    // row, keep reissuing it is just repeating the thing that already
    // didn't work — escalate to a full src reload instead, same as
    // `_escalateStuckPlayback` does for the play()-retry path.
    if (this._recoverIdx === idx) this._recoverAttempts = (this._recoverAttempts || 0) + 1;
    else { this._recoverIdx = idx; this._recoverAttempts = 1; }
    this._currentIndex = idx;
    const seg = segs[idx];
    if (this._recoverAttempts > 2) {
      this._forceReloadSegment(idx);
    } else {
      this._loadSegment(idx, seg.start, { andPlay: true });
    }
    // Defensive re-assert (hypothesis-3 hardening): _loadSegment reuses
    // whichever element is currently "active" and never touches its
    // ontimeupdate, which is correct because doSwap()/_crossfadeSwap() are
    // the only paths that ever null it out and both always restore it on
    // whatever becomes active — but this is the one place recovering from an
    // truly-unanticipated wedge, so make doubly sure the active element can
    // still report future boundaries.
    const active = this._active();
    if (active && !active.ontimeupdate) active.ontimeupdate = () => this._onTimeUpdate();
    this._reconcilePlayState(true);
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
          // Boundary-freeze hardening v2 (hypothesis 3 belt-and-suspenders):
          // by construction `doSwap()`/`_crossfadeSwap()` always restore
          // `ontimeupdate` on whichever element becomes active, so this
          // should never actually be needed — but re-asserting it here every
          // tick is nearly free and guarantees the active element can never
          // be left with a detached handler by some path we haven't
          // anticipated, which would otherwise silently stop `_advance()`
          // from ever being called again.
          if (this.mode === "draft" && this.videos) {
            const active = this._active();
            if (active && !active.ontimeupdate) active.ontimeupdate = () => this._onTimeUpdate();
          }
        }
        this._checkStallWatchdog();
      } catch (_e) { /* never let the raf loop die */ }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  },
};

window.EditorUI.player = Player;
