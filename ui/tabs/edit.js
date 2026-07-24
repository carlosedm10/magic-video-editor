/* Studio tab — manual Premiere-lite editor on top of the AI cut's EDL
   (cutroom/api/edl.py). A vertical, drag-to-reorder timeline of segments:
   trim start/end, split, delete, preview in a shared <video> player.
   Render still uses the existing render stage (project["edl"] is what
   render.run consumes). Color/audio panels render below if their feature
   agents have implemented them. */

const editState = { pid: null, segments: null, saveTimer: null, dirty: false };

function _clipInfo(p, cid) {
  return p.clips.find((c) => c.id === cid);
}

function _clipDuration(p, cid) {
  return _clipInfo(p, cid)?.info?.duration ?? Infinity;
}

async function loadEdl(pid) {
  const { segments } = await api(`/projects/${pid}/edl`);
  editState.pid = pid;
  editState.segments = segments;
  editState.dirty = false;
}

function scheduleSave() {
  editState.dirty = true;
  clearTimeout(editState.saveTimer);
  editState.saveTimer = setTimeout(saveEdl, 700);
}

async function saveEdl() {
  if (!editState.dirty || !editState.pid) return;
  const body = {
    segments: editState.segments.map((s) => ({
      clip_id: s.clip_id, start: s.start, end: s.end, text: s.text || "",
    })),
  };
  try {
    await api(`/projects/${editState.pid}/edl`, { method: "PUT", body });
    editState.dirty = false;
  } catch (e) {
    alert(`Save failed: ${e.message}`);
  }
}

function renderEdit() {
  const p = state.project;
  if (editState.pid !== p.id) {
    editState.segments = null;
  }
  if (editState.segments === null) {
    $("#tab-edit").innerHTML = '<div class="dim">Loading timeline...</div>';
    loadEdl(p.id).then(renderEdit);
    return;
  }
  renderStudioBody(p);
}

function renderStudioBody(p) {
  const segs = editState.segments;
  const total = segs.reduce((acc, s) => acc + (s.end - s.start), 0);

  $("#tab-edit").innerHTML = `
    <div class="hint">Trim, reorder (drag), split, or delete segments — Render uses this exact list.</div>
    <div class="card row" style="justify-content:space-between">
      <span><b>${segs.length} segments</b> <span class="dim">${fmtT(total)} total</span></span>
      <span class="row">
        <button class="btn small" id="edl-reset">Reset to AI cut</button>
        <button class="btn small" id="edl-save">Save EDL</button>
        <button class="btn primary small" id="edl-render">Render</button>
      </span>
    </div>
    <video id="studio-preview" controls preload="metadata" style="display:${segs.length ? "block" : "none"}"></video>
    <div id="edl-list">${segs.map((s, i) => {
      const clip = _clipInfo(p, s.clip_id);
      const dur = clip?.info?.duration;
      const mid = ((s.start + s.end) / 2).toFixed(1);
      return `<div class="card row edl-row" draggable="true" data-idx="${i}" style="cursor:default">
        <span class="drag-handle" style="cursor:grab">⠿</span>
        <span style="min-width:160px"><b>${esc(clip?.filename || s.clip_id)}</b>
          <div class="dim">clip ${dur != null ? fmtT(dur) : "?"}</div></span>
        <span class="row" style="gap:4px">
          <button class="btn small nudge" data-i="${i}" data-field="start" data-delta="-0.1">−</button>
          <input type="number" step="0.1" class="edl-num" data-i="${i}" data-field="start" value="${s.start.toFixed(1)}" style="width:70px">
          <button class="btn small nudge" data-i="${i}" data-field="start" data-delta="0.1">+</button>
          <span class="dim">→</span>
          <button class="btn small nudge" data-i="${i}" data-field="end" data-delta="-0.1">−</button>
          <input type="number" step="0.1" class="edl-num" data-i="${i}" data-field="end" value="${s.end.toFixed(1)}" style="width:70px">
          <button class="btn small nudge" data-i="${i}" data-field="end" data-delta="0.1">+</button>
        </span>
        <span class="dim">${fmtT(s.end - s.start)}</span>
        <span class="grow"></span>
        <input type="number" step="0.1" class="edl-num split-at" data-i="${i}" value="${mid}" style="width:70px" title="Split point">
        <button class="btn small split" data-i="${i}">Split</button>
        <button class="btn small preview" data-i="${i}">Preview</button>
        <button class="btn small danger delete" data-i="${i}">Delete</button>
      </div>`;
    }).join("") || '<div class="dim">No segments — nothing kept yet, or reset to the AI cut.</div>'}</div>
    <div id="panel-color"></div>
    <div id="panel-audio"></div>`;

  wireStudioEvents(p);

  if (typeof window.ColorPanel !== "undefined") window.ColorPanel.render($("#panel-color"));
  if (typeof window.AudioPanel !== "undefined") window.AudioPanel.render($("#panel-audio"));
  // Backward/alt-shape hooks, per the original brief naming — used if a feature
  // agent exposes PANELS.color/audio instead of window.ColorPanel/AudioPanel.
  if (typeof window.PANELS !== "undefined") {
    if (typeof window.PANELS.color === "function") window.PANELS.color($("#panel-color"));
    if (typeof window.PANELS.audio === "function") window.PANELS.audio($("#panel-audio"));
  }
}

function wireStudioEvents(p) {
  const pid = p.id;

  $("#edl-reset").onclick = async () => {
    await api(`/projects/${pid}/edl/reset`, { method: "POST" });
    editState.segments = null;
    renderEdit();
  };
  $("#edl-save").onclick = async () => {
    clearTimeout(editState.saveTimer);
    editState.dirty = true;
    await saveEdl();
  };
  $("#edl-render").onclick = () => runStage("render");

  const video = $("#studio-preview");
  let stopAt = null;
  video.ontimeupdate = () => {
    if (stopAt != null && video.currentTime >= stopAt) {
      video.pause();
      stopAt = null;
    }
  };

  document.querySelectorAll(".preview").forEach((btn) => btn.onclick = () => {
    const i = Number(btn.dataset.i);
    const s = editState.segments[i];
    video.style.display = "block";
    stopAt = s.end;
    const src = `/api/projects/${pid}/media/clip/${s.clip_id}#t=${s.start}`;
    if (video.dataset.clipId !== s.clip_id) {
      video.dataset.clipId = s.clip_id;
      video.src = src;
    } else {
      video.currentTime = s.start;
    }
    video.play().catch(() => {});
  });

  document.querySelectorAll(".nudge").forEach((btn) => btn.onclick = () => {
    const i = Number(btn.dataset.i);
    const field = btn.dataset.field;
    const delta = Number(btn.dataset.delta);
    const s = editState.segments[i];
    const clipDur = _clipDuration(p, s.clip_id);
    let v = s[field] + delta;
    v = Math.max(0, Math.min(v, clipDur));
    if (field === "start") v = Math.min(v, s.end - 0.1);
    else v = Math.max(v, s.start + 0.1);
    s[field] = Math.round(v * 10) / 10;
    scheduleSave();
    renderStudioBody(p);
  });

  document.querySelectorAll(".edl-num").forEach((inp) => {
    if (inp.classList.contains("split-at")) return;
    inp.onchange = () => {
      const i = Number(inp.dataset.i);
      const field = inp.dataset.field;
      const s = editState.segments[i];
      const clipDur = _clipDuration(p, s.clip_id);
      let v = Number(inp.value);
      if (Number.isNaN(v)) v = s[field];
      v = Math.max(0, Math.min(v, clipDur));
      if (field === "start") v = Math.min(v, s.end - 0.1);
      else v = Math.max(v, s.start + 0.1);
      s[field] = Math.round(v * 10) / 10;
      scheduleSave();
      renderStudioBody(p);
    };
  });

  document.querySelectorAll(".split").forEach((btn) => btn.onclick = async () => {
    const i = Number(btn.dataset.i);
    const atInput = document.querySelector(`.split-at[data-i="${i}"]`);
    const at = Number(atInput.value);
    try {
      await api(`/projects/${pid}/edl/split`, { method: "POST", body: { index: i, at } });
      editState.segments = null;
      renderEdit();
    } catch (e) { alert(e.message); }
  });

  document.querySelectorAll(".delete").forEach((btn) => btn.onclick = async () => {
    const i = Number(btn.dataset.i);
    editState.segments.splice(i, 1);
    clearTimeout(editState.saveTimer);
    editState.dirty = true;
    await saveEdl();
    renderStudioBody(p);
  });

  // drag & drop reordering of segments (native drag, mirrors the old order-list pattern)
  let dragEl = null;
  document.querySelectorAll(".edl-row").forEach((el) => {
    el.ondragstart = () => (dragEl = el);
    el.ondragover = (e) => {
      e.preventDefault();
      const list = $("#edl-list");
      const after = [...list.children].find((c) =>
        c !== dragEl && e.clientY < c.getBoundingClientRect().top + c.offsetHeight / 2);
      list.insertBefore(dragEl, after || null);
    };
    el.ondragend = async () => {
      const newOrder = [...document.querySelectorAll(".edl-row")].map((c) => Number(c.dataset.idx));
      editState.segments = newOrder.map((i) => editState.segments[i]);
      clearTimeout(editState.saveTimer);
      editState.dirty = true;
      await saveEdl();
      renderStudioBody(p);
    };
  });
}

window.TABS.edit = renderEdit;
