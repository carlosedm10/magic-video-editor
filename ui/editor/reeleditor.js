/* Reel Editor (spec v5): full takeover of the editor area when a reel is
   opened for editing — fix framing (crop_x), extend/trim the cut beyond the
   AI window (in_override/out_override), hand-edit subtitles (cue_overrides +
   subtitle_style), restyle, re-render THAT reel.

   Entry point: ui/tabs/reels.js's "Edit" button calls window.ReelEditor.open(rid).
   "← Back to project" / Esc calls window.ReelEditor.close().

   Server contract (cutroom/api/reels.py): PATCH /api/projects/{pid}/reels/{rid}
   (in_override/out_override/crop_x/cue_overrides/subtitle_style/title/
   description — all optional, partial), POST .../regenerate-copy, GET
   .../cues (index-keyed, text already reflects cue_overrides, re-based so
   0 == the effective window's start). Render itself reuses the existing
   POST .../render (cutroom/api/pipeline.py), which enqueues through the
   queue (state.queue, kept fresh by core.js's global poll).

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

  function _effectiveWindowFor(reel, clipDuration, dragPreview) {
    if (dragPreview) return dragPreview;
    let duration = clipDuration;
    if (!duration || duration <= 0) duration = Math.max(reel.end || 0, reel.out_override || 0) || 1;
    let start = reel.in_override;
    start = start == null ? reel.start : Number(start);
    let end = reel.out_override;
    end = end == null ? reel.end : Number(end);
    start = Math.max(0, Math.min(start, duration));
    end = Math.max(start + 0.05, Math.min(end, duration));
    return { start, end };
  }

  /* Defensive against a real backend data bug observed live against project
     c7642fc7755e: cutroom/pipeline/reels.py does `list(copy.get("hashtags") or [])`
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

  const SUB_STYLES = [["clean", "Clean"], ["bold", "Bold"], ["karaoke", "Karaoke"]];
  const SUB_SIZES = [["S", "S"], ["M", "M"], ["L", "L"]];
  const SUB_POSITIONS = [["bottom", "Bottom"], ["center", "Center"]];

  const ReelEditor = {
    pid: null,
    rid: null,
    clip: null,
    isOpen: false,
    activeTab: "reel",
    playing: false,
    cues: [],
    thumbs: { meta: null, stripUrl: null, failed: false },
    _dragPreview: null,     // {start,end} while dragging a strip edge, else null
    _cropDragPreview: null, // 0..1 while dragging the framing window, else null
    _pxPerSec: 1,
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
      this._dragPreview = null;
      this._cropDragPreview = null;
      this._exportSig = null;
      this.playing = false;
      this.activeTab = this.activeTab || "reel";

      this._ensureStyles();
      this._ensureDom();

      try { closeDrawer(); } catch (_e) { /* drawer may not be open */ }
      try { if (!$("#settings-overlay").hidden) closeSettings(); } catch (_e) { /* settings not open */ }

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

      this._layoutFrame();
      this._mountVideo();
      this._loadThumbs();
      this._reloadCues();
      this._loadSubsBase();
      this.switchTab(this.activeTab);
      this._startTick();
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
        #re-frame { position: relative; background: #000; overflow: hidden; border-radius: 8px; cursor: ew-resize; }
        #re-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #000;
          pointer-events: none; }
        .re-band { position: absolute; top: 0; bottom: 0; background: rgba(2, 3, 7, .62); pointer-events: none; z-index: 2; }
        #re-band-l { left: 0; } #re-band-r { right: 0; }
        #re-crop-window { position: absolute; top: 0; bottom: 0; border: 2px solid var(--accent-hover);
          box-shadow: 0 0 0 1px rgba(0,0,0,.4), 0 0 18px rgba(194,32,48,.5); pointer-events: none; z-index: 3; }
        #re-frame-note { position: absolute; left: 8px; right: 8px; bottom: 8px; text-align: center; z-index: 4;
          background: rgba(0,0,0,.5); border-radius: 8px; padding: 4px 6px; }
        .re-transport { flex-shrink: 0; padding: 8px 16px; border-top: 1px solid var(--border); }

        #re-inspector { grid-area: inspector; display: flex; flex-direction: column; overflow: hidden; }
        .re-tabs { display: flex; flex-shrink: 0; border-bottom: 1px solid var(--border); background: var(--panel2); }
        .re-tab { flex: 1; background: none; border: none; border-bottom: 2px solid transparent; color: var(--dim);
          cursor: pointer; padding: 9px 4px; font-size: 12px; transition: color .15s ease, border-color .15s ease; }
        .re-tab:hover { color: var(--text); }
        .re-tab.active { color: var(--text); border-bottom-color: var(--accent-hover); font-weight: 600; }
        .re-tabpanels { flex: 1; overflow-y: auto; padding: 12px; }
        .re-readonly-row { display: flex; gap: 14px; margin: 6px 0; font-size: 12px; color: var(--dim); }
        .re-readonly-row b { color: var(--text); }

        #re-timeline-pane { grid-area: timeline; display: flex; flex-direction: column; overflow: hidden;
          padding: 8px 12px 10px; }
        #re-strip-wrap { flex: 1; min-height: 0; position: relative; overflow: hidden; border-radius: 8px;
          background: var(--panel2); border: 1px solid var(--border); }
        #re-strip-track { position: relative; height: 100%; }
        .re-strip-film { position: absolute; inset: 0; opacity: .7; background-repeat: no-repeat; }
        #re-ai-marker { position: absolute; top: 0; bottom: 0; border: 1px dashed rgba(154,163,178,.65);
          background: rgba(154,163,178,.08); pointer-events: none; }
        #re-window { position: absolute; top: 4px; bottom: 4px; border: 2px solid var(--accent-hover);
          border-radius: 6px; background: rgba(194,32,48,.14); }
        .re-edge { position: absolute; top: 0; bottom: 0; width: 12px; cursor: ew-resize; }
        .re-edge-l { left: -5px; } .re-edge-r { right: -5px; }
        .re-tl-inputs { flex-shrink: 0; padding-top: 8px; }
        .re-tl-inputs input[type=number] { width: 80px; }

        #re-cue-list .field-row { align-items: center; }
        #re-cue-list .field-row label { width: 56px; }
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
          <button id="re-back" class="btn small" title="Esc">← Back to project</button>
          <div class="re-heading">
            <b id="re-heading-title">Reel</b>
            <span id="re-heading-sub" class="dim"></span>
          </div>
          <span class="grow"></span>
          <span id="re-save-state" class="dim"></span>
        </div>
        <div class="re-body">
          <section id="re-preview-pane" class="re-area">
            <div id="re-frame-wrap">
              <div id="re-frame" title="Drag to reframe the 9:16 crop">
                <video id="re-video" playsinline preload="auto"></video>
                <div id="re-band-l" class="re-band"></div>
                <div id="re-band-r" class="re-band"></div>
                <div id="re-crop-window"></div>
                <div id="re-frame-note" class="dim" hidden></div>
              </div>
            </div>
            <div class="re-transport row">
              <button id="re-playpause" class="btn small" title="Space">▶</button>
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
            <div class="dim" style="margin-bottom:6px">Drag the bright edges to trim — the dashed band is the
              original AI cut; you can extend past it.</div>
            <div id="re-strip-wrap">
              <div id="re-strip-track">
                <div id="re-strip-film" class="re-strip-film"></div>
                <div id="re-ai-marker"></div>
                <div id="re-window">
                  <div class="re-edge re-edge-l" data-edge="start"></div>
                  <div class="re-edge re-edge-r" data-edge="end"></div>
                </div>
              </div>
            </div>
            <div class="re-tl-inputs row">
              <label class="dim">In</label><input type="number" step="0.1" id="re-in-input">
              <label class="dim">Out</label><input type="number" step="0.1" id="re-out-input">
              <span class="dim" id="re-dur-label"></span>
            </div>
          </section>
        </div>`;
      content.appendChild(div);
      this._wireOnce();
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
      const inInput = document.getElementById("re-in-input");
      if (inInput) inInput.onchange = () => {
        const { end } = this._effectiveWindow();
        this._commitWindow(Number(inInput.value), end);
      };
      const outInput = document.getElementById("re-out-input");
      if (outInput) outInput.onchange = () => {
        const { start } = this._effectiveWindow();
        this._commitWindow(start, Number(outInput.value));
      };
      this._wireStripDrag();
      this._wireFrameDrag();
      window.addEventListener("resize", () => { if (this.isOpen) this._layoutFrame(); });
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

    _effectiveWindow() {
      const reel = this._reel();
      return _effectiveWindowFor(reel, this.clip?.info?.duration, this._dragPreview);
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

    /* ---------- framing (crop_x) ---------- */

    _layoutFrame() {
      const wrap = document.getElementById("re-frame-wrap");
      const frame = document.getElementById("re-frame");
      if (!wrap || !frame) return;
      const cw = wrap.clientWidth, ch = wrap.clientHeight;
      const w = this.clip?.info?.width || 16, h = this.clip?.info?.height || 9;
      const ar = w / h || 16 / 9;
      let fw = cw, fh = cw / ar;
      if (fh > ch) { fh = ch; fw = ch * ar; }
      frame.style.width = `${Math.max(1, fw)}px`;
      frame.style.height = `${Math.max(1, fh)}px`;
      this._renderCropOverlay();
    },

    _cropWidthFrac() {
      const w = this.clip?.info?.width || 16, h = this.clip?.info?.height || 9;
      const targetAr = 9 / 16;
      let cropH = h, cropW = cropH * targetAr;
      if (cropW > w) { cropW = w; cropH = cropW / targetAr; }
      return cropW / w;
    },

    _renderCropOverlay() {
      const frame = document.getElementById("re-frame");
      const bandL = document.getElementById("re-band-l");
      const bandR = document.getElementById("re-band-r");
      const win = document.getElementById("re-crop-window");
      const note = document.getElementById("re-frame-note");
      if (!frame || !bandL || !bandR || !win) return;
      const fw = frame.clientWidth || 1;
      const cropWFrac = this._cropWidthFrac();
      if (cropWFrac >= 0.999) {
        bandL.style.display = "none"; bandR.style.display = "none"; win.style.display = "none";
        if (note) { note.hidden = false; note.textContent = "Source is already narrower than 9:16 — no horizontal crop available."; }
        return;
      }
      if (note) note.hidden = true;
      bandL.style.display = ""; bandR.style.display = ""; win.style.display = "";
      const reel = this._reel();
      const centerFrac = this._cropDragPreview != null ? this._cropDragPreview
        : (reel.crop_x != null ? Number(reel.crop_x) : 0.5);
      let leftFrac = centerFrac - cropWFrac / 2;
      leftFrac = Math.max(0, Math.min(leftFrac, 1 - cropWFrac));
      const leftPx = leftFrac * fw, widthPx = cropWFrac * fw;
      bandL.style.width = `${leftPx}px`;
      bandR.style.width = `${Math.max(0, fw - (leftPx + widthPx))}px`;
      win.style.left = `${leftPx}px`;
      win.style.width = `${widthPx}px`;
    },

    _wireFrameDrag() {
      const frame = document.getElementById("re-frame");
      if (!frame) return;
      frame.addEventListener("pointerdown", (e) => this._onFrameDown(e));
    },

    _onFrameDown(e) {
      const frame = document.getElementById("re-frame");
      if (!frame) return;
      const rect = frame.getBoundingClientRect();
      if (!rect.width || this._cropWidthFrac() >= 0.999) return;
      const update = (clientX) => {
        const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        this._cropDragPreview = frac;
        this._renderCropOverlay();
      };
      update(e.clientX);
      const onMove = (ev) => update(ev.clientX);
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        const frac = this._cropDragPreview;
        if (frac == null) return;
        // Keep showing the dragged (optimistic) position until the PATCH
        // settles -- clearing _cropDragPreview here would make the overlay
        // snap back to the old crop_x for the round-trip's duration (or
        // forever, on failure), which reads as "the drag didn't work" even
        // though it visibly did. Only drop it once the server confirms the
        // same value (success) so there's no visual jump either way.
        this._patch({ crop_x: Math.round(frac * 1000) / 1000 }, {
          afterSave: () => { this._cropDragPreview = null; this._renderCropOverlay(); },
        });
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    /* ---------- playback (single clip, virtual window, loop) ---------- */

    _mountVideo() {
      const v = document.getElementById("re-video");
      if (!v || !this.clip) return;
      const src = `/api/projects/${this.pid}/media/preview/${this.clip.id}`;
      if (v.dataset.src !== src) {
        v.dataset.src = src;
        v.src = src;
        v.onerror = () => console.error("Reel Editor video error", v.error);
        v.onloadedmetadata = () => {
          const { start } = this._effectiveWindow();
          try { v.currentTime = start; } catch (_e) { /* not ready */ }
        };
      }
      v.ontimeupdate = () => this._onTimeUpdate();
    },

    _onTimeUpdate() {
      const v = document.getElementById("re-video");
      if (!v) return;
      const { start, end } = this._effectiveWindow();
      if (v.currentTime >= end - 0.05) {
        try { v.currentTime = start; } catch (_e) { /* ignore */ }
        if (!this.playing) v.pause();
      } else if (v.currentTime < start - 0.25) {
        try { v.currentTime = start; } catch (_e) { /* ignore */ }
      }
      this._updateTimeDisplay();
    },

    play() {
      const v = document.getElementById("re-video");
      if (!v) return;
      const { start, end } = this._effectiveWindow();
      if (v.currentTime < start || v.currentTime >= end) { try { v.currentTime = start; } catch (_e) { /* ignore */ } }
      this.playing = true;
      v.play().catch(() => {});
      const btn = document.getElementById("re-playpause");
      if (btn) btn.textContent = "⏸";
    },
    pause() {
      this.playing = false;
      const v = document.getElementById("re-video");
      v?.pause();
      const btn = document.getElementById("re-playpause");
      if (btn) btn.textContent = "▶";
    },
    togglePlay() { this.playing ? this.pause() : this.play(); },

    _updateTimeDisplay() {
      const el = document.getElementById("re-time");
      if (!el) return;
      const v = document.getElementById("re-video");
      const { start, end } = this._effectiveWindow();
      const local = Math.max(0, (v?.currentTime || 0) - start);
      el.textContent = `${fmtT(local)} / ${fmtT(end - start)}`;
    },

    /* ---------- mini-timeline (single-segment, full clip strip) ---------- */

    async _loadThumbs() {
      this.thumbs = { meta: null, stripUrl: null, failed: false };
      if (this.clip) {
        try {
          this.thumbs.meta = await api(`/projects/${this.pid}/thumbs/${this.clip.id}/meta`);
          this.thumbs.stripUrl = `/api/projects/${this.pid}/thumbs/${this.clip.id}/strip`;
        } catch (_e) {
          this.thumbs.failed = true; // 404 — no filmstrip generated yet; plain track is fine
        }
      }
      this._renderStrip();
    },

    _renderStrip() {
      const wrap = document.getElementById("re-strip-wrap");
      const track = document.getElementById("re-strip-track");
      if (!wrap || !track) return;
      const dur = this.clip?.info?.duration || 1;
      const trackW = wrap.clientWidth || 600;
      this._pxPerSec = trackW / dur;
      track.style.width = `${trackW}px`;

      const film = document.getElementById("re-strip-film");
      if (film) {
        if (this.thumbs.meta && this.thumbs.stripUrl) {
          const bgW = Math.max(1, this.thumbs.meta.count * this.thumbs.meta.interval_s * this._pxPerSec);
          film.style.backgroundImage = `url('${this.thumbs.stripUrl}')`;
          film.style.backgroundRepeat = "no-repeat";
          film.style.backgroundSize = `${bgW.toFixed(1)}px 100%`;
          film.style.backgroundPosition = "0 0";
          film.hidden = false;
        } else {
          film.hidden = true;
        }
      }

      const reel = this._reel();
      const aiMarker = document.getElementById("re-ai-marker");
      if (aiMarker) {
        aiMarker.style.left = `${(reel.start || 0) * this._pxPerSec}px`;
        aiMarker.style.width = `${Math.max(2, ((reel.end || 0) - (reel.start || 0)) * this._pxPerSec)}px`;
      }
      this._renderWindowRegion();
    },

    _renderWindowRegion() {
      const win = document.getElementById("re-window");
      if (!win) return;
      const { start, end } = this._effectiveWindow();
      const px = this._pxPerSec || 1;
      win.style.left = `${start * px}px`;
      win.style.width = `${Math.max(3, (end - start) * px)}px`;
      const inInput = document.getElementById("re-in-input");
      const outInput = document.getElementById("re-out-input");
      if (inInput && document.activeElement !== inInput) inInput.value = start.toFixed(2);
      if (outInput && document.activeElement !== outInput) outInput.value = end.toFixed(2);
      const durLabel = document.getElementById("re-dur-label");
      if (durLabel) durLabel.textContent = `Duration ${fmtT(end - start)}`;
    },

    _wireStripDrag() {
      const win = document.getElementById("re-window");
      if (!win) return;
      win.querySelectorAll(".re-edge").forEach((edge) => {
        edge.addEventListener("pointerdown", (e) => this._onEdgeDown(e, edge.dataset.edge));
      });
    },

    _onEdgeDown(e, field) {
      e.stopPropagation();
      const startX = e.clientX;
      const px = this._pxPerSec || 1;
      const dur = this.clip?.info?.duration ?? Infinity;
      const orig = this._effectiveWindow();
      const onMove = (ev) => {
        const deltaSec = (ev.clientX - startX) / px;
        let s = orig.start, en = orig.end;
        if (field === "start") {
          s = Math.round((orig.start + deltaSec) / 0.05) * 0.05;
          s = Math.max(0, Math.min(s, en - 0.2));
        } else {
          en = Math.round((orig.end + deltaSec) / 0.05) * 0.05;
          en = Math.max(s + 0.2, Math.min(en, dur));
        }
        this._dragPreview = { start: s, end: en };
        this._renderWindowRegion();
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        const p = this._dragPreview;
        this._dragPreview = null;
        if (p) this._commitWindow(p.start, p.end);
        else this._renderWindowRegion();
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    _commitWindow(s, e) {
      const dur = this.clip?.info?.duration ?? Infinity;
      s = Math.max(0, Math.min(s, dur));
      e = Math.max(s + 0.1, Math.min(e, dur));
      // Kept as the optimistic value (not nulled) until the PATCH settles --
      // _effectiveWindow() prefers _dragPreview when set, so the strip/inputs/
      // video-loop bounds all keep reflecting the just-committed trim through
      // the round-trip instead of snapping back to the stale server value
      // (or staying snapped-back forever if the save fails).
      this._dragPreview = { start: s, end: e };
      this._renderWindowRegion();
      this._patch(
        { in_override: Math.round(s * 1000) / 1000, out_override: Math.round(e * 1000) / 1000 },
        { afterSave: () => { this._dragPreview = null; this._afterWindowChange(); } },
      );
    },

    _afterWindowChange() {
      this._renderWindowRegion();
      if (this.activeTab === "reel") this._renderReelTab();
      this._reloadCues().then(() => { if (this.activeTab === "subs") this._renderSubsTab(); });
      const v = document.getElementById("re-video");
      if (v) {
        const { start, end } = this._effectiveWindow();
        if (v.currentTime < start || v.currentTime > end) { try { v.currentTime = start; } catch (_e) { /* ignore */ } }
      }
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
       in/out/duration/score readonly (spec v5: "title editable, score readonly") ---- */

    _renderReelTab() {
      const el = document.getElementById("re-panel-reel");
      if (!el) return;
      const reel = this._reel();
      const { start, end } = this._effectiveWindow();
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
            <button class="btn small" id="re-copy-btn">📋 Copy</button>
            <button class="btn small" id="re-regen-btn">↻ Regenerate copy</button>
          </div>
          <div class="re-readonly-row" style="margin-top:12px">
            <span>In <b>${start.toFixed(2)}s</b></span>
            <span>Out <b>${end.toFixed(2)}s</b></span>
            <span>Duration <b>${fmtT(end - start)}</b></span>
            <span>Score <b class="score">${reel.score ?? "—"}</b></span>
          </div>
        </div>`;

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
        copyBtn.textContent = ok ? "✓ Copied" : "Copy failed";
        setTimeout(() => { copyBtn.textContent = "📋 Copy"; }, 1500);
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
          regenBtn.textContent = "↻ Regenerate copy";
        }
      };
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
          <div class="hint">Typo fixes for this reel's burned-in captions (times are relative to the trimmed window).</div>
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
      return this.cues.map((c) => `
        <div class="field-row" data-cue="${c.index}">
          <label class="mono">${fmtT(c.start)}</label>
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
            <button class="btn primary small" id="re-render-btn" ${busy ? "disabled" : ""}>${busy ? "Rendering…" : "▶ Render"}</button>
            <span class="dim">${esc(statusText)}</span>
          </div>
          ${last?.status === "error" ? `<div class="dim" style="color:var(--danger);margin-top:6px">${esc(last.error || "Render failed")}</div>` : ""}
          ${reel.path ? `
            <div style="margin-top:10px">
              <video controls preload="metadata" src="/api/projects/${this.pid}/media/file?path=${encodeURIComponent(reel.path)}"></video>
              <div class="row" style="margin-top:6px">
                <button class="btn small" id="re-open-folder">📂 Open in Finder</button>
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
