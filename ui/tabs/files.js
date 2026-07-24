/* Files tab — clip list, add files, clip role/main toggles. */

function renderFiles() {
  const p = state.project;
  const clips = p.clips.map((c) => `
    <div class="card row">
      <div class="grow">
        <div>${esc(c.filename)}
          ${c.is_main ? '<span class="pill main">main camera</span>' : ""}
          <span class="pill ${c.role}">${c.role}</span>
        </div>
        <div class="dim">${c.info ? `${c.info.duration.toFixed(0)}s · ${c.info.width}x${c.info.height} @ ${c.info.fps}fps` : "not ingested yet"}
          ${c.language ? ` · lang: ${c.language}` : ""}</div>
      </div>
      ${c.role === "camera" && !c.is_main ?
        `<button class="btn small" data-main="${c.id}">Set main</button>` : ""}
      <button class="btn small" data-role="${c.id}">${c.role === "camera" ? "→ audio" : "→ camera"}</button>
      <button class="btn small danger" data-del="${c.id}">Remove</button>
    </div>`).join("");

  $("#tab-files").innerHTML = `
    <div class="hint">Add your raw footage (and optionally a separate audio recording).
      Files are imported (linked or copied) into the project, so it's safe to move or delete
      the originals afterwards. Mark which camera is the main one, then run the pipeline
      stages in order (top right) — or click <b>✦ Run pipeline</b> in the header to run
      everything at once.</div>
    <div class="row" style="margin-bottom:14px">
      <button class="btn primary" id="add-files">＋ Add files…</button>
      <input type="text" id="path-input" class="grow" placeholder="…or paste absolute file paths, comma separated, and press Enter">
    </div>
    ${clips || '<div class="dim">No files yet.</div>'}`;

  $("#add-files").onclick = async () => {
    let paths = [];
    if (window.pywebview?.api?.pick_files) paths = await window.pywebview.api.pick_files();
    else { $("#path-input").focus(); return; }
    if (paths.length) await addPaths(paths);
  };
  $("#path-input").onkeydown = async (e) => {
    if (e.key === "Enter") await addPaths(e.target.value.split(",").map((s) => s.trim()).filter(Boolean));
  };
  document.querySelectorAll("[data-main]").forEach((el) => el.onclick = async () => {
    await api(`/projects/${p.id}/clips/${el.dataset.main}`, { method: "POST", body: { is_main: true } });
    refreshProject();
  });
  document.querySelectorAll("[data-role]").forEach((el) => el.onclick = async () => {
    const clip = p.clips.find((c) => c.id === el.dataset.role);
    await api(`/projects/${p.id}/clips/${clip.id}`, {
      method: "POST", body: { role: clip.role === "camera" ? "audio" : "camera" } });
    refreshProject();
  });
  document.querySelectorAll("[data-del]").forEach((el) => el.onclick = async () => {
    await api(`/projects/${p.id}/clips/${el.dataset.del}`, { method: "DELETE" });
    refreshProject();
  });
}

async function addPaths(paths) {
  await api(`/projects/${state.pid}/clips`, { method: "POST", body: { paths } });
  await refreshProject();
}

window.TABS.files = renderFiles;
