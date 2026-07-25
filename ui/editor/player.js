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
   Regression-checked by code walkthrough — see this task's final report. */

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

    this.onSegmentsChanged();
    if (!this._rafId) this._startLoop();
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
  },
  _currentSubtitleText(t) {
    if (!this._subtitleCfg?.enabled || !this._cues?.length) return "";
    const cue = this._cues.find((c) => t >= c.edl_t_start && t < c.edl_t_end);
    return cue?.text || "";
  },
  _updateSubtitleOverlay() {
    if (!this._subtitleEl) return;
    const text = this._currentSubtitleText(this.currentEdlTime());
    if (this._subtitleEl.textContent !== text) this._subtitleEl.textContent = text;
    this._subtitleEl.style.display = text ? "block" : "none";
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
    if (this.mode === "preview") {
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
  },
  // `auto` guards against the internal pause() that setMode() itself does
  // right before flipping this.mode — re-running _autoSelectMode() there
  // would race the manual switch it's still in the middle of (e.g. flip
  // straight back to Preview a beat after the user manually chose Draft).
  // Real user-driven pauses (play/pause button, K, video ending) all go
  // through the default auto=true path.
  pause({ auto = true } = {}) {
    this.playing = false;
    if (this.mode === "preview") this._previewVideo?.pause();
    else this._active()?.pause();
    const btn = document.getElementById("pp-playpause");
    if (btn) { btn.innerHTML = '<i data-lucide="play"></i>'; refreshIcons(); }
    // v5.14: "auto-select ... when paused" — the instant it's safe (no
    // active playback to interrupt), let a fresh preview settle in on its
    // own instead of leaving the user staring at a glowing toggle forever.
    if (auto) this._autoSelectMode();
  },
  togglePlay() { this.playing ? this.pause() : this.play(); },

  /* ---------- J/K/L shuttle (spec v4 §4) ---------- */
  jump(deltaSec) {
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
      const v = this.mode === "preview" ? this._previewVideo : this._active();
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
    el.textContent = `${fmtT(this.currentEdlTime())} / ${fmtT(Editor.totalDuration())}`;
  },

  _startLoop() {
    const tick = () => {
      try {
        this._updateTimeDisplay();
        this._updateSubtitleOverlay();
        if (this.videos) window.EditorUI.timeline?.updatePlayhead(this.currentEdlTime());
      } catch (_e) { /* never let the raf loop die */ }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  },
};

window.EditorUI.player = Player;
