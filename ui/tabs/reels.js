/* Reels tab — scored short-form candidates: copywriter title + collapsible
   description + hashtags (spec v5 addendum "SEO copywriter"), a
   "Regenerate copy" action, an "Edit" button into the Reel Editor
   (ui/editor/reeleditor.js, spec v5 "Reel Editor"), and render 9:16. */

const _reelsExpandedDesc = new Set(); // reel ids whose description is expanded (kept across re-renders this session)

/* Field bug fix (2026-07-25): the multi-segment reel migration (spec v5.8b,
   reel["segments"] = [{clip_id,start,end,...}]) left this drawer only ever
   reading the legacy top-level r.clip_id/r.start/r.end. Those are still kept
   in sync with segments[0] by the backend's _sync_legacy_fields for the
   single-window case, but composed (multi-segment) reels' top-level fields
   only ever describe segment 0 too -- so "read segment 0, fall back to the
   legacy flat fields" is the one migrate-on-read helper every consumer here
   needs; never read r.clip_id/r.start/r.end directly. */
function _reelSeg0(r) {
  const s = (r.segments && r.segments[0]) || null;
  return {
    clip_id: s ? s.clip_id : r.clip_id,
    start: s ? s.start : r.start,
    end: s ? s.end : r.end,
  };
}

/* Poster + hover-preview support. Suggested (not yet rendered) reels have no
   r.path to point a <video> at, which is why the drawer used to fall back to
   a bare "Render 9:16" button with zero visual -- forcing a trip into the
   Reel Editor just to see what a candidate looks like. Instead we crop a
   single frame out of the clip's existing filmstrip sprite (magic_video_editor/
   pipeline/thumbs.py, already generated for the media bin/timeline) as a
   poster shown IMMEDIATELY, regardless of preview state.

   Reel preview render (spec v7.14): once the backend's "reel_previews"
   queue job has produced this reel's own low-res 9:16 render
   (r.preview_ready, GET /media/reel-preview/{reel_id}, Range-seekable), the
   card lazily spins up a real <video> against THAT on hover/click instead
   -- never the raw source clip (that was the original bug: HEVC/10-bit
   sources Chrome can't decode at all, and even the browser-safe proxy shows
   the wrong crop/framing/blur-background for this specific reel). While
   the preview hasn't been rendered yet, the poster shows a subtle
   "Generando previsualización…" badge instead of a video -- no player, dead
   or otherwise. Never more than 1-2 live decoders at rest either way (video
   elements are created on hover/click and torn down on mouseleave). */
const _reelsThumbCache = new Map(); // clip_id -> {meta,stripUrl,loading,failed}
function _reelsThumbEntry(pid, clipId) {
  let e = _reelsThumbCache.get(clipId);
  if (!e) {
    e = { meta: null, stripUrl: null, loading: true, failed: false };
    _reelsThumbCache.set(clipId, e);
    api(`/projects/${pid}/thumbs/${clipId}/meta`)
      .then((meta) => {
        e.meta = meta;
        e.stripUrl = `/api/projects/${pid}/thumbs/${clipId}/strip`;
      })
      .catch(() => { e.failed = true; })
      .finally(() => {
        e.loading = false;
        if (state.project?.id === pid) renderReels(); // reflow once, cache hit after
      });
  }
  return e;
}

/* Crops a single frame out of the filmstrip sprite to "cover" the poster box
   (fill it, cropping the landscape 16:9 tile's sides), centered horizontally
   on the frame. Sized against the element's OWN real layout box (clientWidth/
   Height, known once inserted — the card grid column width is fluid, so a
   hardcoded assumed box size would over/under-crop depending on viewport). */
function _reelsApplyPoster(el, entry, atTime) {
  if (!entry?.meta || !entry.stripUrl) return false;
  const { frame_w, frame_h, interval_s, count } = entry.meta;
  if (!frame_w || !frame_h || !count) return false;
  const boxW = el.clientWidth || 1;
  const boxH = el.clientHeight || Math.round((boxW * 16) / 9);
  const scale = boxH / frame_h;
  const frameW = frame_w * scale;
  const totalW = count * frameW;
  const idx = Math.min(count - 1, Math.max(0, Math.round((atTime || 0) / (interval_s || 1))));
  const x = -(idx * frameW) + (boxW / 2 - frameW / 2);
  el.style.backgroundImage = `url('${entry.stripUrl}')`;
  el.style.backgroundRepeat = "no-repeat";
  el.style.backgroundSize = `${totalW.toFixed(1)}px ${boxH}px`;
  el.style.backgroundPosition = `${x.toFixed(1)}px 0`;
  return true;
}

/* Defensive against a real backend data bug observed live against project
   c7642fc7755e: magic_video_editor/pipeline/reels.py does `list(copy.get("hashtags") or [])`
   but magic_video_editor/pipeline/copywriter.py's copy_for_reel returns "hashtags" as a
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
    const segCount = r.segments?.length || 1;
    const composedBadge = r.composed
      ? ` <span class="pill main">Compuesto · ${segCount} segmentos</span>`
      : "";
    return `
    <div class="card">
      <div><span class="score">${r.score}</span> · #${r.rank} <b>${esc(r.title || "Untitled")}</b>${composedBadge}</div>
      <div class="dim">${r.duration}s · hook ${r.hook} · standalone ${r.self_contained} · payoff ${r.payoff}</div>
      ${hasDesc ? `
        <button class="btn small" data-desc-toggle="${r.id}" style="margin:6px 0 4px">
          ${expanded ? '<i data-lucide="chevron-down"></i> Hide description' : '<i data-lucide="chevron-right"></i> Show description'}</button>
        <div class="dim" data-desc="${r.id}" style="white-space:pre-wrap;margin-bottom:6px" ${expanded ? "" : "hidden"}>${esc(r.description)}</div>
      ` : `<div class="dim" style="margin:6px 0">${esc((r.text || "").slice(0, 160))}…</div>`}
      ${(() => {
        const tags = _reelsValidHashtags(r.hashtags);
        return tags.length ? `<div class="chip-row">${tags.map((h) => `<span class="pill">${esc(h.startsWith("#") ? h : "#" + h)}</span>`).join("")}</div>` : "";
      })()}
      <div class="row" style="margin:8px 0">
        <button class="btn small" data-copy="${r.id}"><i data-lucide="copy"></i> Copy</button>
        <button class="btn small" data-regen="${r.id}"><i data-lucide="refresh-cw"></i> Regenerate copy</button>
        <button class="btn small" data-edit="${r.id}"><i data-lucide="pencil"></i> Edit</button>
      </div>
      ${(() => {
        // Field bug fix (spec v7.14 addendum, SEAM 1): a rendered reel
        // (r.path set, r.status === "rendered") used to point this card's
        // <video> at GET /media/file?path=<absolute export path>. Exports
        // land under settings.export_dir (~/Movies/... by default), which
        // is OUTSIDE the project dir -- media_file() 403s anything outside
        // it, so the player was dead (MediaError code 4) exactly like the
        // original bug this whole feature exists to fix. Product decision:
        // the drawer ALWAYS plays the low-res 9:16 preview (never the
        // export), full quality is an export-only concern. So this no
        // longer branches on r.path at all -- every reel, rendered or not,
        // goes through the same poster + reel-preview-endpoint path below;
        // a rendered reel differs only in the badge (shows when it was
        // last rendered instead of "Generando previsualización…" once its
        // own preview_ready flips true, which happens independently).
        const seg0 = _reelSeg0(r);
        const thumbEntry = seg0.clip_id ? _reelsThumbEntry(p.id, seg0.clip_id) : null;
        const hasPoster = !!(thumbEntry?.meta && thumbEntry.stripUrl);
        // Reel preview render (spec v7.14): a suggestion has no rendered
        // file and pointing a player at the raw source clip is exactly the
        // dead-<video> bug this fixes (HEVC/10-bit sources Chrome can't
        // decode, and even the H.264 proxy shows the wrong framing/crop for
        // this reel). The filmstrip poster shows immediately either way;
        // once the backend's "reel_previews" queue job has produced a
        // low-res 9:16 render (r.preview_ready), hovering/clicking plays
        // THAT instead of anything source-derived (wired below). Until
        // then, a subtle pending badge -- refreshProject() (triggered by
        // the existing 2s queue poll in ui/core.js once "reel_previews"
        // stops running) re-renders this tab and flips it once ready.
        const previewReady = !!r.preview_ready;
        return `
        <div class="reel-poster" data-reel-poster="${r.id}" data-clip="${esc(seg0.clip_id || "")}"
             data-start="${seg0.start ?? 0}" data-preview-ready="${previewReady ? "1" : "0"}"
             style="position:relative;width:100%;aspect-ratio:9/16;border-radius:8px;overflow:hidden;
                    background:#111;cursor:${previewReady ? "pointer" : "default"}">
          ${hasPoster ? "" : `<div class="dim" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:8px">${thumbEntry?.loading ? "Loading preview…" : "No preview"}</div>`}
          ${previewReady ? `<div class="reel-poster-play" style="position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.55);
               border-radius:999px;padding:4px 6px;line-height:0;pointer-events:none">
            <i data-lucide="play"></i>
          </div>` : `<div class="pill" style="position:absolute;left:6px;top:6px;opacity:.85">Generando previsualización…</div>`}
          ${r.status === "rendered" ? `<div class="pill" style="position:absolute;left:6px;bottom:6px;opacity:.85">Rendered${r.rendered_at ? " " + esc(r.rendered_at) : ""}</div>` : ""}
        </div>
        <button class="btn primary small" data-reel="${r.id}" style="margin-top:6px;width:100%">${r.status === "rendered" ? "Re-render 9:16" : "Render 9:16"}</button>`;
      })()}
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
    const original = el.innerHTML;
    el.innerHTML = ok ? '<i data-lucide="check"></i> Copied' : "Copy failed";
    refreshIcons();
    setTimeout(() => { el.innerHTML = original; refreshIcons(); }, 1500);
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
      el.innerHTML = '<i data-lucide="refresh-cw"></i> Regenerate copy';
      refreshIcons();
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

  // Hover(-or-click)-play preview for not-yet-rendered reels. IMPORTANT:
  // this now ONLY plays the reel's own low-res 9:16 preview render (spec
  // v7.14, GET /media/reel-preview/{id}) -- never the raw source clip. The
  // old approach (CSS-cropping the full clip's preview proxy) is exactly
  // the bug being fixed: it showed the wrong framing (no transform/blur
  // background) and, before the H.264 proxy existed, a dead <video> with
  // an undecodable HEVC/10-bit source. Reels without a ready preview yet
  // get no hover video at all -- just the poster + pending badge above; a
  // grid of ~20 suggestion cards never holds more than 1-2 live decoders at
  // once either way (created on hover, torn down on mouseleave).
  document.querySelectorAll("[data-reel-poster]").forEach((el) => {
    const clipId = el.dataset.clip;
    const start = parseFloat(el.dataset.start) || 0;
    const previewReady = el.dataset.previewReady === "1";
    const reelId = el.dataset.reelPoster;

    // Poster frame: applied against the element's real (post-layout) box
    // size — see _reelsApplyPoster's docstring for why this can't be baked
    // into the HTML string above.
    const thumbEntry = clipId ? _reelsThumbCache.get(clipId) : null;
    _reelsApplyPoster(el, thumbEntry, start);

    if (!previewReady) return; // nothing decodable for this reel yet

    // withSound=false (hover): muted, looping, autoplay-safe ambient
    // preview. withSound=true (click): unmuted, the explicit "let me
    // actually watch/listen to this" gesture — matches how the drawer
    // previews behaved before (hover=silent glance, click=play w/ sound).
    const startPreview = (withSound) => {
      let v = el.querySelector("video");
      if (v) {
        if (withSound) { v.muted = false; v.play().catch(() => {}); }
        return;
      }
      v = document.createElement("video");
      v.muted = !withSound;
      v.loop = true;
      v.playsInline = true;
      v.preload = "auto";
      v.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:cover";
      v.src = `/api/projects/${p.id}/media/reel-preview/${reelId}`;
      v.addEventListener("loadedmetadata", () => { v.play().catch(() => {}); }, { once: true });
      el.appendChild(v);
    };
    const stopPreview = () => {
      const v = el.querySelector("video");
      if (v) { v.pause(); v.remove(); }
    };
    el.addEventListener("mouseenter", () => startPreview(false));
    el.addEventListener("mouseleave", stopPreview);
    el.addEventListener("click", () => {
      const v = el.querySelector("video");
      if (v && !v.muted) stopPreview(); else startPreview(true);
    });
  });

  refreshIcons();
}

window.TABS.reels = renderReels;
