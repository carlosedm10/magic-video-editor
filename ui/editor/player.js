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
   approximation, see the final report). */

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
    btn.textContent = this.mode === "preview" ? "🎬 Preview" : "◎ Draft";
    btn.classList.toggle("active", this.mode === "preview");
  },

  /* ---------- mode switching (spec v4 §3) ---------- */
  setMode(mode, { manual = false } = {}) {
    if (mode === this.mode) { this._updateModeButton(); return; }
    const wasPlaying = this.playing;
    const t = this.currentEdlTime();
    this.pause();
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

  async _autoSelectMode() {
    if (!state.project?.preview?.path) { if (this.mode === "preview") this.setMode("draft"); return; }
    const token = (this._autoSelectToken = (this._autoSelectToken || 0) + 1);
    let stale = true;
    try { stale = await Editor.previewIsStale(); } catch (_e) { stale = true; }
    if (token !== this._autoSelectToken) return; // a newer check finished first — drop this one
    this.setMode(stale ? "draft" : "preview");
  },

  _previewSrc() {
    const path = state.project?.preview?.path;
    if (!path || !Editor.pid) return null;
    return `/api/projects/${Editor.pid}/media/file?path=${encodeURIComponent(path)}`;
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
    if (btn) btn.textContent = "⏸";
  },
  pause() {
    this.playing = false;
    if (this.mode === "preview") this._previewVideo?.pause();
    else this._active()?.pause();
    const btn = document.getElementById("pp-playpause");
    if (btn) btn.textContent = "▶";
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
