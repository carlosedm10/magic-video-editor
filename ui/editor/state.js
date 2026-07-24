/* Editor core: EDL segment state, undo/redo history (depth 50), selection,
   and debounced autosave. Everything else in ui/editor/*.js reads/mutates
   the timeline strictly through this `Editor` object and registers its own
   render entry points onto window.EditorUI; this file wires those entry
   points to core.js's project lifecycle (onProjectSelected/onProjectRefreshed)
   so core.js never needs to know the editor's internals.

   Server contract: GET/PUT /api/projects/{pid}/edl, POST .../edl/reset,
   POST .../edl/split (cutroom/api/edl.py). Each segment may carry a
   `transition` {type: none|fade|crossfade, duration} — the transition INTO
   that segment (docs/PLATFORM-SPEC.md "Transitions (junction-level)"). */

window.EditorUI = window.EditorUI || {};

const HISTORY_DEPTH = 50;

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
   Mirrors cutroom/pipeline/render.py's _preview_manifest: sha256 hex[:16] of
   json.dumps({edl, color, subtitles, audio_enhance}, sort_keys=True,
   default=str). Two things a generic JSON.stringify would get wrong here:
   (1) Python's default `json.dumps` separators are ", " and ": " (a space
   after each), NOT JSON.stringify's compact ",%":"; get this wrong and every
   hash mismatches even when the data is identical — verified byte-for-byte
   against cutroom.pipeline.render._preview_manifest while building this.
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
    this.history = [_cloneSegments(this.segments)];
    this.historyIndex = 0;
    this.dirty = false;
    this._loadMarkers();
    _setSaveState("");
    this._notify();
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
    try { window.EditorUI.timeline?.renderMarkers?.(); } catch (e) { console.error(e); }
  },
  removeMarker(id) {
    this.markers = this.markers.filter((m) => m.id !== id);
    this._saveMarkers();
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
    this.commit(segs, { select: idx });
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

  _pushHistory() {
    this.history = this.history.slice(0, this.historyIndex + 1);
    this.history.push(_cloneSegments(this.segments));
    if (this.history.length > HISTORY_DEPTH) this.history.shift();
    this.historyIndex = this.history.length - 1;
  },

  commit(newSegments, { select } = {}) {
    this.segments = newSegments;
    if (select != null) this.selected = select;
    if (this.selected >= this.segments.length) this.selected = Math.max(0, this.segments.length - 1);
    this._pushHistory();
    this._scheduleSave();
    this._notify();
  },

  undo() {
    if (this.historyIndex <= 0) return;
    this.historyIndex--;
    this.segments = _cloneSegments(this.history[this.historyIndex]);
    if (this.selected >= this.segments.length) this.selected = Math.max(0, this.segments.length - 1);
    this._scheduleSave();
    this._notify();
  },
  redo() {
    if (this.historyIndex >= this.history.length - 1) return;
    this.historyIndex++;
    this.segments = _cloneSegments(this.history[this.historyIndex]);
    if (this.selected >= this.segments.length) this.selected = Math.max(0, this.segments.length - 1);
    this._scheduleSave();
    this._notify();
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
    this.commit(segs, { select: i });
  },

  setTransition(i, type) {
    if (!this.segments?.[i]) return;
    const segs = _cloneSegments(this.segments);
    const s = segs[i];
    s.transition = s.transition || { type: "none", duration: 0.5 };
    s.transition.type = type;
    this.commit(segs, { select: i });
  },
  setTransitionDuration(i, dur) {
    if (!this.segments?.[i] || this.segments[i].transition?.type === "none") return;
    const segs = _cloneSegments(this.segments);
    segs[i].transition.duration = Math.max(0.2, Math.min(1.5, dur));
    this.commit(segs, { select: i });
  },

  reorder(fromIndex, toIndex) {
    if (fromIndex === toIndex) return;
    const segs = _cloneSegments(this.segments);
    const [moved] = segs.splice(fromIndex, 1);
    segs.splice(toIndex, 0, moved);
    this.commit(segs, { select: toIndex });
  },

  deleteSelected() {
    if (!this.segments?.length) return;
    const i = this.selected;
    const segs = _cloneSegments(this.segments);
    segs.splice(i, 1);
    this.commit(segs, { select: Math.min(i, segs.length - 1) });
  },

  splitAt(i, at) {
    const s = this.segments?.[i];
    if (!s || !(s.start < at && at < s.end)) return;
    const segs = _cloneSegments(this.segments);
    const text = segs[i].text || "";
    const first = { ...segs[i], end: at };
    const second = _withClientId({ ...segs[i], start: at, text, transition: { type: "none", duration: 0.5 } });
    segs.splice(i, 1, first, second);
    this.commit(segs, { select: i + 1 });
  },

  async resetToAiCut() {
    if (!this.pid) return;
    const { segments } = await api(`/projects/${this.pid}/edl/reset`, { method: "POST" });
    this.segments = segments.map(_withClientId);
    this.selected = 0;
    this.history = [_cloneSegments(this.segments)];
    this.historyIndex = 0;
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
};
