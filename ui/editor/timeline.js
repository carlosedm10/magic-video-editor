/* Bottom timeline: adaptive ruler, zoom (slider + wheel + min-zoom-fits-all +
   Fit button), one video track of blocks (width ∝ duration) with filmstrip +
   waveform art, selection highlight, playhead synced both ways with the
   player, drag blocks to reorder, drag block edges to trim (live feedback,
   0.05s snap, optional snap-to-playhead/edges, left-edge drags stay anchored
   by their right edge on screen), per-junction transition chips, markers on
   the ruler, a render-status bar, media-bin drop-to-append, a history panel,
   and the toolbar (split / ripple-delete / undo / redo / reset / save / snap
   toggle / history / shortcuts popover). All EDL mutations go through
   `Editor` (ui/editor/state.js), which owns history + autosave.

   Keyboard map (spec v4 §4 "Timeline pro" + the "?" popover):
     Space  play/pause      J  jump back 2s       K  pause
     L      play (2x on 2nd press while playing)  X  split at playhead
     I/O    set selected segment in/out at playhead (clip-local, clamped)
     Del    ripple delete   S  toggle snapping     M  add marker
     ⌘Z/⇧⌘Z undo/redo       +/-  zoom              ?  shortcuts popover

   DOM note: this module injects its own <style> block and a couple of
   container divs (render bar, markers strip, shortcuts popover, history
   popover) at mount time rather than depending on ui/index.html/ui/style.css
   (owned by other agents this phase) — see the injectStyles()/_ensureChrome()
   functions. */

window.EditorUI = window.EditorUI || {};

const Timeline = {
  pxPerSec: 40,
  snapping: true,
  _drag: null,       // active drag/trim operation, or null
  _wired: false,
  _dragLeftAnchor: null,   // {index, anchorRightPx} while live-dragging a LEFT edge
  _lastTrimValue: null,    // last (possibly snapped) value computed during an edge drag
  _thumbCache: new Map(),  // clip_id -> {meta, peaks, stripUrl, metaFailed, loading}
  _shortcutsOpen: false,
  _historyOpen: false,
  _zoomMin: 4,             // recomputed every render() from total duration + viewport (spec v5 addendum "Zoom")

  mount() {
    this._injectStyles();
    this._ensureChrome();
    this.render();
    if (this._wired) return;
    this._wired = true;

    const zoom = document.getElementById("tl-zoom");
    if (zoom) {
      zoom.max = "220";
      zoom.value = String(this.pxPerSec);
      zoom.oninput = () => { this._setZoom(Number(zoom.value)); };
    }
    const scroll = document.getElementById("timeline-scroll");
    if (scroll && !this._resizeObserved) {
      // Viewport width feeds directly into the min-zoom-fits-all computation
      // (spec v5 addendum "Zoom") — re-render (which recomputes it) whenever
      // the timeline pane is resized (window resize, sidebar collapse, …).
      this._resizeObserved = true;
      new ResizeObserver(() => this.render()).observe(scroll);
    }
    if (scroll) {
      scroll.addEventListener("wheel", (e) => {
        if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return; // let horizontal wheel/trackpad pan normally
        e.preventDefault();
        const rect = scroll.getBoundingClientRect();
        const xInScroll = e.clientX - rect.left + scroll.scrollLeft;
        const timeAtCursor = xInScroll / this.pxPerSec;
        const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        this._setZoom(this.pxPerSec * factor);
        const newX = timeAtCursor * this.pxPerSec;
        scroll.scrollLeft = Math.max(0, newX - (e.clientX - rect.left));
      }, { passive: false });
    }

    const content = document.getElementById("timeline-content");
    if (content) {
      content.addEventListener("pointerdown", (e) => this._onBackgroundPointerDown(e));
      content.addEventListener("dragover", (e) => this._onBinDragOver(e));
      content.addEventListener("dragleave", () => this._onBinDragLeave());
      content.addEventListener("drop", (e) => this._onBinDrop(e));
    }

    const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
    bind("tl-split", () => this.splitAtPlayhead());
    bind("tl-delete", () => Editor.deleteSelected());
    bind("tl-undo", () => Editor.undo());
    bind("tl-redo", () => Editor.redo());
    bind("tl-save", () => Editor.save());
    bind("tl-reset", async () => {
      if (!confirm("Discard manual edits and reset the timeline to the AI cut?")) return;
      await Editor.resetToAiCut();
    });
    bind("tl-fit", () => this.zoomToFit());

    // Relabel/retitle two existing v3 toolbar controls in place (index.html
    // is owned by another agent this phase, but a DOM mutation from here is
    // fair game): ripple delete is worth calling out explicitly, and the
    // split shortcut hint is stale now that S means "toggle snapping".
    const delBtn = document.getElementById("tl-delete");
    if (delBtn) {
      delBtn.innerHTML = '<i data-lucide="trash-2"></i> Ripple delete';
      delBtn.title = "Ripple delete (Del) — closes the gap";
    }
    const splitBtn = document.getElementById("tl-split");
    if (splitBtn) splitBtn.title = "Split at playhead (X)";
    refreshIcons();

    document.addEventListener("keydown", (e) => this._onKeydown(e));
  },

  /* ---------- one-time injected chrome (style + containers this module
     needs but that don't exist in ui/index.html / ui/style.css) ---------- */

  _injectStyles() {
    if (document.getElementById("tl-pro-styles")) return;
    const style = document.createElement("style");
    style.id = "tl-pro-styles";
    style.textContent = `
      .tl-renderbar { height: 6px; margin: 0 0 2px; border-radius: 3px; flex-shrink: 0;
        background: var(--panel2); border: 1px solid var(--border); cursor: pointer;
        position: relative; overflow: hidden; transition: background .2s ease, border-color .2s ease; }
      .tl-renderbar::after { content: ""; position: absolute; inset: 0; opacity: .9; }
      .tl-renderbar.stale { border-color: var(--accent); }
      .tl-renderbar.stale::after { background: linear-gradient(90deg, var(--accent), var(--accent-hover)); }
      .tl-renderbar.fresh { border-color: var(--accent2); }
      .tl-renderbar.fresh::after { background: var(--accent2); }
      .tl-renderbar.unknown::after { background: var(--border); }
      .tl-renderbar-label { position: absolute; right: 6px; top: -1px; font-size: 9px; line-height: 6px;
        color: var(--dim); }
      .tl-film { position: absolute; inset: 0; bottom: 16px; z-index: 0; opacity: .6; pointer-events: none; }
      .tl-wave { position: absolute; left: 0; right: 0; bottom: 0; height: 16px; z-index: 0; pointer-events: none;
        display: block; width: 100%; }
      .tl-block .tl-label { position: relative; z-index: 1; text-shadow: 0 1px 3px rgba(0,0,0,.9); }
      .tl-block .tl-edge { z-index: 2; }
      .tl-snap-btn.active { border-color: var(--accent2); color: var(--accent2); }
      .tl-markers-strip { position: absolute; top: 0; left: 0; right: 0; height: 22px; pointer-events: none; }
      .tl-marker { position: absolute; top: 0; transform: translateX(-50%); pointer-events: auto; cursor: pointer;
        width: 0; height: 0; border-left: 6px solid transparent; border-right: 6px solid transparent;
        border-top: 8px solid var(--warn); }
      .tl-marker:hover { border-top-color: var(--accent-hover); }
      .tl-shortcuts-pop { position: absolute; bottom: 100%; right: 0; margin-bottom: 6px; width: 280px;
        background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px;
        backdrop-filter: blur(16px); box-shadow: 0 8px 24px rgba(0,0,0,.4); z-index: 20; font-size: 12px; }
      .tl-shortcuts-pop table { width: 100%; border-collapse: collapse; }
      .tl-shortcuts-pop td { padding: 2px 0; color: var(--dim); }
      .tl-shortcuts-pop td:first-child { color: var(--text); font-variant-numeric: tabular-nums;
        white-space: nowrap; padding-right: 10px; width: 1%; }
      #timeline-content.tl-drop-target { outline: 2px dashed var(--accent); outline-offset: -2px; }
      .tl-history-pop { position: absolute; bottom: 100%; right: 0; margin-bottom: 6px; width: 260px;
        max-height: 320px; overflow-y: auto; background: var(--panel); border: 1px solid var(--border);
        border-radius: 12px; padding: 8px; backdrop-filter: blur(16px); box-shadow: 0 8px 24px rgba(0,0,0,.4);
        z-index: 20; font-size: 12px; }
      .tl-history-title { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
        padding: 2px 6px 6px; }
      .tl-history-list { list-style: none; margin: 0; padding: 0; }
      .tl-history-item { display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
        padding: 6px 6px; border-radius: 8px; cursor: pointer; color: var(--text); }
      .tl-history-item:hover { background: var(--panel2); }
      .tl-history-item.active { background: var(--accent); color: #fff; }
      .tl-history-item.active .tl-history-time { color: rgba(255,255,255,.75); }
      .tl-history-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .tl-history-time { color: var(--dim); font-variant-numeric: tabular-nums; flex-shrink: 0; }
      .tl-history-empty { color: var(--dim); padding: 6px; }

      /* ---- manual overlay track (spec v5.9b) ---- */
      .tl-overlay-track { position: absolute; left: 0; right: 0; top: 22px; height: 26px; z-index: 1;
        border-bottom: 1px solid var(--border); }
      .tl-overlay-track.tl-drop-target { outline: 2px dashed var(--accent2); outline-offset: -2px; }
      .ov-block { position: absolute; top: 3px; height: 20px; border-radius: 6px; overflow: hidden;
        background: rgba(53,194,143,.20); border: 1px solid var(--accent2); cursor: grab;
        display: flex; align-items: center; }
      .ov-block:hover { background: rgba(53,194,143,.3); }
      .ov-block.selected { background: rgba(53,194,143,.42); box-shadow: 0 0 0 1px var(--accent2); }
      .ov-block .ov-label { padding: 0 6px; font-size: 10px; color: var(--text); white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis; pointer-events: none; }
      .ov-edge { position: absolute; top: 0; bottom: 0; width: 6px; cursor: ew-resize; z-index: 2; }
      .ov-edge-l { left: 0; } .ov-edge-r { right: 0; }
      .ov-edge:hover { background: rgba(53,194,143,.6); }
    `;
    document.head.appendChild(style);
  },

  _ensureChrome() {
    const pane = document.getElementById("timeline-pane");
    const toolbar = document.querySelector("#timeline-pane .timeline-toolbar");
    const scroll = document.getElementById("timeline-scroll");
    if (pane && scroll && !document.getElementById("tl-renderbar")) {
      const bar = document.createElement("div");
      bar.id = "tl-renderbar";
      bar.className = "tl-renderbar unknown";
      bar.title = "Preview render status — click to render a preview";
      bar.innerHTML = `<span class="tl-renderbar-label" id="tl-renderbar-label"></span>`;
      bar.onclick = () => this._enqueuePreviewRender();
      pane.insertBefore(bar, scroll);
    }
    if (toolbar && !document.getElementById("tl-snap")) {
      const snapBtn = document.createElement("button");
      snapBtn.id = "tl-snap";
      snapBtn.className = "btn small tl-snap-btn active";
      snapBtn.title = "Snap to playhead/edges while trimming (S)";
      snapBtn.innerHTML = '<i data-lucide="magnet"></i> Snap';
      snapBtn.onclick = () => this._toggleSnapping();
      toolbar.appendChild(snapBtn);
    }
    if (toolbar && !document.getElementById("tl-fit")) {
      const fitBtn = document.createElement("button");
      fitBtn.id = "tl-fit";
      fitBtn.className = "btn small";
      fitBtn.title = "Zoom to fit the whole timeline (~20% spare room)";
      fitBtn.innerHTML = '<i data-lucide="maximize-2"></i> Fit';
      toolbar.appendChild(fitBtn); // onclick wired in mount() via bind()
    }
    if (toolbar && !document.getElementById("tl-history-btn")) {
      const wrap = document.createElement("div");
      wrap.style.position = "relative";
      wrap.id = "tl-history-wrap";
      const btn = document.createElement("button");
      btn.id = "tl-history-btn";
      btn.className = "btn small";
      btn.title = "Edit history";
      btn.innerHTML = '<i data-lucide="history"></i>';
      btn.onclick = (e) => { e.stopPropagation(); this._toggleHistory(); };
      wrap.appendChild(btn);
      toolbar.appendChild(wrap);
      document.addEventListener("pointerdown", (e) => {
        if (this._historyOpen && !wrap.contains(e.target)) this._toggleHistory(false);
      });
    }
    if (toolbar && !document.getElementById("tl-shortcuts-btn")) {
      const wrap = document.createElement("div");
      wrap.style.position = "relative";
      wrap.id = "tl-shortcuts-wrap";
      const btn = document.createElement("button");
      btn.id = "tl-shortcuts-btn";
      btn.className = "btn small";
      btn.title = "Keyboard shortcuts";
      btn.textContent = "?";
      btn.onclick = (e) => { e.stopPropagation(); this._toggleShortcuts(); };
      wrap.appendChild(btn);
      toolbar.appendChild(wrap);
      document.addEventListener("pointerdown", (e) => {
        if (this._shortcutsOpen && !wrap.contains(e.target)) this._toggleShortcuts(false);
      });
    }
    if (scroll && !document.getElementById("timeline-markers")) {
      const content = document.getElementById("timeline-content");
      if (content) {
        const strip = document.createElement("div");
        strip.id = "timeline-markers";
        strip.className = "tl-markers-strip";
        content.appendChild(strip);
      }
    }
    this._ensureOverlayTrack();
  },

  /* ---------- manual overlay track chrome (spec v5.9b) ----------
     A second, thinner lane ABOVE the main video track: created once here,
     pushed down from under the ruler (22px), and #timeline-track (owned by
     the CSS in ui/style.css as `top: 22px`) is shifted down via inline
     style to make room — inline style beats the external stylesheet rule
     for the same element/property, same trick the rest of this file already
     relies on for chrome ui/index.html/ui/style.css don't know about. */
  _ensureOverlayTrack() {
    const content = document.getElementById("timeline-content");
    const mainTrack = document.getElementById("timeline-track");
    if (!content || !mainTrack || document.getElementById("timeline-overlay-track")) return;
    const track = document.createElement("div");
    track.id = "timeline-overlay-track";
    track.className = "tl-overlay-track"; // top/height/left/right come from the injected stylesheet above
    content.appendChild(track);
    mainTrack.style.top = "48px"; // was 22px (CSS) — pushed down to make room for the 22-48px overlay lane

    track.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".ov-block")) return; // block/edge handlers below own their own drag
      Editor.deselectOverlay();
    });
    track.addEventListener("dragover", (e) => {
      if (!e.dataTransfer?.types?.includes("application/x-mve-clip")) return;
      e.preventDefault();
      e.stopPropagation(); // don't also let #timeline-content's main-track drop handler fire
      e.dataTransfer.dropEffect = "copy";
      track.classList.add("tl-drop-target");
    });
    track.addEventListener("dragleave", (e) => { e.stopPropagation(); track.classList.remove("tl-drop-target"); });
    track.addEventListener("drop", (e) => {
      track.classList.remove("tl-drop-target");
      const clipId = e.dataTransfer?.getData("application/x-mve-clip");
      if (!clipId) return;
      e.preventDefault();
      e.stopPropagation();
      const rect = track.getBoundingClientRect();
      const scroll = document.getElementById("timeline-scroll");
      const x = e.clientX - rect.left + (scroll?.scrollLeft || 0);
      Editor.insertOverlay(clipId, Math.max(0, x / this.pxPerSec));
    });
  },

  _toggleSnapping(force) {
    this.snapping = force != null ? force : !this.snapping;
    const btn = document.getElementById("tl-snap");
    if (btn) btn.classList.toggle("active", this.snapping);
  },

  _toggleShortcuts(force) {
    this._shortcutsOpen = force != null ? force : !this._shortcutsOpen;
    const wrap = document.getElementById("tl-shortcuts-wrap");
    if (!wrap) return;
    let pop = document.getElementById("tl-shortcuts-pop");
    if (this._shortcutsOpen) {
      if (!pop) {
        pop = document.createElement("div");
        pop.id = "tl-shortcuts-pop";
        pop.className = "tl-shortcuts-pop";
        const rows = [
          ["Space", "Play / pause"], ["J", "Jump back 2s"], ["K", "Pause"],
          ["L", "Play (again = 2×)"], ["X", "Split at playhead"],
          ["I / O", "Set in / out at playhead"], ["Del", "Ripple delete"],
          ["S", "Toggle snapping"], ["M", "Add marker"],
          ["⌘Z / ⇧⌘Z", "Undo / redo"], ["+ / -", "Zoom in / out"],
        ];
        pop.innerHTML = "<table>" + rows.map(([k, d]) =>
          `<tr><td>${esc(k)}</td><td>${esc(d)}</td></tr>`).join("") + "</table>";
        wrap.appendChild(pop);
      }
    } else if (pop) {
      pop.remove();
    }
  },

  /* ---------- history panel (spec v5 addendum "Undo history panel") ----------
     Reuses the same button chrome pattern as the "?" shortcuts popover:
     small toolbar toggle + click-outside-to-close, but the content is
     re-rendered on every Editor history push/undo/redo (via Editor.
     _notifyHistory -> renderHistoryPanel) so it never goes stale while open. */
  _toggleHistory(force) {
    this._historyOpen = force != null ? force : !this._historyOpen;
    const wrap = document.getElementById("tl-history-wrap");
    if (!wrap) return;
    if (this._historyOpen) {
      if (!document.getElementById("tl-history-pop")) {
        const pop = document.createElement("div");
        pop.id = "tl-history-pop";
        pop.className = "tl-history-pop";
        wrap.appendChild(pop);
      }
      this.renderHistoryPanel();
    } else {
      document.getElementById("tl-history-pop")?.remove();
    }
  },

  renderHistoryPanel() {
    if (!this._historyOpen) return;
    const pop = document.getElementById("tl-history-pop");
    if (!pop) return;
    const hist = Editor.history || [];
    if (!hist.length) { pop.innerHTML = `<div class="tl-history-empty">No history yet</div>`; return; }
    // Most recent first; current position highlighted.
    const rows = hist.map((entry, idx) => ({ idx, entry })).reverse();
    pop.innerHTML = `<div class="tl-history-title">Edit history</div><ul class="tl-history-list">`
      + rows.map(({ idx, entry }) => {
        const active = idx === Editor.historyIndex;
        const time = entry.ts
          ? new Date(entry.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
          : "";
        return `<li class="tl-history-item${active ? " active" : ""}" data-hidx="${idx}">
          <span class="tl-history-label">${esc(entry.label || "Edit")}</span>
          <span class="tl-history-time">${esc(time)}</span>
        </li>`;
      }).join("")
      + `</ul>`;
    pop.querySelectorAll(".tl-history-item").forEach((el) => {
      el.onclick = () => Editor.restoreToHistoryIndex(Number(el.dataset.hidx));
    });
  },

  /* ---------- zoom (spec v5 addendum "Zoom-out must fit everything") ----------
     Minimum zoom = the level at which the ENTIRE EDL fills ~80% of the
     visible strip (i.e. ~20% spare room), recomputed every render() from the
     current total duration and the scroll viewport's live width, so it
     tracks both EDL edits (trim/split/delete/reset) and viewport resizes
     (the ResizeObserver in mount() just triggers a render()). The zoom
     slider's `min` attribute is kept in sync, and _setZoom (used by the
     slider, wheel-zoom, +/- keys, and the Fit button) clamps to it — so
     nothing can zoom out further than "everything fits". */
  _fitZoomValue(segs) {
    const list = segs || this._previewSegments || Editor.segments || [];
    const scroll = document.getElementById("timeline-scroll");
    const viewportW = scroll?.clientWidth || 800;
    const total = list.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
    if (total <= 0) return 4;
    return Math.max(1, Math.min((viewportW * 0.8) / total, 220));
  },

  _updateZoomBounds(segs) {
    const min = this._fitZoomValue(segs);
    this._zoomMin = min;
    const zoom = document.getElementById("tl-zoom");
    if (zoom) zoom.min = String(Math.round(min * 100) / 100);
    // Never force pxPerSec up mid-edge-drag: the left-edge anchor math in
    // _onEdgePointerDown fixes anchorRightPx in CURRENT-px terms for the
    // whole gesture, and a trim can transiently shrink total duration enough
    // to raise the fit-minimum — changing pxPerSec underneath that drag would
    // make the anchor pixel stale and the block jump. The slider's min
    // attribute still updates live; the actual pxPerSec clamp is deferred
    // until the drag's pointerup (next render() call after it ends).
    if (this.pxPerSec < min && !this._edgeDragging) {
      this.pxPerSec = min;
      if (zoom) zoom.value = String(Math.round(this.pxPerSec));
    }
  },

  zoomToFit() {
    this._setZoom(this._fitZoomValue());
  },

  _setZoom(v) {
    const min = this._zoomMin ?? this._fitZoomValue();
    this.pxPerSec = Math.max(min, Math.min(220, v));
    const zoom = document.getElementById("tl-zoom");
    if (zoom) zoom.value = String(Math.round(this.pxPerSec));
    this.render();
  },

  splitAtPlayhead() {
    const t = window.EditorUI.player?.currentEdlTime?.() ?? 0;
    const hit = Editor.segmentAtEdlTime(t);
    if (!hit) return;
    Editor.splitAt(hit.index, hit.local);
  },

  /* ---------- I/O in/out-at-playhead (spec v4 §4) ---------- */
  setInOutAtPlayhead(field) {
    const t = window.EditorUI.player?.currentEdlTime?.() ?? 0;
    const hit = Editor.segmentAtEdlTime(t);
    if (!hit) return;
    Editor.trim(hit.index, field, hit.local); // commit() re-selects hit.index for us
  },

  _onKeydown(e) {
    if (state.tab) return; // a drawer (Takes/Reels/Settings/Activity) is open
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || document.activeElement?.isContentEditable) return;
    const meta = e.metaKey || e.ctrlKey;
    const player = window.EditorUI.player;
    if (e.key === " ") { e.preventDefault(); player?.togglePlay(); }
    else if (e.key === "x" || e.key === "X") { e.preventDefault(); this.splitAtPlayhead(); }
    else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      // A selected overlay (spec v5.9b) takes priority — it's the more
      // recently-interacted-with selection, and overlays/segments have
      // fully independent selection state (Editor.overlaySelected vs.
      // Editor.selected) so this never accidentally eats a segment delete.
      if (Editor.overlaySelected) Editor.deleteOverlay(Editor.overlaySelected);
      else Editor.deleteSelected();
    }
    else if (meta && (e.key === "z" || e.key === "Z")) {
      e.preventDefault();
      if (e.shiftKey) Editor.redo(); else Editor.undo();
    } else if (e.key === "+" || e.key === "=") { e.preventDefault(); this._setZoom(this.pxPerSec * 1.2); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); this._setZoom(this.pxPerSec / 1.2); }
    else if (e.key === "s" || e.key === "S") { e.preventDefault(); this._toggleSnapping(); }
    else if (e.key === "j" || e.key === "J") { e.preventDefault(); player?.jump?.(-2); }
    else if (e.key === "k" || e.key === "K") { e.preventDefault(); player?.handleK?.(); }
    else if (e.key === "l" || e.key === "L") { e.preventDefault(); player?.handleL?.(); }
    else if (e.key === "i" || e.key === "I") { e.preventDefault(); this.setInOutAtPlayhead("start"); }
    else if (e.key === "o" || e.key === "O") { e.preventDefault(); this.setInOutAtPlayhead("end"); }
    else if (e.key === "m" || e.key === "M") {
      e.preventDefault();
      const t = player?.currentEdlTime?.() ?? 0;
      const label = prompt("Marker label (optional):", "") || "";
      Editor.addMarker(t, label);
    } else if (e.key === "?") { e.preventDefault(); this._toggleShortcuts(); }
  },

  /* ---------- rendering ---------- */

  render() {
    const segs = this._previewSegments || Editor.segments || [];
    this._updateZoomBounds(segs);
    const px = this.pxPerSec;
    const total = segs.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
    const widthPx = Math.max(total * px, 40);

    const content = document.getElementById("timeline-content");
    if (content) content.style.width = `${widthPx}px`;

    this._renderRuler(total, widthPx);
    this._renderTrack(segs, px);
    this.renderSelection();
    this.renderMarkers();
    this.renderOverlays();
    this.updatePlayhead(window.EditorUI.player?.currentEdlTime?.() ?? 0);
    this.refreshRenderBar();
    refreshIcons();
  },

  _renderRuler(total, widthPx) {
    const ruler = document.getElementById("timeline-ruler");
    if (!ruler) return;
    ruler.style.width = `${widthPx}px`;
    const px = this.pxPerSec;
    const candidates = [0.1, 0.2, 0.5, 1, 2, 5,10, 15, 30, 60, 120, 300, 600, 900];
    let step = candidates[candidates.length - 1];
    for (const c of candidates) { if (c * px >= 70) { step = c; break; } }
    let html = "";
    for (let t = 0; t <= total + step; t += step) {
      html += `<div class="tl-tick" style="left:${(t * px).toFixed(1)}px"><span>${fmtT(t)}</span></div>`;
    }
    ruler.innerHTML = html;
    const markers = document.getElementById("timeline-markers");
    if (markers) markers.style.width = `${widthPx}px`;
  },

  /* ---------- markers (spec v4 §4 "M adds a marker") ---------- */
  renderMarkers() {
    const strip = document.getElementById("timeline-markers");
    if (!strip) return;
    const px = this.pxPerSec;
    strip.innerHTML = (Editor.markers || []).map((m) => {
      const label = m.label ? `${fmtT(m.edl_t)} — ${esc(m.label)}` : fmtT(m.edl_t);
      return `<div class="tl-marker" data-marker="${m.id}" style="left:${(m.edl_t * px).toFixed(1)}px"
        title="${label} (click: seek, ⌥click: remove)"></div>`;
    }).join("");
    strip.querySelectorAll(".tl-marker").forEach((el) => {
      el.onclick = (e) => {
        const id = el.dataset.marker;
        const m = (Editor.markers || []).find((x) => x.id === id);
        if (!m) return;
        if (e.altKey) Editor.removeMarker(id);
        else window.EditorUI.player?.seekToEdlTime?.(m.edl_t);
      };
    });
  },

  /* ---------- manual overlay track (spec v5.9b) ----------
     Thinner blocks in the lane above the main track. All mutations go
     through Editor.overlay* (ui/editor/state.js) exactly like segments go
     through Editor.commit()/trim() — this module never touches
     Editor.overlays directly. Move = drag the block body; trim = drag
     either edge handle (left trims the in-point, right trims duration) —
     same live-preview-then-commit-on-release shape as the main track's
     edge drag below, just without the left-edge-anchor complication (an
     overlay's on-screen position already comes straight from t_start, no
     cumulative-duration layout to fight). */
  renderOverlays() {
    const track = document.getElementById("timeline-overlay-track");
    if (!track) return;
    const px = this.pxPerSec;
    const overlays = Editor.overlays || [];
    track.innerHTML = overlays.map((o) => {
      const leftPx = o.t_start * px;
      const widthPx = Math.max(o.duration * px, 3);
      const clip = Editor.clip(o.clip_id);
      const name = clip?.filename || o.clip_id;
      const selected = o.id === Editor.overlaySelected;
      return `<div class="ov-block${selected ? " selected" : ""}" data-ov="${o.id}"
        style="left:${leftPx.toFixed(1)}px;width:${widthPx.toFixed(1)}px" title="${esc(name)} (overlay)">
        <div class="ov-edge ov-edge-l" data-ov="${o.id}" data-edge="start"></div>
        <span class="ov-label">${esc(name)} · ${fmtT(o.duration)}</span>
        <div class="ov-edge ov-edge-r" data-ov="${o.id}" data-edge="end"></div>
      </div>`;
    }).join("");
    track.querySelectorAll(".ov-block").forEach((el) => {
      el.addEventListener("pointerdown", (e) => this._onOverlayPointerDown(e, el));
    });
  },

  renderOverlaySelection() {
    document.querySelectorAll(".ov-block").forEach((el) => {
      el.classList.toggle("selected", el.dataset.ov === Editor.overlaySelected);
    });
  },

  _onOverlayPointerDown(e, el) {
    if (e.target.classList.contains("ov-edge")) return this._onOverlayEdgePointerDown(e);
    e.stopPropagation(); // don't also trigger the main track's background-click-to-seek
    const id = el.dataset.ov;
    Editor.selectOverlay(id);
    const ov = (Editor.overlays || []).find((o) => o.id === id);
    if (!ov) return;
    const startX = e.clientX;
    const startT = ov.t_start;
    const px = this.pxPerSec;
    let moved = false;
    const onMove = (ev) => {
      const dt = (ev.clientX - startX) / px;
      if (Math.abs(dt) * px > 2) moved = true;
      Editor.overlayMoveLive(id, Math.round((startT + dt) * 1000) / 1000);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (moved) Editor.commitOverlayEdit("Move overlay");
      else this.renderOverlays(); // no-op drag: still worth a clean re-render (selection only)
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  _onOverlayEdgePointerDown(e) {
    e.stopPropagation();
    const edge = e.target;
    const id = edge.dataset.ov;
    const field = edge.dataset.edge; // "start" | "end"
    const ov = (Editor.overlays || []).find((o) => o.id === id);
    if (!ov) return;
    Editor.selectOverlay(id);
    const startX = e.clientX;
    const px = this.pxPerSec;
    const originAbs = field === "start" ? ov.t_start : ov.t_start + ov.duration;
    const onMove = (ev) => {
      const dt = (ev.clientX - startX) / px;
      Editor.overlayTrimLive(id, field, Math.round((originAbs + dt) * 1000) / 1000);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      Editor.commitOverlayEdit(field === "start" ? "Trim overlay start" : "Trim overlay end");
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  /* ---------- render bar (spec v4 §3) ---------- */
  async refreshRenderBar() {
    const bar = document.getElementById("tl-renderbar");
    const label = document.getElementById("tl-renderbar-label");
    if (!bar) return;
    const hasPreview = !!state.project?.preview?.manifest;
    if (!Editor.segments?.length) {
      bar.className = "tl-renderbar unknown";
      if (label) label.textContent = "";
      return;
    }
    const token = (this._renderBarToken = (this._renderBarToken || 0) + 1);
    let stale = true;
    try { stale = await Editor.previewIsStale(); } catch (_e) { stale = true; }
    if (token !== this._renderBarToken) return; // a newer check finished first — drop this one
    bar.className = `tl-renderbar ${stale ? "stale" : "fresh"}`;
    if (label) label.textContent = stale ? (hasPreview ? "outdated — click to render" : "no preview — click to render") : "preview up to date";
  },

  async _enqueuePreviewRender() {
    if (!Editor.pid) return;
    try {
      await api(`/projects/${Editor.pid}/queue`, { method: "POST", body: { kind: "preview_render", payload: {} } });
    } catch (e) {
      alert(`Couldn't queue preview render: ${e.message}`);
    }
  },

  /* ---------- filmstrip + waveform assets (spec v4 §4) ---------- */
  _getThumbEntry(clipId) {
    let e = this._thumbCache.get(clipId);
    if (!e) {
      e = { meta: null, peaks: null, stripUrl: null, metaFailed: false, loading: false };
      this._thumbCache.set(clipId, e);
      this._loadThumbs(clipId, e);
    }
    return e;
  },
  async _loadThumbs(clipId, entry) {
    entry.loading = true;
    const pid = Editor.pid;
    try {
      entry.meta = await api(`/projects/${pid}/thumbs/${clipId}/meta`);
      entry.stripUrl = `/api/projects/${pid}/thumbs/${clipId}/strip`;
    } catch (_e) {
      entry.metaFailed = true; // 404: no video track / not generated yet — plain block
    }
    try {
      entry.peaks = await api(`/projects/${pid}/thumbs/${clipId}/peaks`);
    } catch (_e) {
      entry.peaks = [];
    }
    entry.loading = false;
    if (Editor.pid === pid) this.render(); // reflow once — subsequent renders hit the cache
  },

  _drawWaveform(canvas, peaks, clipDuration, segStart, segEnd) {
    if (!canvas || !peaks?.length || !clipDuration) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const n = peaks.length;
    const iStart = Math.max(0, Math.floor((segStart / clipDuration) * n));
    const iEnd = Math.min(n, Math.max(iStart + 1, Math.ceil((segEnd / clipDuration) * n)));
    const count = iEnd - iStart;
    if (count <= 0) return;
    ctx.fillStyle = "rgba(154,163,178,.55)";
    const mid = h / 2;
    for (let x = 0; x < w; x++) {
      const idx = iStart + Math.floor((x / w) * count);
      const peak = Math.min(1, peaks[Math.min(n - 1, idx)] || 0);
      const barH = Math.max(1, peak * (h - 2));
      ctx.fillRect(x, mid - barH / 2, 1, barH);
    }
  },

  _renderTrack(segs, px) {
    const track = document.getElementById("timeline-track");
    if (!track) return;
    let t = 0;
    let html = "";
    segs.forEach((s, i) => {
      const dur = Math.max(0, s.end - s.start);
      let leftPx = t * px;
      const widthPx = Math.max(dur * px, 3);
      if (this._dragLeftAnchor && this._dragLeftAnchor.index === i) {
        leftPx = this._dragLeftAnchor.anchorRightPx - widthPx;
      }
      const clip = Editor.clip(s.clip_id);
      const name = clip?.filename || s.clip_id;
      const trType = s.transition?.type || "none";

      const thumbs = this._getThumbEntry(s.clip_id);
      let filmHtml = "";
      if (thumbs.meta && thumbs.stripUrl) {
        const bgW = Math.max(1, thumbs.meta.count * thumbs.meta.interval_s * px);
        const bgX = -(s.start * px);
        filmHtml = `<div class="tl-film" style="background-image:url('${thumbs.stripUrl}');`
          + `background-repeat:no-repeat;background-size:${bgW.toFixed(1)}px 100%;`
          + `background-position:${bgX.toFixed(1)}px 0"></div>`;
      }
      const waveHtml = thumbs.peaks?.length
        ? `<canvas class="tl-wave" data-wave="${i}" width="${Math.max(1, Math.round(widthPx))}" height="16"></canvas>`
        : "";

      html += `<div class="tl-chip ${trType}" data-chip="${i}" style="left:${leftPx.toFixed(1)}px"
        title="Transition into this clip — click to cycle">${trType === "none" ? "·" : trType === "fade" ? "F" : "X"}</div>
      <div class="tl-block" data-idx="${i}" style="left:${leftPx.toFixed(1)}px;width:${widthPx.toFixed(1)}px">
        ${filmHtml}
        <div class="tl-edge tl-edge-l" data-idx="${i}" data-edge="start"></div>
        <span class="tl-label">${esc(name)} · ${fmtT(dur)}</span>
        <div class="tl-edge tl-edge-r" data-idx="${i}" data-edge="end"></div>
        ${waveHtml}
      </div>`;
      t += dur;
    });
    track.innerHTML = html;

    track.querySelectorAll(".tl-block").forEach((el) => {
      el.addEventListener("pointerdown", (e) => this._onBlockPointerDown(e, el));
    });
    track.querySelectorAll(".tl-chip").forEach((el) => {
      el.onclick = (e) => {
        e.stopPropagation();
        const i = Number(el.dataset.chip);
        const cur = Editor.segments[i]?.transition?.type || "none";
        const next = cur === "none" ? "fade" : cur === "fade" ? "crossfade" : "none";
        Editor.setTransition(i, next);
      };
    });
    track.querySelectorAll(".tl-wave").forEach((canvas) => {
      const i = Number(canvas.dataset.wave);
      const s = segs[i];
      const thumbs = this._thumbCache.get(s.clip_id);
      const clipDur = Editor.clipDuration(s.clip_id);
      this._drawWaveform(canvas, thumbs?.peaks, clipDur, s.start, s.end);
    });
  },

  renderSelection() {
    document.querySelectorAll(".tl-block").forEach((el) => {
      el.classList.toggle("selected", Number(el.dataset.idx) === Editor.selected);
    });
  },

  updatePlayhead(edlTime) {
    const ph = document.getElementById("timeline-playhead");
    if (!ph) return;
    const x = Math.max(0, edlTime) * this.pxPerSec;
    ph.style.left = `${x}px`;
    const scroll = document.getElementById("timeline-scroll");
    if (scroll) {
      const rect = scroll.getBoundingClientRect();
      if (x < scroll.scrollLeft || x > scroll.scrollLeft + rect.width - 20) {
        scroll.scrollLeft = Math.max(0, x - rect.width / 3);
      }
    }
  },

  /* ---------- background click/drag = seek ---------- */

  _onBackgroundPointerDown(e) {
    if (e.target.closest(".tl-block") || e.target.closest(".tl-chip") || e.target.closest(".tl-marker")) return;
    const content = document.getElementById("timeline-content");
    const seekFromEvent = (ev) => {
      const rect = content.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      window.EditorUI.player?.seekToEdlTime(Math.max(0, x / this.pxPerSec));
    };
    seekFromEvent(e);
    const onMove = (ev) => seekFromEvent(ev);
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  /* ---------- media bin drag-drop = insert full clip (spec v4 §4) ---------- */

  _dropIndexForX(trackX) {
    const segs = Editor.segments || [];
    const px = this.pxPerSec;
    let t = 0;
    for (let k = 0; k < segs.length; k++) {
      const dur = Math.max(0, segs[k].end - segs[k].start);
      if (trackX < t + dur / 2) return k;
      t += dur;
    }
    return segs.length;
  },

  _onBinDragOver(e) {
    if (!e.dataTransfer?.types?.includes("application/x-mve-clip")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    document.getElementById("timeline-content")?.classList.add("tl-drop-target");
  },
  _onBinDragLeave() {
    document.getElementById("timeline-content")?.classList.remove("tl-drop-target");
  },
  _onBinDrop(e) {
    document.getElementById("timeline-content")?.classList.remove("tl-drop-target");
    const clipId = e.dataTransfer?.getData("application/x-mve-clip");
    if (!clipId) return;
    e.preventDefault();
    const content = document.getElementById("timeline-content");
    const rect = content.getBoundingClientRect();
    const x = e.clientX - rect.left + content.scrollLeft;
    const idx = this._dropIndexForX(x);
    Editor.insertClip(clipId, idx);
  },

  /* ---------- block drag = reorder ---------- */

  _onBlockPointerDown(e, el) {
    if (e.target.classList.contains("tl-edge")) return this._onEdgePointerDown(e, el);
    e.stopPropagation();
    const startIndex = Number(el.dataset.idx);
    const track = document.getElementById("timeline-track");
    const trackRect = track.getBoundingClientRect();
    el.classList.add("dragging");
    let targetIndex = startIndex;

    const onMove = (ev) => {
      const trackX = ev.clientX - trackRect.left;
      targetIndex = this._targetIndexForX(startIndex, trackX);
    };
    const onUp = () => {
      el.classList.remove("dragging");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (targetIndex !== startIndex) Editor.reorder(startIndex, targetIndex);
      else Editor.select(startIndex);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },

  _targetIndexForX(dragIndex, trackX) {
    const segs = Editor.segments;
    const px = this.pxPerSec;
    let t = 0;
    for (let k = 0; k < segs.length; k++) {
      if (k === dragIndex) continue;
      const dur = Math.max(0, segs[k].end - segs[k].start);
      const midPx = t * px + (dur * px) / 2;
      if (trackX < midPx) return k - (dragIndex < k ? 1 : 0);
      t += dur;
    }
    return segs.length - 1;
  },

  /* ---------- edge drag = trim (live preview, commit on release) ----------
     Two things beyond the plain trim math:
     - LEFT-edge anchor: dragging a block's start must visually extend/
       shrink from the block's right edge (which stays put on screen) —
       naturally, layout anchors every block's left edge on the cumulative
       duration of its predecessors, so a start-trim would otherwise appear
       to grow/shrink the block from its END. _dragLeftAnchor overrides just
       this block's rendered leftPx for the duration of the drag.
     - Snapping: when `this.snapping`, the dragged edge's on-screen position
       snaps to the playhead or to any OTHER segment's boundary within 8px. */

  _onEdgePointerDown(e, blockEl) {
    e.stopPropagation();
    const edge = e.target;
    const i = Number(edge.dataset.idx);
    const field = edge.dataset.edge;
    const original = Editor.segments[i];
    if (!original) return;
    const startX = e.clientX;
    const px = this.pxPerSec;
    const clipDur = Editor.clipDuration(original.clip_id);

    const cum = Editor.cumulative();
    const row = cum[i];
    const anchorRightPx = row.end * px;
    const anchorLeftPx = row.start * px;
    this._dragLeftAnchor = field === "start" ? { index: i, anchorRightPx } : null;
    this._edgeDragging = true;

    const SNAP_PX = 8;
    const snapPx = (rawPx) => {
      if (!this.snapping) return rawPx;
      const candidates = [(window.EditorUI.player?.currentEdlTime?.() ?? 0) * px];
      cum.forEach((r, k) => { if (k !== i) candidates.push(r.start * px, r.end * px); });
      let best = rawPx, bestD = SNAP_PX;
      for (const c of candidates) {
        const d = Math.abs(rawPx - c);
        if (d < bestD) { bestD = d; best = c; }
      }
      return best;
    };

    const computeValue = (clientX) => {
      const deltaSec = (clientX - startX) / px;
      let v = Math.round((original[field] + deltaSec) / 0.05) * 0.05;
      v = Math.max(0, Math.min(v, clipDur));
      if (field === "start") v = Math.min(v, original.end - 0.1);
      else v = Math.max(v, original.start + 0.1);

      if (field === "start") {
        const widthPx = Math.max((original.end - v) * px, 3);
        const rawLeftPx = anchorRightPx - widthPx;
        const snapped = snapPx(rawLeftPx);
        if (snapped !== rawLeftPx) v = original.end - (anchorRightPx - snapped) / px;
      } else {
        const rawRightPx = anchorLeftPx + (v - original.start) * px;
        const snapped = snapPx(rawRightPx);
        if (snapped !== rawRightPx) v = original.start + (snapped - anchorLeftPx) / px;
      }
      v = Math.round(v / 0.05) * 0.05;
      v = Math.max(0, Math.min(v, clipDur));
      if (field === "start") v = Math.min(v, original.end - 0.1);
      else v = Math.max(v, original.start + 0.1);
      return Math.round(v * 1000) / 1000;
    };

    const onMove = (ev) => {
      const v = computeValue(ev.clientX);
      this._lastTrimValue = v;
      const preview = _cloneForPreview(Editor.segments);
      preview[i][field] = v;
      this._previewSegments = preview;
      this.render();
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      this._dragLeftAnchor = null;
      this._edgeDragging = false;
      this._previewSegments = null;
      if (this._lastTrimValue != null) Editor.trim(i, field, this._lastTrimValue);
      this._lastTrimValue = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },
};

function _cloneForPreview(segs) {
  return segs.map((s) => ({ ...s, transition: { ...(s.transition || { type: "none", duration: 0.5 }) } }));
}

window.EditorUI.timeline = Timeline;
