/* Reels tab — scored short-form candidates, render 9:16. */

function renderReels() {
  const p = state.project;
  if (!p.reels?.length) {
    $("#tab-reels").innerHTML = '<div class="dim">Run the Reels stage to get ~20 scored suggestions.</div>';
    return;
  }
  $("#tab-reels").innerHTML = `<div class="reel-grid">` + p.reels.map((r) => `
    <div class="card">
      <div><span class="score">${r.score}</span> · #${r.rank} <b>${esc(r.title)}</b></div>
      <div class="dim">${r.duration}s · hook ${r.hook} · standalone ${r.self_contained} · payoff ${r.payoff}</div>
      <div class="dim" style="margin:6px 0">${esc(r.text.slice(0, 160))}…</div>
      ${r.path
        ? `<video controls preload="metadata" src="/api/projects/${p.id}/media/file?path=${encodeURIComponent(r.path)}"></video>`
        : `<button class="btn primary small" data-reel="${r.id}">Render 9:16</button>`}
    </div>`).join("") + `</div>`;
  document.querySelectorAll("[data-reel]").forEach((el) => el.onclick = async () => {
    const { job } = await api(`/projects/${p.id}/reels/${el.dataset.reel}/render`, { method: "POST" });
    watchJob(job);
    setTab("jobs");
  });
}

window.TABS.reels = renderReels;
