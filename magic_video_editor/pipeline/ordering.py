"""Stage 5 — Narrative ordering: clips may be recorded out of order; the LLM
reads each clip's (kept) transcript and proposes the order that makes the
dialog flow. v1 orders at CLIP granularity and keeps in-clip chronology —
much more robust with local models than sentence-level shuffling.
The result is editable in the UI."""

from .. import llm, store


def _clip_summary_text(project: dict, clip_id: str, max_chars: int = 1200) -> str:
    kept = [s for s in project["sentences"] if s["clip_id"] == clip_id and s["kept"]]
    text = " ".join(s["text"] for s in kept)
    if len(text) > max_chars:
        text = text[: max_chars // 2] + " [...] " + text[-max_chars // 2 :]
    return text


def _clips_with_kept_sentences(project: dict) -> list[str]:
    """Camera clip ids, in file order, that currently have >=1 kept
    sentence. This is the ONE definition of "clips that belong in the EDL"
    -- build_edl, run(), and the add/remove invalidation hook all derive
    from it, so a clip can never be silently dropped or kept around after
    its sentences (or the clip itself) are gone."""
    kept_clip_ids = {s["clip_id"] for s in project.get("sentences", []) if s["kept"]}
    return [c["id"] for c in project["clips"] if c["role"] == "camera" and c["id"] in kept_clip_ids]


def reconcile_clip_order(project: dict) -> list[str]:
    """Reconcile project.get("clip_order") against reality: drop any id that
    no longer has kept sentences (stale/phantom -- e.g. left over from a
    project state that had a different clip set, the exact "62e6cae7"
    live-diagnosed bug where clip_order pointed at a clip that no longer
    existed and build_edl silently returned []), then APPEND any clip that
    has kept sentences but is missing from the order (a newly-added or
    re-imported clip), in file order. Falls back to all clips-with-kept (file
    order) if `order` would otherwise be empty. Never mutates `project` --
    callers that want the reconciled value persisted must assign/save it
    themselves. This makes an empty EDL while kept sentences exist
    impossible: every caller of build_edl is protected by this one
    reconciliation."""
    valid = _clips_with_kept_sentences(project)
    valid_set = set(valid)
    order = [cid for cid in (project.get("clip_order") or []) if cid in valid_set]
    order += [cid for cid in valid if cid not in order]
    if not order and valid:
        order = valid
    return order


def invalidate_after_clipset_change(project: dict) -> None:
    """Call whenever the clip SET changes (a clip added/imported, or
    removed) -- never on plain sentence/kept edits. The clip set changing
    is exactly what made "clip_order"/"edl" stale in the live bug (a
    project's clips were replaced but clip_order/edl and the order/render/
    reels stage badges kept reporting their previous, now-nonexistent,
    state). Mutates `project` in place; caller is responsible for the
    eventual store.save().

    Drops clip_order ids that no longer exist at all (a removed clip);
    stale-but-still-real ids and missing-but-kept ids are left for
    reconcile_clip_order/build_edl to sort out on next read, since kept-
    sentence status isn't settled yet right after an add (transcription
    hasn't run). Clears the cached edl so the next read rebuilds via
    build_edl, and un-dones the order/render/reels stage badges so the UI
    stops claiming stale-done for content that no longer matches.
    Deliberately does NOT touch transcribe/takes -- those are keyed
    per-clip and remain valid for every clip that's still present."""
    current_ids = {c["id"] for c in project["clips"]}
    order = project.get("clip_order") or []
    filtered = [cid for cid in order if cid in current_ids]
    if filtered != order:
        project["clip_order"] = filtered
    project["edl"] = None
    stages = project.get("stages", {})
    for stage in ("order", "render", "reels"):
        if stages.get(stage, {}).get("status") == "done":
            del stages[stage]


def run(log, project: dict) -> None:
    if not project.get("sentences"):
        raise RuntimeError("Run Take analysis first.")
    project["edl"] = None  # reordering invalidates any previously computed EDL

    # Defensive: never let a stale/phantom pre-existing clip_order (e.g. from
    # a clip set that no longer matches) leak into this run -- both branches
    # below always compute clip_order fresh from clip_ids regardless, but
    # discarding it up front means nothing in between can accidentally read
    # a bogus value off `project` while this function is executing.
    current_ids = {c["id"] for c in project["clips"]}
    if any(cid not in current_ids for cid in project.get("clip_order") or []):
        project["clip_order"] = []

    clip_ids = [
        c["id"]
        for c in project["clips"]
        if c["role"] == "camera"
        and any(s["clip_id"] == c["id"] and s["kept"] for s in project["sentences"])
    ]

    if len(clip_ids) <= 1:
        project["clip_order"] = clip_ids
        project["order_notes"] = "single clip — chronological"
        store.save(project)
        log("Single clip with content — nothing to reorder.")
        return

    if not llm.available():
        project["clip_order"] = clip_ids
        project["order_notes"] = "ollama unavailable — kept file order"
        store.save(project)
        log("Ollama not reachable; kept file order. Reorder manually in the Edit tab.")
        return

    listing = "\n\n".join(
        f"CLIP {i} ({store.get_clip(project, cid)['filename']}):\n"
        f"{_clip_summary_text(project, cid)}"
        for i, cid in enumerate(clip_ids)
    )
    from ..agents.agents import get_agent

    log(f"Asking the clip-order agent to order {len(clip_ids)} clips by narrative flow...")
    try:
        result = get_agent("clip_order").run_sync(listing).output
        order = [int(x) for x in result.order]
        if sorted(order) != list(range(len(clip_ids))):
            raise ValueError(f"not a permutation: {order}")
        project["clip_order"] = [clip_ids[i] for i in order]
        project["order_notes"] = result.notes[:300]
        log(f"Order: {[store.get_clip(project, c)['filename'] for c in project['clip_order']]}")
        log(f"Rationale: {project['order_notes']}")
    except (Exception, ValueError, KeyError) as e:
        project["clip_order"] = clip_ids
        project["order_notes"] = f"LLM ordering failed ({e}); kept file order"
        log(project["order_notes"])
    store.save(project)


def build_edl(project: dict) -> list[dict]:
    """Ordered render plan: kept sentences grouped into contiguous segments
    (per clip, merging small gaps), following clip_order."""
    from .. import config

    order = reconcile_clip_order(project)
    segments = []
    for cid in order:
        sents = sorted(
            (s for s in project["sentences"] if s["clip_id"] == cid and s["kept"]),
            key=lambda s: s["start"],
        )
        cur = None
        for s in sents:
            if cur and s["start"] - cur["end"] <= config.MERGE_GAP:
                cur["end"] = s["end"]
                cur["text"] += " " + s["text"]
            else:
                if cur:
                    segments.append(cur)
                cur = {
                    "clip_id": cid,
                    "start": s["start"],
                    "end": s["end"],
                    "text": s["text"],
                }
        if cur:
            segments.append(cur)

    dur = store.get_clip  # noqa: F841  (clip lookup used below)
    for seg in segments:
        clip = store.get_clip(project, seg["clip_id"])
        seg["start"] = max(0.0, seg["start"] - config.SEGMENT_PAD)
        seg["end"] = min(clip["info"]["duration"], seg["end"] + config.SEGMENT_PAD)
    return segments
