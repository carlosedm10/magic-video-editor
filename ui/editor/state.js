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
    _setSaveState("");
    this._notify();
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
};
