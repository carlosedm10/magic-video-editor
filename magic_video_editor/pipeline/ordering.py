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


def run(log, project: dict) -> None:
    if not project.get("sentences"):
        raise RuntimeError("Run Take analysis first.")
    project["edl"] = None  # reordering invalidates any previously computed EDL

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

    order = project.get("clip_order") or [
        c["id"] for c in project["clips"] if c["role"] == "camera"
    ]
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
