/* Activity tab — this session's job log, most recent first. */

function renderJobs() {
  const jobs = Object.values(state.jobs).sort((a, b) => b.started_at - a.started_at);
  $("#tab-jobs").innerHTML = jobs.length ? jobs.map((j) => `
    <div class="card">
      <div class="row"><b>${esc(j.name)}</b>
        <span class="pill">${j.status}</span>
        <span class="dim">${Math.round(j.progress * 100)}%</span></div>
      ${j.error ? `<div class="dim" style="color:var(--danger)">${esc(j.error)}</div>` : ""}
      <div class="log">${j.log.map(esc).join("\n")}</div>
    </div>`).join("") : '<div class="dim">No activity yet this session.</div>';
}

window.TABS.jobs = renderJobs;
