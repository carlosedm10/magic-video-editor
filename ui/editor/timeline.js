/* Bottom timeline: adaptive ruler, zoom (slider + wheel), one video track of
   blocks (width ∝ duration), selection highlight, playhead synced both ways
   with the player, drag blocks to reorder, drag block edges to trim (live
   feedback, 0.05s snap), per-junction transition chips, toolbar (split at
   playhead / delete / undo / redo / reset / save). All mutations go through
   `Editor` (ui/editor/state.js), which owns history + autosave. */

window.EditorUI = window.EditorUI || {};

const Timeline = {
  pxPerSec: 40,
  _drag: null,       // active drag/trim operation, or null
  _wired: false,

  mount() {
    this.render();
    if (this._wired) return;
    this._wired = true;

    const zoom = document.getElementById("tl-zoom");
    if (zoom) {
      zoom.value = String(this.pxPerSec);
      zoom.oninput = () => { this.pxPerSec = Number(zoom.value); this.render(); };
    }
    const scroll = document.getElementById("timeline-scroll");
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

    document.addEventListener("keydown", (e) => this._onKeydown(e));
  },

  _setZoom(v) {
    this.pxPerSec = Math.max(4, Math.min(220, v));
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

  _onKeydown(e) {
    if (state.tab) return; // a drawer (Takes/Reels/Settings/Activity) is open
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || document.activeElement?.isContentEditable) return;
    const meta = e.metaKey || e.ctrlKey;
    if (e.key === " ") { e.preventDefault(); window.EditorUI.player?.togglePlay(); }
    else if (e.key === "s" || e.key === "S") { e.preventDefault(); this.splitAtPlayhead(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); Editor.deleteSelected(); }
    else if (meta && (e.key === "z" || e.key === "Z")) {
      e.preventDefault();
      if (e.shiftKey) Editor.redo(); else Editor.undo();
    } else if (e.key === "+" || e.key === "=") { e.preventDefault(); this._setZoom(this.pxPerSec * 1.2); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); this._setZoom(this.pxPerSec / 1.2); }
  },

  /* ---------- rendering ---------- */

  render() {
    const segs = this._previewSegments || Editor.segments || [];
    const px = this.pxPerSec;
    const total = segs.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
    const widthPx = Math.max(total * px, 40);

    const content = document.getElementById("timeline-content");
    if (content) content.style.width = `${widthPx}px`;

    this._renderRuler(total, widthPx);
    this._renderTrack(segs, px);
    this.renderSelection();
    this.updatePlayhead(window.EditorUI.player?.currentEdlTime?.() ?? 0);
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
  },

  _renderTrack(segs, px) {
    const track = document.getElementById("timeline-track");
    if (!track) return;
    let t = 0;
    let html = "";
    segs.forEach((s, i) => {
      const dur = Math.max(0, s.end - s.start);
      const leftPx = t * px;
      const widthPx = Math.max(dur * px, 3);
      const clip = Editor.clip(s.clip_id);
      const name = clip?.filename || s.clip_id;
      const trType = s.transition?.type || "none";
      html += `<div class="tl-chip ${trType}" data-chip="${i}" style="left:${leftPx.toFixed(1)}px"
        title="Transition into this clip — click to cycle">${trType === "none" ? "·" : trType === "fade" ? "F" : "X"}</div>
      <div class="tl-block" data-idx="${i}" style="left:${leftPx.toFixed(1)}px;width:${widthPx.toFixed(1)}px">
        <div class="tl-edge tl-edge-l" data-idx="${i}" data-edge="start"></div>
        <span class="tl-label">${esc(name)} · ${fmtT(dur)}</span>
        <div class="tl-edge tl-edge-r" data-idx="${i}" data-edge="end"></div>
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
    if (e.target.closest(".tl-block") || e.target.closest(".tl-chip")) return;
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

  /* ---------- edge drag = trim (live preview, commit on release) ---------- */

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

    const onMove = (ev) => {
      const deltaSec = (ev.clientX - startX) / px;
      let v = Math.round((original[field] + deltaSec) / 0.05) * 0.05;
      v = Math.max(0, Math.min(v, clipDur));
      if (field === "start") v = Math.min(v, original.end - 0.1);
      else v = Math.max(v, original.start + 0.1);
      const preview = _cloneForPreview(Editor.segments);
      preview[i][field] = Math.round(v * 1000) / 1000;
      this._previewSegments = preview;
      this.render();
    };
    const onUp = (ev) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      const deltaSec = (ev.clientX - startX) / px;
      this._previewSegments = null;
      Editor.trim(i, field, original[field] + deltaSec);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  },
};

function _cloneForPreview(segs) {
  return segs.map((s) => ({ ...s, transition: { ...(s.transition || { type: "none", duration: 0.5 }) } }));
}

window.EditorUI.timeline = Timeline;
