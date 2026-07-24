/* Reels tab — scored short-form candidates: copywriter title + collapsible
   description + hashtags (spec v5 addendum "SEO copywriter"), a
   "Regenerate copy" action, an "Edit" button into the Reel Editor
   (ui/editor/reeleditor.js, spec v5 "Reel Editor"), and render 9:16. */

const _reelsExpandedDesc = new Set(); // reel ids whose description is expanded (kept across re-renders this session)

/* Defensive against a real backend data bug observed live against project
   c7642fc7755e: cutroom/pipeline/reels.py does `list(copy.get("hashtags") or [])`
   but cutroom/pipeline/copywriter.py's copy_for_reel returns "hashtags" as a
   SPACE-JOINED STRING (not a list) -- Python's list("#a #b") explodes it into
   one array entry per CHARACTER, which reel["hashtags"] then persists as-is.
   That's a backend fix (not ui/tabs/reels.js's or ui/editor/reeleditor.js's
   to make), so this just filters out the resulting one-char noise rather
   than rendering 50+ single-letter pills. */
function _reelsValidHashtags(tags) {
  return (tags || []).filter((h) => typeof h === "string" && h.replace(/^#/, "").trim().length > 1);
}

function _reelsHashtagText(tags) {
  return _reelsValidHashtags(tags).map((h) => (h.startsWith("#") ? h : `#${h}`)).join(" ");
}

async function _reelsCopyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand("copy");
      ta.remove();
      return true;
    } catch (_e2) {
      return false;
    }
  }
}

function renderReels() {
  const p = state.project;
  if (!p.reels?.length) {
    $("#tab-reels").innerHTML = '<div class="dim">Run the Reels stage to get ~20 scored suggestions.</div>';
    return;
  }
  $("#tab-reels").innerHTML = `<div class="reel-grid">` + p.reels.map((r) => {
    const expanded = _reelsExpandedDesc.has(r.id);
    const hasDesc = !!(r.description || "").trim();
    return `
    <div class="card">
      <div><span class="score">${r.score}</span> · #${r.rank} <b>${esc(r.title || "Untitled")}</b></div>
      <div class="dim">${r.duration}s · hook ${r.hook} · standalone ${r.self_contained} · payoff ${r.payoff}</div>
      ${hasDesc ? `
        <button class="btn small" data-desc-toggle="${r.id}" style="margin:6px 0 4px">
          ${expanded ? "▾ Hide description" : "▸ Show description"}</button>
        <div class="dim" data-desc="${r.id}" style="white-space:pre-wrap;margin-bottom:6px" ${expanded ? "" : "hidden"}>${esc(r.description)}</div>
      ` : `<div class="dim" style="margin:6px 0">${esc((r.text || "").slice(0, 160))}…</div>`}
      ${(() => {
        const tags = _reelsValidHashtags(r.hashtags);
        return tags.length ? `<div class="chip-row">${tags.map((h) => `<span class="pill">${esc(h.startsWith("#") ? h : "#" + h)}</span>`).join("")}</div>` : "";
      })()}
      <div class="row" style="margin:8px 0">
        <button class="btn small" data-copy="${r.id}">📋 Copy</button>
        <button class="btn small" data-regen="${r.id}">↻ Regenerate copy</button>
        <button class="btn small" data-edit="${r.id}">✎ Edit</button>
      </div>
      ${r.path
        ? `<video controls preload="metadata" src="/api/projects/${p.id}/media/file?path=${encodeURIComponent(r.path)}"></video>`
        : `<button class="btn primary small" data-reel="${r.id}">Render 9:16</button>`}
    </div>`;
  }).join("") + `</div>`;

  document.querySelectorAll("[data-desc-toggle]").forEach((el) => el.onclick = () => {
    const id = el.dataset.descToggle;
    if (_reelsExpandedDesc.has(id)) _reelsExpandedDesc.delete(id); else _reelsExpandedDesc.add(id);
    renderReels();
  });

  document.querySelectorAll("[data-copy]").forEach((el) => el.onclick = async () => {
    const r = p.reels.find((x) => x.id === el.dataset.copy);
    if (!r) return;
    const text = [r.title || "", "", r.description || "", "", _reelsHashtagText(r.hashtags)].join("\n");
    const ok = await _reelsCopyToClipboard(text);
    const original = el.textContent;
    el.textContent = ok ? "✓ Copied" : "Copy failed";
    setTimeout(() => { el.textContent = original; }, 1500);
  });

  document.querySelectorAll("[data-regen]").forEach((el) => el.onclick = async () => {
    el.disabled = true;
    el.textContent = "Regenerating…";
    try {
      const updated = await api(`/projects/${p.id}/reels/${el.dataset.regen}/regenerate-copy`, { method: "POST" });
      const idx = p.reels.findIndex((x) => x.id === updated.id);
      if (idx >= 0) p.reels[idx] = updated;
      renderReels();
    } catch (e) {
      alert(`Regenerate failed: ${e.message}`);
      el.disabled = false;
      el.textContent = "↻ Regenerate copy";
    }
  });

  document.querySelectorAll("[data-edit]").forEach((el) => el.onclick = () => {
    window.ReelEditor?.open(el.dataset.edit);
  });

  document.querySelectorAll("[data-reel]").forEach((el) => el.onclick = async () => {
    // reels/{rid}/render now enqueues via the job queue (spec v4 §2) and
    // returns {item}, not {job} — progress shows up in the Queue view
    // (tabs/jobs.js), which already polls state.queue on its own cadence.
    await api(`/projects/${p.id}/reels/${el.dataset.reel}/render`, { method: "POST" });
    await pollQueue();
    setTab("jobs");
  });
}

window.TABS.reels = renderReels;
