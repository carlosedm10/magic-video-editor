/* Virtual EDL playback: plays the ordered segment list by seeking the
   underlying clip <video> and auto-advancing at each segment's out point —
   no render needed to preview the current cut (docs/PLATFORM-SPEC.md
   "mental model changed" / "virtual preview of the CURRENT EDL"). Two
   <video> elements are kept in the DOM ("active" visible/playing, "idle"
   hidden); whenever the *next* segment comes from a different clip file we
   preload it into the idle element ahead of time and swap active<->idle at
   the junction instead of reloading, to minimize the gap. */

window.EditorUI = window.EditorUI || {};

const Player = {
  videos: null,
  activeIdx: 0,
  playing: false,
  epsilon: 0.05,
  _currentIndex: 0,
  _rafId: null,

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

    const playBtn = document.getElementById("pp-playpause");
    const fsBtn = document.getElementById("pp-fullscreen");
    if (playBtn) playBtn.onclick = () => this.togglePlay();
    if (fsBtn) fsBtn.onclick = () => this.toggleFullscreen();

    this.onSegmentsChanged();
    if (!this._rafId) this._startLoop();
  },

  onSegmentsChanged() {
    if (!this.videos) return;
    if (!Editor.segments || !Editor.segments.length) {
      this.pause();
      this._showEmpty(true);
      return;
    }
    this._showEmpty(false);
    const i = Math.min(Editor.selected, Editor.segments.length - 1);
    this._loadSegment(i, Editor.segments[i].start, { andPlay: false });
  },

  _showEmpty(show) {
    const el = document.getElementById("player-empty");
    if (el) el.style.display = show ? "flex" : "none";
    this.videos?.forEach((v) => (v.style.visibility = show ? "hidden" : "visible"));
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
    if (!Editor.segments?.length || !this.videos) return;
    this.playing = true;
    this._active().play().catch(() => {});
    const btn = document.getElementById("pp-playpause");
    if (btn) btn.textContent = "⏸";
  },
  pause() {
    this.playing = false;
    this._active()?.pause();
    const btn = document.getElementById("pp-playpause");
    if (btn) btn.textContent = "▶";
  },
  togglePlay() { this.playing ? this.pause() : this.play(); },

  toggleFullscreen() {
    const stage = document.getElementById("player-stage");
    if (!stage) return;
    if (!document.fullscreenElement) stage.requestFullscreen?.().catch(() => {});
    else document.exitFullscreen?.().catch(() => {});
  },

  seekToEdlTime(t) {
    const hit = Editor.segmentAtEdlTime(t);
    if (!hit) return;
    Editor.select(hit.index);
    this._loadSegment(hit.index, hit.local, { andPlay: this.playing });
  },

  currentEdlTime() {
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

  _advance() {
    const nextIndex = this._currentIndex + 1;
    const next = Editor.segments?.[nextIndex];
    if (!next) { this.pause(); return; }
    const wasPlaying = this.playing;
    const idle = this._idle();
    if (idle.dataset.clipId === next.clip_id && idle.readyState >= 2) {
      this._active().pause();
      this._active().ontimeupdate = null;
      this.activeIdx = 1 - this.activeIdx;
      this._currentIndex = nextIndex;
      const nowActive = this._active();
      try { nowActive.currentTime = next.start; } catch (_e) { /* ignore */ }
      nowActive.ontimeupdate = () => this._onTimeUpdate();
      this.videos.forEach((v, i) => v.classList.toggle("active", i === this.activeIdx));
      if (wasPlaying) nowActive.play().catch(() => {});
      this._preloadNext(nextIndex);
    } else {
      // Preload wasn't ready in time (rare) — fall back to a plain load on
      // the currently active element; costs a small gap but stays correct.
      this._loadSegment(nextIndex, next.start, { andPlay: wasPlaying });
    }
    // Editor.select() already re-renders the timeline selection + inspector
    // (see Editor._notifySelection in ui/editor/state.js) — no need to do it
    // again here.
    Editor.select(nextIndex);
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
        if (this.videos) window.EditorUI.timeline?.updatePlayhead(this.currentEdlTime());
      } catch (_e) { /* never let the raf loop die */ }
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  },
};

window.EditorUI.player = Player;
