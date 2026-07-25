/* Reel Editor (spec v5 + v5.8a/v5.8b UI): full takeover of the editor area
   when a reel is opened for editing — fix framing (crop_x), extend/trim the
   cut beyond the AI window (in_override/out_override, now per-segment),
   hand-edit subtitles (cue_overrides + subtitle_style), restyle, re-render
   THAT reel, and (v5.8b) build/edit multi-segment "podcast case" reels.

   Entry point: ui/tabs/reels.js's "Edit" button calls window.ReelEditor.open(rid).
   "← Back to project" / Esc calls window.ReelEditor.close().

   Server contract (magic_video_editor/api/reels.py): PATCH /api/projects/{pid}/reels/{rid}
   (in_override/out_override/crop_x/cue_overrides/subtitle_style/title/
   description/segments/transitions — all optional, partial; `segments` and
   `transitions` REPLACE the whole list wholesale — see that file's
   SegmentInput/TransitionInput docstrings), POST .../regenerate-copy, GET
   .../cues (GLOBAL index across every segment concatenated in order, text
   already reflects cue_overrides, each cue carries a `segment` field).
   Render itself reuses the existing POST .../render (magic_video_editor/api/pipeline.py),
   which enqueues through the queue (state.queue, kept fresh by core.js's
   global poll).

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
    _cropDragPreview: null, // 0..1 while dragging the framing window, else null
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
      this._closeAddPicker();

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
      this._wireFrameDrag();
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
    },

    play() {
      const v = document.getElementById("re-video");
      if (!v || !this._segments().length) return;
      const { start, end } = this._segEffectiveWindow(this._activeSeg);
      if (v.currentTime < start || v.currentTime >= end) { try { v.currentTime = start; } catch (_e) { /* ignore */ } }
      this.playing = true;
      v.play().catch(() => {});
      const btn = document.getElementById("re-playpause");
      if (btn) { btn.innerHTML = '<i data-lucide="pause"></i>'; refreshIcons(); }
    },
    pause() {
      this.playing = false;
      const v = document.getElementById("re-video");
      v?.pause();
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
      refreshIcons();
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
              <video controls preload="metadata" src="/api/projects/${this.pid}/media/file?path=${encodeURIComponent(reel.path)}"></video>
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
