/* Editor core: EDL segment state, undo/redo history (depth 50), selection,
   and debounced autosave. Everything else in ui/editor/*.js reads/mutates
   the timeline strictly through this `Editor` object and registers its own
   render entry points onto window.EditorUI; this file wires those entry
   points to core.js's project lifecycle (onProjectSelected/onProjectRefreshed)
   so core.js never needs to know the editor's internals.

   Server contract: GET/PUT /api/projects/{pid}/edl, POST .../edl/reset,
   POST .../edl/split (magic_video_editor/api/edl.py). Each segment may carry a
   `transition` {type: none|fade|crossfade, duration} — the transition INTO
   that segment (docs/PLATFORM-SPEC.md "Transitions (junction-level)"). */

window.EditorUI = window.EditorUI || {};

/* History is capped at 30 LABELED entries (spec v5 addendum "Undo history
   panel"): each entry is a full {label, ts, segments, markers} snapshot (not
   just segments) so restoring an arbitrary entry from the panel — not just
   linear undo/redo — puts markers back in sync too. historyIndex is the
   currently-active entry; the panel highlights it and clicking any other
   entry jumps straight there (still funnels through the same restore path
   Cmd+Z/Shift+Cmd+Z use, so the panel and the keyboard shortcuts can never
   drift out of sync). */
const HISTORY_DEPTH = 30;

function _withClientId(s) {
  return { ...s, _id: `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}` };
}
function _cloneSegments(segs) {
  return segs.map((s) => ({ ...s, transition: { ...(s.transition || { type: "none", duration: 0.5 }) } }));
}
function _setSaveState(text, isError) {
  const el = document.getElementById("tl-save-state");
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? "var(--danger)" : "";
}

/* ---------- manifest hash (spec v4 §3 render bar / preview-match) ----------
   Mirrors magic_video_editor/pipeline/render.py's _preview_manifest: sha256 hex[:16] of
   json.dumps({edl, color, subtitles, audio_enhance}, sort_keys=True,
   default=str). Two things a generic JSON.stringify would get wrong here:
   (1) Python's default `json.dumps` separators are ", " and ": " (a space
   after each), NOT JSON.stringify's compact ",%":"; get this wrong and every
   hash mismatches even when the data is identical — verified byte-for-byte
   against magic_video_editor.pipeline.render._preview_manifest while building this.
   (2) every numeric field in this payload that's declared Python `float`
   (edl start/end/transition.duration, all four color sliders) serializes
   with a trailing ".0" when integer-valued, vs plain ints for the one true
   int field (subtitles.words_per_cue) — JS has one number type, so this
   encodes the known shape field-by-field rather than walking it generically. */
function _pyFloat(v) {
  if (v === null || v === undefined) return "null";
  const n = Number(v);
  if (!Number.isFinite(n)) return "null";
  return Number.isInteger(n) ? `${n}.0` : String(n);
}
function _pyInt(v) {
  if (v === null || v === undefined) return "null";
  return String(Math.trunc(Number(v)));
}
function _pyStr(v) {
  return v === null || v === undefined ? "null" : JSON.stringify(String(v));
}
function _pyBool(v) {
  return v === null || v === undefined ? "null" : (v ? "true" : "false");
}
function _pyEdlJson(edl) {
  if (!edl) return "null";
  return "[" + edl.map((s) => {
    const tr = s.transition || { type: "none", duration: 0.5 };
    return "{"
      + `"clip_id": ${_pyStr(s.clip_id)}, `
      + `"end": ${_pyFloat(s.end)}, `
      + `"start": ${_pyFloat(s.start)}, `
      + `"text": ${_pyStr(s.text || "")}, `
      + `"transition": {"duration": ${_pyFloat(tr.duration)}, "type": ${_pyStr(tr.type)}}`
      + "}";
  }).join(", ") + "]";
}
function _pyColorJson(c) {
  if (!c) return "null";
  return "{"
    + `"brightness": ${_pyFloat(c.brightness)}, `
    + `"contrast": ${_pyFloat(c.contrast)}, `
    + `"preset": ${_pyStr(c.preset)}, `
    + `"saturation": ${_pyFloat(c.saturation)}, `
    + `"temperature": ${_pyFloat(c.temperature)}`
    + "}";
}
function _pySubtitlesJson(s) {
  if (!s) return "null";
  return "{"
    + `"color": ${_pyStr(s.color)}, `
    + `"enabled": ${_pyBool(s.enabled)}, `
    + `"font": ${_pyStr(s.font)}, `
    + `"outline_color": ${_pyStr(s.outline_color)}, `
    + `"position": ${_pyStr(s.position)}, `
    + `"size": ${_pyStr(s.size)}, `
    + `"style": ${_pyStr(s.style)}, `
    + `"words_per_cue": ${_pyInt(s.words_per_cue)}`
    + "}";
}
function _manifestJson(payload) {
  return "{"
    + `"audio_enhance": ${_pyBool(payload.audio_enhance)}, `
    + `"color": ${_pyColorJson(payload.color)}, `
    + `"edl": ${_pyEdlJson(payload.edl)}, `
    + `"subtitles": ${_pySubtitlesJson(payload.subtitles)}`
    + "}";
}
async function _sha256Hex16(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

const Editor = {
  pid: null,
  segments: null,      // array of {_id, clip_id, start, end, text, transition}
  selected: 0,
  history: [],
  historyIndex: -1,
  dirty: false,
  saveTimer: null,

  clip(cid) {
    return (state.project?.clips || []).find((c) => c.id === cid);
  },
  clipDuration(cid) {
    return this.clip(cid)?.info?.duration ?? Infinity;
  },

  async load(pid) {
    const { segments } = await api(`/projects/${pid}/edl`);
    this.pid = pid;
    this.segments = segments.map(_withClientId);
    this.selected = 0;
    this._loadMarkers();
    this.history = [];
    this.historyIndex = -1;
    this._pushHistory("Opened project");
    this.dirty = false;
    _setSaveState("");
    await this._loadOverlays();
    this._notify();
  },

  /* ---------- manual overlay track (spec v5.9b) ----------
     project["overlays"] — a second timeline track for video-over-video/PiP,
     STRICTLY MANUAL: only magic_video_editor/api/overlays.py's GET/PUT ever touch it
     server-side (the AI pipeline never creates/edits overlay items), and on
     the client only the methods below mutate `this.overlays` — timeline.js's
     overlay track and overlaybox.js read/act through these, never touching
     the array directly, mirroring how segments only ever change through
     commit()/trim()/etc. above.

     History is DELIBERATELY separate from the main EDL undo stack (spec:
     "never through EDL history — separate small undo stack ok") — a smaller
     20-entry stack (_ovHistory/_ovHistoryIndex) that only overlay edits push
     to; Cmd+Z/redo for the main timeline never touches it and vice versa
     (there's no shared keyboard binding for the overlay stack yet — future
     UI can call undoOverlays()/redoOverlays() directly, e.g. from a chip in
     overlaybox.js's floating controls).

     Save is a debounced PUT of the WHOLE list (the endpoint replaces
     project["overlays"] wholesale, no partial-update route) — same
     _scheduleSave-style pattern as the EDL, just on its own timer so an
     overlay edit never resets the EDL's 2s save countdown or vice versa. */
  overlays: [],
  overlaySelected: null,
  _ovHistory: [],
  _ovHistoryIndex: -1,
  _ovSaveTimer: null,

  async _loadOverlays() {
    this.overlays = [];
    this.overlaySelected = null;
    this._ovHistory = [];
    this._ovHistoryIndex = -1;
    try {
      const { overlays } = await api(`/projects/${this.pid}/overlays`);
      this.overlays = (overlays || []).map((o) => ({ ...o }));
    } catch (e) {
      console.error("Failed to load overlays", e); // e.g. router not mounted yet server-side
      this.overlays = [];
    }
    this._pushOverlayHistory("Loaded overlays");
  },

  overlayClipDuration(clipId) { return this.clipDuration(clipId); },

  _clampOverlay(ov) {
    const clipDur = this.clipDuration(ov.clip_id);
    const cutDur = this.totalDuration();
    ov.duration = Math.max(0.2, ov.duration);
    if (Number.isFinite(clipDur)) {
      ov.clip_in = Math.max(0, Math.min(ov.clip_in, Math.max(0, clipDur - 0.2)));
      ov.duration = Math.min(ov.duration, Math.max(0.2, clipDur - ov.clip_in));
    }
    ov.t_start = Math.max(0, ov.t_start);
    if (cutDur > 0) {
      ov.t_start = Math.min(ov.t_start, Math.max(0, cutDur - ov.duration));
      ov.duration = Math.min(ov.duration, Math.max(0.2, cutDur - ov.t_start));
    }
    ov.x = Math.max(0, Math.min(1, ov.x));
    ov.y = Math.max(0, Math.min(1, ov.y));
    ov.scale = Math.max(0.02, Math.min(1, ov.scale));
    ov.opacity = Math.max(0, Math.min(1, ov.opacity));
    return ov;
  },

  /* Adds a full-clip overlay dropped from the media bin at timeline second
     `tStart` (spec: "created by dragging a clip from the media bin onto the
     overlay track"). Client-generated id (non-empty) survives the PUT
     round-trip unchanged (overlays.py only mints a server id when the
     client sent none), so selection stays stable through the debounced save. */
  insertOverlay(clipId, tStart) {
    const clip = this.clip(clipId);
    const dur = clip?.info?.duration;
    if (!dur || dur <= 0) return null;
    const cutDur = this.totalDuration();
    let duration = Math.min(3, dur);
    let t = Math.max(0, tStart);
    if (cutDur > 0) {
      t = Math.min(t, Math.max(0, cutDur - duration));
      duration = Math.min(duration, Math.max(0.2, cutDur - t));
    }
    const id = `ov${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    const ov = {
      id, clip_id: clipId, t_start: Math.round(t * 100) / 100,
      duration: Math.round(duration * 100) / 100, clip_in: 0,
      x: 0.35, y: 0.35, scale: 0.3, opacity: 1,
    };
    const list = [...this.overlays.map((o) => ({ ...o })), ov];
    this.overlaySelected = id;
    this.commitOverlays(list, { label: `Add overlay ${clip?.filename || clipId}` });
    return id;
  },

  deleteOverlay(id) {
    const list = this.overlays.filter((o) => o.id !== id).map((o) => ({ ...o }));
    if (this.overlaySelected === id) this.overlaySelected = null;
    this.commitOverlays(list, { label: "Delete overlay" });
  },

  selectOverlay(id) { this.overlaySelected = id; this._notifyOverlaySelection(); },
  deselectOverlay() { this.overlaySelected = null; this._notifyOverlaySelection(); },

  /* ---------- live-drag updates (no history push / no save — mirrors
     timeline.js's _previewSegments pattern for edge/block drags): the
     dragging module (timeline.js overlay track, overlaybox.js) calls these
     on every pointermove for immediate visual feedback, then calls
     commitOverlayEdit() once on pointerup to snapshot+push history+save. ---------- */
  overlayMoveLive(id, tStart) {
    const ov = this.overlays.find((o) => o.id === id);
    if (!ov) return;
    ov.t_start = Math.max(0, tStart);
    const cutDur = this.totalDuration();
    if (cutDur > 0) ov.t_start = Math.min(ov.t_start, Math.max(0, cutDur - ov.duration));
    this._notifyOverlays();
  },
  /* edge: "start" trims the IN point (t_start & clip_in move together,
     duration shrinks/grows inversely, the overlay's END stays put); "end"
     only changes duration. `absTime` is the proposed new absolute timeline
     second for that edge (new t_start for "start", new t_start+duration for
     "end") — same convention timeline.js already uses for EDL edge drags. */
  overlayTrimLive(id, edge, absTime) {
    const ov = this.overlays.find((o) => o.id === id);
    if (!ov) return;
    if (edge === "start") {
      let dt = absTime - ov.t_start;
      dt = Math.min(dt, ov.duration - 0.2);
      dt = Math.max(dt, -ov.t_start, -ov.clip_in);
      ov.t_start += dt; ov.clip_in += dt; ov.duration -= dt;
    } else {
      let newDuration = Math.max(0.2, absTime - ov.t_start);
      const clipDur = this.clipDuration(ov.clip_id);
      if (Number.isFinite(clipDur)) newDuration = Math.min(newDuration, Math.max(0.2, clipDur - ov.clip_in));
      const cutDur = this.totalDuration();
      if (cutDur > 0) newDuration = Math.min(newDuration, Math.max(0.2, cutDur - ov.t_start));
      ov.duration = newDuration;
    }
    this._notifyOverlays();
  },
  /* Player-stage bounding box (overlaybox.js): move = x/y, corners = scale
     (+the anchor-corner x/y shift that comes with resizing from a corner). */
  overlayTransformLive(id, patch) {
    const ov = this.overlays.find((o) => o.id === id);
    if (!ov) return;
    Object.assign(ov, patch);
    ov.x = Math.max(0, Math.min(1, ov.x));
    ov.y = Math.max(0, Math.min(1, ov.y));
    ov.scale = Math.max(0.02, Math.min(1, ov.scale));
    this._notifyOverlays();
  },
  overlayOpacity(id, value) {
    const list = this.overlays.map((o) => ({ ...o }));
    const ov = list.find((o) => o.id === id);
    if (!ov) return;
    ov.opacity = Math.max(0, Math.min(1, value));
    this.commitOverlays(list, { label: "Overlay opacity" });
  },

  /* Finalizes whatever a live-drag left in `this.overlays`: re-clamps every
     field (belt-and-braces — the live methods above already clamp, but this
     is also the entry point for anything that mutates the array directly),
     pushes one history entry, and schedules the debounced save. */
  commitOverlayEdit(label) {
    this.overlays = this.overlays.map((o) => this._clampOverlay({ ...o }));
    this._pushOverlayHistory(label);
    this._scheduleOverlaySave();
    this._notifyOverlays();
  },

  commitOverlays(list, { label } = {}) {
    this.overlays = list.map((o) => this._clampOverlay({ ...o }));
    if (this.overlaySelected && !this.overlays.some((o) => o.id === this.overlaySelected)) this.overlaySelected = null;
    this._pushOverlayHistory(label);
    this._scheduleOverlaySave();
    this._notifyOverlays();
  },

  _pushOverlayHistory(label) {
    const idx = this._ovHistoryIndex ?? -1;
    this._ovHistory = (this._ovHistory || []).slice(0, idx + 1);
    this._ovHistory.push({ label: label || "Overlay edit", overlays: this.overlays.map((o) => ({ ...o })) });
    if (this._ovHistory.length > 20) this._ovHistory.shift();
    this._ovHistoryIndex = this._ovHistory.length - 1;
  },
  undoOverlays() {
    if ((this._ovHistoryIndex ?? -1) <= 0) return;
    this._ovHistoryIndex--;
    this.overlays = (this._ovHistory[this._ovHistoryIndex].overlays || []).map((o) => ({ ...o }));
    this._scheduleOverlaySave();
    this._notifyOverlays();
  },
  redoOverlays() {
    if (!this._ovHistory || this._ovHistoryIndex >= this._ovHistory.length - 1) return;
    this._ovHistoryIndex++;
    this.overlays = (this._ovHistory[this._ovHistoryIndex].overlays || []).map((o) => ({ ...o }));
    this._scheduleOverlaySave();
    this._notifyOverlays();
  },

  _scheduleOverlaySave() {
    clearTimeout(this._ovSaveTimer);
    this._ovSaveTimer = setTimeout(() => this.saveOverlays(), 600);
  },
  async saveOverlays() {
    if (!this.pid) return;
    clearTimeout(this._ovSaveTimer);
    try {
      const body = { overlays: this.overlays.map((o) => ({ ...o })) };
      const res = await api(`/projects/${this.pid}/overlays`, { method: "PUT", body });
      this.overlays = (res.overlays || []).map((o) => ({ ...o }));
      this._notifyOverlays();
    } catch (e) {
      console.error("Overlay save failed", e); // fail-soft: local edits stay visible, just unsaved
    }
  },

  _notifyOverlays() {
    [
      () => window.EditorUI.timeline?.renderOverlays?.(),
      () => window.EditorUI.overlaybox?.render?.(),
    ].forEach((fn) => { try { fn(); } catch (e) { console.error("EditorUI overlay render error", e); } });
  },
  _notifyOverlaySelection() {
    [
      () => window.EditorUI.timeline?.renderOverlaySelection?.(),
      () => window.EditorUI.overlaybox?.render?.(),
    ].forEach((fn) => { try { fn(); } catch (e) { console.error("EditorUI overlay selection error", e); } });
  },

  /* ---------- markers (spec v4 §4 — "M adds a marker") ----------
     No backend field fits (PUT /edl only accepts `segments`, and there's no
     generic project PATCH endpoint) — kept client-side in localStorage per
     project, per the brief's documented fallback. Integrator: a real
     project["markers"] field + small PATCH endpoint would let these survive
     a different machine/browser and show up for other viewers. */
  markers: [],
  _markersKey() { return `mve.markers.${this.pid}`; },
  _loadMarkers() {
    try { this.markers = JSON.parse(localStorage.getItem(this._markersKey()) || "[]"); }
    catch (_e) { this.markers = []; }
  },
  _saveMarkers() {
    try { localStorage.setItem(this._markersKey(), JSON.stringify(this.markers)); }
    catch (_e) { /* storage full/unavailable — markers still work for this session */ }
  },
  addMarker(edlTime, label) {
    this.markers.push({
      id: `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
      edl_t: Math.round(Math.max(0, edlTime) * 100) / 100,
      label: label || "",
    });
    this.markers.sort((a, b) => a.edl_t - b.edl_t);
    this._saveMarkers();
    this._pushHistory(label ? `Marker: ${label}` : "Marker");
    try { window.EditorUI.timeline?.renderMarkers?.(); } catch (e) { console.error(e); }
  },
  removeMarker(id) {
    this.markers = this.markers.filter((m) => m.id !== id);
    this._saveMarkers();
    this._pushHistory("Remove marker");
    try { window.EditorUI.timeline?.renderMarkers?.(); } catch (e) { console.error(e); }
  },

  /* ---------- media-bin drag-drop (spec v4 §4 — "clips draggable onto the
     timeline") ---------- */
  insertClip(clipId, atIndex) {
    const clip = this.clip(clipId);
    const dur = clip?.info?.duration;
    if (!dur || dur <= 0) return;
    const seg = _withClientId({
      clip_id: clipId, start: 0, end: dur, text: "",
      transition: { type: "none", duration: 0.5 },
    });
    const segs = _cloneSegments(this.segments || []);
    const idx = Math.max(0, Math.min(atIndex, segs.length));
    segs.splice(idx, 0, seg);
    const name = clip?.filename || clipId;
    this.commit(segs, { select: idx, label: `Insert ${name}` });
  },

  /* ---------- preview-render staleness (spec v4 §3 render bar) ----------
     Uses the LIVE (possibly-unsaved) edit state for the edl part — if you're
     mid-edit the backend doesn't know about it yet regardless of hash, so
     that's the more correct "stale" signal than re-fetching project.edl. */
  async computeManifestHash() {
    const payload = {
      edl: (this.segments || []).map((s) => ({
        clip_id: s.clip_id, start: s.start, end: s.end, text: s.text || "",
        transition: s.transition || { type: "none", duration: 0.5 },
      })),
      color: state.project?.color ?? null,
      subtitles: state.project?.subtitles ?? null,
      audio_enhance: state.project?.audio_enhance ?? null,
    };
    return _sha256Hex16(_manifestJson(payload));
  },
  async previewIsStale() {
    const preview = state.project?.preview;
    if (!preview?.manifest) return true;
    try {
      const hash = await this.computeManifestHash();
      return hash !== preview.manifest;
    } catch (_e) {
      return true; // crypto.subtle unavailable or similar — default to "stale"
    }
  },

  /* Pushes a new labeled snapshot as the current position, dropping any redo
     tail (standard undo-stack semantics) and capping to HISTORY_DEPTH oldest-
     first. Used both by commit() (EDL-affecting edits) and directly by the
     marker functions above (markers aren't part of `segments`/the EDL, but
     the spec's example label list includes "Marker", so they get an entry
     too — with a full snapshot so restoring it is still correct). */
  _pushHistory(label) {
    this.history = this.history.slice(0, this.historyIndex + 1);
    this.history.push({
      label: label || "Edit",
      ts: Date.now(),
      segments: _cloneSegments(this.segments || []),
      markers: (this.markers || []).map((m) => ({ ...m })),
    });
    if (this.history.length > HISTORY_DEPTH) this.history.shift();
    this.historyIndex = this.history.length - 1;
    this._notifyHistory();
  },

  commit(newSegments, { select, label } = {}) {
    this.segments = newSegments;
    if (select != null) this.selected = select;
    if (this.selected >= this.segments.length) this.selected = Math.max(0, this.segments.length - 1);
    this._pushHistory(label);
    this._scheduleSave();
    this._notify();
  },

  /* Shared restore path for undo(), redo(), and clicking an arbitrary entry
     in the history panel — one code path so the panel and the keyboard
     shortcuts can never disagree about what "current" means. */
  _restoreHistoryIndex(idx) {
    const entry = this.history[idx];
    if (!entry) return;
    this.historyIndex = idx;
    this.segments = _cloneSegments(entry.segments);
    this.markers = (entry.markers || []).map((m) => ({ ...m }));
    this._saveMarkers();
    if (this.selected >= this.segments.length) this.selected = Math.max(0, this.segments.length - 1);
    this._scheduleSave();
    this._notify();
    this._notifyHistory();
    try { window.EditorUI.timeline?.renderMarkers?.(); } catch (e) { console.error(e); }
  },
  undo() {
    if (this.historyIndex <= 0) return;
    this._restoreHistoryIndex(this.historyIndex - 1);
  },
  redo() {
    if (this.historyIndex >= this.history.length - 1) return;
    this._restoreHistoryIndex(this.historyIndex + 1);
  },
  /* Entry point for the history panel (spec v5 addendum "Undo history
     panel") — clicking any entry, not just the immediate neighbor. */
  restoreToHistoryIndex(idx) {
    if (idx < 0 || idx >= this.history.length || idx === this.historyIndex) return;
    this._restoreHistoryIndex(idx);
  },

  select(i) {
    if (!this.segments || !this.segments.length) return;
    this.selected = Math.max(0, Math.min(i, this.segments.length - 1));
    this._notifySelection();
  },

  trim(i, field, value) {
    if (!this.segments?.[i]) return;
    const segs = _cloneSegments(this.segments);
    const s = segs[i];
    const dur = this.clipDuration(s.clip_id);
    let v = Math.round(value / 0.05) * 0.05;
    v = Math.max(0, Math.min(v, dur));
    if (field === "start") v = Math.min(v, s.end - 0.1);
    else v = Math.max(v, s.start + 0.1);
    s[field] = Math.round(v * 1000) / 1000;
    const name = this.clip(s.clip_id)?.filename || s.clip_id;
    this.commit(segs, { select: i, label: `Trim ${name} ${field}` });
  },

  setTransition(i, type) {
    if (!this.segments?.[i]) return;
    const segs = _cloneSegments(this.segments);
    const s = segs[i];
    s.transition = s.transition || { type: "none", duration: 0.5 };
    s.transition.type = type;
    this.commit(segs, { select: i, label: type === "none" ? "Remove transition" : `Transition ${type}` });
  },
  setTransitionDuration(i, dur) {
    if (!this.segments?.[i] || this.segments[i].transition?.type === "none") return;
    const segs = _cloneSegments(this.segments);
    segs[i].transition.duration = Math.max(0.2, Math.min(1.5, dur));
    this.commit(segs, { select: i, label: "Transition duration" });
  },

  reorder(fromIndex, toIndex) {
    if (fromIndex === toIndex) return;
    const segs = _cloneSegments(this.segments);
    const [moved] = segs.splice(fromIndex, 1);
    segs.splice(toIndex, 0, moved);
    this.commit(segs, { select: toIndex, label: "Reorder" });
  },

  deleteSelected() {
    if (!this.segments?.length) return;
    const i = this.selected;
    const name = this.clip(this.segments[i]?.clip_id)?.filename || this.segments[i]?.clip_id;
    const segs = _cloneSegments(this.segments);
    segs.splice(i, 1);
    this.commit(segs, { select: Math.min(i, segs.length - 1), label: name ? `Delete ${name}` : "Delete" });
  },

  splitAt(i, at) {
    const s = this.segments?.[i];
    if (!s || !(s.start < at && at < s.end)) return;
    const segs = _cloneSegments(this.segments);
    const text = segs[i].text || "";
    const first = { ...segs[i], end: at };
    const second = _withClientId({ ...segs[i], start: at, text, transition: { type: "none", duration: 0.5 } });
    segs.splice(i, 1, first, second);
    this.commit(segs, { select: i + 1, label: "Split" });
  },

  async resetToAiCut() {
    if (!this.pid) return;
    const { segments } = await api(`/projects/${this.pid}/edl/reset`, { method: "POST" });
    this.segments = segments.map(_withClientId);
    this.selected = 0;
    this.history = [];
    this.historyIndex = -1;
    this._pushHistory("Reset to AI cut");
    this.dirty = false;
    _setSaveState("Reset to AI cut.");
    this._notify();
  },

  totalDuration() {
    return (this.segments || []).reduce((acc, s) => acc + (s.end - s.start), 0);
  },
  cumulative() {
    let t = 0;
    return (this.segments || []).map((s) => {
      const d = Math.max(0, s.end - s.start);
      const row = { start: t, end: t + d };
      t += d;
      return row;
    });
  },
  segmentAtEdlTime(t) {
    if (!this.segments?.length) return null;
    const cum = this.cumulative();
    for (let i = 0; i < cum.length; i++) {
      if (t < cum[i].end || i === cum.length - 1) {
        const local = this.segments[i].start + Math.max(0, t - cum[i].start);
        return { index: i, local: Math.min(local, this.segments[i].end) };
      }
    }
    return null;
  },

  _scheduleSave() {
    this.dirty = true;
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.save(), 2000);
    _setSaveState("Unsaved changes…");
  },

  async save() {
    if (!this.pid || !this.segments) return;
    clearTimeout(this.saveTimer);
    const body = {
      segments: this.segments.map((s) => ({
        clip_id: s.clip_id, start: s.start, end: s.end, text: s.text || "",
        transition: s.transition || { type: "none", duration: 0.5 },
      })),
    };
    try {
      await api(`/projects/${this.pid}/edl`, { method: "PUT", body });
      this.dirty = false;
      _setSaveState("Saved");
    } catch (e) {
      _setSaveState(`Save failed: ${e.message}`, true);
    }
  },

  _notify() {
    [
      () => window.EditorUI.timeline?.render(),
      () => window.EditorUI.player?.onSegmentsChanged(),
      () => window.EditorUI.inspector?.renderVideo(),
    ].forEach((fn) => { try { fn(); } catch (e) { console.error("EditorUI render error", e); } });
  },
  _notifySelection() {
    [
      () => window.EditorUI.timeline?.renderSelection(),
      () => window.EditorUI.inspector?.renderVideo(),
    ].forEach((fn) => { try { fn(); } catch (e) { console.error("EditorUI selection error", e); } });
  },
  /* Keeps the history panel (if open) in sync with every push/undo/redo/
     restore — cheap no-op when the panel is closed (renderHistoryPanel
     checks its own open flag first). */
  _notifyHistory() {
    try { window.EditorUI.timeline?.renderHistoryPanel?.(); } catch (e) { console.error("EditorUI history error", e); }
  },
};

window.Editor = Editor;

/* ---------- lifecycle hooks driven by core.js refreshProject() ---------- */

window.EditorUI.onProjectSelected = async function (project) {
  try { window.EditorUI.mediabin?.render(project); } catch (e) { console.error(e); }
  try {
    await Editor.load(project.id);
  } catch (e) {
    console.error("Failed to load EDL", e);
    Editor.segments = [];
  }
  try { window.EditorUI.player?.mount(); } catch (e) { console.error(e); }
  try { window.EditorUI.timeline?.mount(); } catch (e) { console.error(e); }
  try { window.EditorUI.inspector?.mount(); } catch (e) { console.error(e); }
  // Resizable panels (spec v5.9a) — idempotent (mount() no-ops after the
  // first call), but harmless to call every time a project is (re)selected.
  try { window.EditorUI.splitters?.mount(); } catch (e) { console.error(e); }
  // Manual overlay track's player-stage bounding box (spec v5.9b).
  try { window.EditorUI.overlaybox?.mount(); } catch (e) { console.error(e); }
  try { _mountSpeakerSelect(project); } catch (e) { console.error(e); }
  try { _mountLanguageSelect(project); } catch (e) { console.error(e); }
  try { _mountPacingSelect(project); } catch (e) { console.error(e); }
};

window.EditorUI.onProjectRefreshed = function (project) {
  // Clips can change (ingest probing durations, add/remove) independently of
  // the EDL, so refresh the bin + the parts of the inspector that read
  // project-level fields directly. Never touch Editor.segments here — that
  // would clobber in-flight local edits every time refreshProject() runs
  // (e.g. while a job is being polled).
  try { window.EditorUI.mediabin?.render(project); } catch (e) { console.error(e); }
  try { window.EditorUI.inspector?.renderColorAudio(); } catch (e) { console.error(e); }
  // color/subtitles/audio_enhance/preview can all change here (inspector tabs,
  // a completed preview_render job) — recheck render-bar + player-mode staleness.
  try { window.EditorUI.timeline?.refreshRenderBar?.(); } catch (e) { console.error(e); }
  try { window.EditorUI.player?.onProjectRefreshed?.(); } catch (e) { console.error(e); }
  try { _mountSpeakerSelect(project); } catch (e) { console.error(e); }
  try { _mountLanguageSelect(project); } catch (e) { console.error(e); }
  try { _mountPacingSelect(project); } catch (e) { console.error(e); }
};

/* ---------- "Locutores" speaker-count field (spec v5.8c UI) ----------
   A small select in the media bin header (ui/editor/mediabin.js's
   #media-bin .bin-head, owned by another agent this phase — same
   fair-game DOM-injection pattern ui/editor/timeline.js already uses on
   the timeline toolbar) PATCHing project.speaker_count via the endpoint
   magic_video_editor/api/projects.py already exposes (1/2/3/4/"auto",
   default 1). Wired once, then just kept in sync with the live project
   value on every render (mediabin.js rebuilds #media-bin-list's innerHTML
   on every render(), but NOT .bin-head itself, so the select survives). */
function _mountSpeakerSelect(project) {
  const head = document.querySelector("#media-bin .bin-head");
  if (!head) return;
  let sel = document.getElementById("speaker-count-select");
  if (!sel) {
    const wrap = document.createElement("label");
    wrap.className = "row";
    wrap.style.cssText = "gap:4px; font-size:12px;";
    wrap.title = "How many distinct speakers to detect for subtitle diarization "
      + "— a known count is far more reliable than auto-detecting it.";
    const span = document.createElement("span");
    span.className = "dim";
    span.textContent = "Locutores";
    sel = document.createElement("select");
    sel.id = "speaker-count-select";
    sel.innerHTML = ["1", "2", "3", "4", "auto"].map((v) =>
      `<option value="${v}">${v === "auto" ? "Auto" : v}</option>`).join("");
    sel.onchange = async () => {
      const raw = sel.value;
      const value = raw === "auto" ? "auto" : Number(raw);
      try {
        await api(`/projects/${state.pid}`, { method: "PATCH", body: { speaker_count: value } });
        if (state.project) state.project.speaker_count = value;
      } catch (e) {
        showToast(`Couldn't update speaker count: ${e.message}`);
        sel.value = String(project.speaker_count ?? 1);
      }
    };
    wrap.appendChild(span);
    wrap.appendChild(sel);
    // Sits just before the health/settings footer's usual spot — right after
    // the group's add-file buttons, before the "grow" spacer would push
    // things off; simplest correct placement is right at the end of the
    // header row (bin-head is a flex .row, so it wraps under narrow bins).
    head.appendChild(wrap);
  }
  const cur = project?.speaker_count ?? 1;
  if (document.activeElement !== sel) sel.value = String(cur);
}

/* ---------- "Idioma" per-project transcription-language override ----------
   Field bug follow-up (2026-07-25): whisper's per-clip auto language
   detection can misfire on one clip's first window and TRANSLATE instead of
   transcribe (fluent Spanish audio -> fluent English text, not garbage).
   Sits right next to "Locutores" in the same media-bin header, same
   fair-game DOM-injection pattern -- PATCHes project.language_override via
   magic_video_editor/api/projects.py (ProjectUpdate.language_override,
   values shared with api/settings.py LANGUAGE_CODES). "auto" means "no
   project override, fall back to the Settings-level default" (see
   pipeline/transcribe.py _resolve_language). */
const _LANGUAGE_OPTIONS = [
  ["auto", "Auto"],
  ["es", "Español"],
  ["en", "English"],
  ["fr", "Français"],
  ["de", "Deutsch"],
  ["it", "Italiano"],
  ["pt", "Português"],
  ["ca", "Català"],
];

function _mountLanguageSelect(project) {
  const head = document.querySelector("#media-bin .bin-head");
  if (!head) return;
  let sel = document.getElementById("language-override-select");
  if (!sel) {
    const wrap = document.createElement("label");
    wrap.className = "row";
    wrap.style.cssText = "gap:4px; font-size:12px;";
    wrap.title = "Fija el idioma de transcripción para este proyecto -- \"Auto\" detecta el "
      + "idioma clip a clip (y se autocorrige si un clip discrepa de la mayoría); fijar un "
      + "idioma se lo aplica a todos los clips y evita que uno se transcriba/traduzca al "
      + "idioma equivocado.";
    const span = document.createElement("span");
    span.className = "dim";
    span.textContent = "Idioma";
    sel = document.createElement("select");
    sel.id = "language-override-select";
    sel.innerHTML = _LANGUAGE_OPTIONS.map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
    sel.onchange = async () => {
      const value = sel.value;
      try {
        await api(`/projects/${state.pid}`, { method: "PATCH", body: { language_override: value } });
        if (state.project) state.project.language_override = value;
      } catch (e) {
        showToast(`Couldn't update language: ${e.message}`);
        sel.value = project.language_override || "auto";
      }
    };
    wrap.appendChild(span);
    wrap.appendChild(sel);
    head.appendChild(wrap);
  }
  const cur = project?.language_override || "auto";
  if (document.activeElement !== sel) sel.value = cur;
}

/* ---------- "Ritmo" per-project cutting-rhythm field ----------
   Owner feature (2026-07-26): a manual-vs-auto comparison found the auto cut
   too aggressive on head lead-in / mid-paragraph micro-breaths / tail vs. a
   human editor. "Ritmo" lets the user dial that in per project --
   tight/natural/airy (Spanish UI: ceñido/natural/con aire), default natural.
   Sits right next to "Locutores"/"Idioma" in the same media-bin header, same
   fair-game DOM-injection pattern -- PATCHes project.pacing via
   magic_video_editor/api/projects.py (ProjectUpdate.pacing, values shared
   with config.PACING_PRESETS' keys). See pipeline/ordering.py's
   resolve_pacing_preset for how each value maps to head_pad_s/merge_gap_s/
   tail_pad_s. An unset value shows as "natural" (config.DEFAULT_PACING) even
   though the project itself has no pacing field yet -- mirrors "Idioma"'s
   own unset-shows-as-auto convention. */
const _PACING_OPTIONS = [
  ["tight", "Ceñido"],
  ["natural", "Natural"],
  ["airy", "Con aire"],
];

function _mountPacingSelect(project) {
  const head = document.querySelector("#media-bin .bin-head");
  if (!head) return;
  let sel = document.getElementById("pacing-select");
  if (!sel) {
    const wrap = document.createElement("label");
    wrap.className = "row";
    wrap.style.cssText = "gap:4px; font-size:12px;";
    wrap.title = "Ritmo de corte: qué tan pegado o respirado queda el resultado -- \"Ceñido\" "
      + "corta lo más posible (menos aire, más riesgo de cortar una micro-pausa a mitad de "
      + "frase); \"Con aire\" deja más aire antes/después y no corta las respiraciones cortas "
      + "dentro de un mismo párrafo; \"Natural\" es el punto intermedio recomendado.";
    const span = document.createElement("span");
    span.className = "dim";
    span.textContent = "Ritmo";
    sel = document.createElement("select");
    sel.id = "pacing-select";
    sel.innerHTML = _PACING_OPTIONS.map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
    sel.onchange = async () => {
      const value = sel.value;
      try {
        await api(`/projects/${state.pid}`, { method: "PATCH", body: { pacing: value } });
        if (state.project) state.project.pacing = value;
      } catch (e) {
        showToast(`Couldn't update pacing: ${e.message}`);
        sel.value = project.pacing || "natural";
      }
    };
    wrap.appendChild(span);
    wrap.appendChild(sel);
    head.appendChild(wrap);
  }
  const cur = project?.pacing || "natural";
  if (document.activeElement !== sel) sel.value = cur;
}
