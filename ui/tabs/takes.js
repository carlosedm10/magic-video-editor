/* Takes tab — per-clip sentence list with keep/cut toggling. */

function renderTakes() {
  const p = state.project;
  if (!p.sentences?.length) {
    $("#tab-takes").innerHTML = '<div class="dim">Run Transcribe + Takes first.</div>';
    return;
  }
  const byClip = {};
  p.sentences.forEach((s) => (byClip[s.clip_id] ??= []).push(s));
  $("#tab-takes").innerHTML = `
    <div class="hint">Every detected sentence. Grayed = cut (repeated take, fragment, or excluded by you).
      Amber border = part of a repeated-take group. Click a sentence to toggle keep/cut.</div>` +
    Object.entries(byClip).map(([cid, sents]) => {
      const clip = p.clips.find((c) => c.id === cid);
      return `<div class="card">
        <div class="row" style="margin-bottom:8px"><b>${esc(clip?.filename || cid)}</b>
          <span class="dim">${sents.filter((s) => s.kept).length}/${sents.length} kept</span></div>
        ${sents.map((s) => `
          <div class="sentence ${s.kept ? "" : "cut"} ${s.dup_group ? "dup" : ""}" data-sid="${s.id}">
            <span class="t">${fmtT(s.start)}–${fmtT(s.end)}</span>
            <span class="grow">${esc(s.text)}
              <div class="why">${esc(s.reason || s.why || "")} ${s.score != null ? `· score ${s.score}` : ""}</div>
            </span>
          </div>`).join("")}
      </div>`;
    }).join("");
  document.querySelectorAll(".sentence").forEach((el) => el.onclick = async () => {
    const s = p.sentences.find((x) => x.id === el.dataset.sid);
    await api(`/projects/${p.id}/sentences/${s.id}`, { method: "POST", body: { kept: !s.kept } });
    refreshProject();
  });
}

window.TABS.takes = renderTakes;
