"""Incremental clip placement (spec v7.3 "Incremental clip addition").

When a clip is added to a project whose pipeline already completed,
magic_video_editor/pipeline/ingest.py enqueues a queue item `analyze_clip:<clip_id>`
(see `_enqueue_analyze_for_new_clips`). `run_analyze_clip` below -- registered
into magic_video_editor.queue as the "analyze_clip:*" runner -- handles it:

1. Bring THAT clip only up to the same state every other clip is in (import/
   proxy/thumbs/wav via the existing idempotent stage runners, transcription
   via a narrow reuse of transcribe.py's backend), then run the per-clip
   cleaner+sequencer passes on its sentences alone and append them to
   project["sentences"]. No other clip's data is read for judging cuts and
   none of it is written.
2. Ask the `clip_placement` agent where the new clip fits into the existing
   narrative (video topic + ordered one-line summaries of the existing kept
   clips + the new clip's kept transcript) and turn the verdict into a
   "placement" or "duplicate_clip" suggestion (project["suggestions"]).

Suggestions are never auto-applied -- owner-locked decision, see
docs/PLATFORM-SPEC.md v7: "new-clip insertion = PROPOSE + 1-click accept
(never auto)". Accepting one (see magic_video_editor/api/suggestions.py) calls
`apply_placement` below, which splices the clip into project["clip_order"]
and inserts its EDL segments at the corresponding boundary WITHOUT touching
any other clip's segments."""

import uuid

from .. import ffmpeg_utils, llm, queue, store
from . import ingest, ordering, takes, thumbs
from .transcribe import _transcribe_faster, _transcribe_mlx


def _transcribe_one(log, clip: dict) -> None:
    """Transcribe exactly this clip. Deliberately NOT transcribe.run(), which
    loops every untranscribed clip in the project AND (when the project
    declares >1 speaker) triggers a full-project speaker re-diarization pass
    afterwards that reclusters and relabels every OTHER clip's segments --
    exactly the cross-clip mutation this incremental path must never cause.
    Mirrors transcribe.run's own per-clip transcription body, minus the loop
    and the diarization call."""
    if clip.get("transcript") or not clip.get("wav"):
        return
    try:
        import mlx_whisper  # noqa: F401

        backend = _transcribe_mlx
    except ImportError:
        backend = _transcribe_faster
    log(f"Transcribing {clip['filename']} ({clip.get('info', {}).get('duration', 0):.0f}s)...")
    result = backend(clip["wav"])
    clip["transcript"] = {"segments": result["segments"]}
    clip["language"] = result["language"]


def _analyze_new_sentences(log, project: dict, clip: dict) -> list[dict]:
    """Per-clip cleaner + sequencer ONLY (spec 7.3 point 2) -- no context
    check, no cross-clip dedup, no fragment-drop pass: those full-pipeline
    passes read (and in the dedup case, cut) sentences from OTHER clips,
    which this incremental, single-clip path must never touch."""
    sentences = takes._sentences_from_clip(clip)
    if not sentences:
        return []

    wav = ffmpeg_utils.load_wav_mono(clip["wav"]) if clip.get("wav") else None
    for idx, s in enumerate(sentences):
        s["score"], s["why"] = takes._score(s, wav, idx)
        s["kept"] = True
        s["dup_group"] = None
        s["reason"] = ""

    if llm.available():
        cleaner_cut_ids = takes._transcript_cleanup(log, sentences, project)
        sequencer_cut_ids = takes._take_sequencer_clip(log, sentences, project)
    else:
        log("clip cleaner/sequencer skipped (ollama unavailable)")
        cleaner_cut_ids, sequencer_cut_ids = set(), set()

    for s in sentences:
        if s["id"] in cleaner_cut_ids:
            s["kept"] = False
            s["reason"] = "restart/abandoned take (AI)"
        elif s["id"] in sequencer_cut_ids:
            s["kept"] = False
            s["reason"] = "stuck take run (AI)"

    for s in sentences:
        s.pop("words", None)  # words stay in the clip transcript; keep project.json light
    return sentences


def _clip_segments(project: dict, clip_id: str) -> list[dict]:
    """Render-plan segments for ONE clip's currently-kept sentences, built by
    reusing ordering.build_edl (not reimplementing its gap-merge/pad logic)
    on a scratch copy of `project` whose clip_order is just this one clip.
    build_edl only reads project["sentences"]/["clip_order"] and
    store.get_clip(project, ...) -- a shallow-copied project still carries
    the full "clips" list, so clip lookups resolve exactly as normal."""
    scratch = {**project, "clip_order": [clip_id]}
    return ordering.build_edl(scratch)


def _edl_insert_index(project: dict, placement_after_clip_index: int) -> int:
    """EDL position right after the last existing segment belonging to any
    clip at or before `placement_after_clip_index` in the CURRENT clip_order
    -- robust to clips that contributed zero EDL segments (fully cut, or
    hand-deleted in Studio). Yields 0 ("insert at the start") when nothing
    qualifies, matching placement_after_clip_index == -1."""
    order = project.get("clip_order") or []
    order_pos = {cid: i for i, cid in enumerate(order)}
    edl = project.get("edl") or []
    insert_at = 0
    for i, seg in enumerate(edl):
        pos = order_pos.get(seg["clip_id"])
        if pos is not None and pos <= placement_after_clip_index:
            insert_at = i + 1
    return insert_at


def _numbered_clip_listing(project: dict) -> tuple[list[str], list[str]]:
    """Ordered one-line summaries per existing clip (reusing
    ordering._clip_summary_text), plus the clip_order they're numbered
    against -- callers need both to interpret the agent's indices."""
    order = project.get("clip_order") or []
    lines = [
        f"CLIP {i}: {ordering._clip_summary_text(project, cid) or '(no kept content)'}"
        for i, cid in enumerate(order)
    ]
    return lines, order


def _ask_placement(log, project: dict, clip: dict, new_sentences: list[dict]) -> dict | None:
    """Runs the clip_placement agent and returns an open suggestion dict, or
    None when there's nothing to propose (no existing narrative yet, ollama
    down, the clip has no kept content, or the agent call failed) -- all
    fail-open, matching every other agent pass in this pipeline."""
    lines, order = _numbered_clip_listing(project)
    if not order:
        log("No existing narrative order yet; skipping placement suggestion.")
        return None
    if not llm.available():
        log("clip_placement skipped (ollama unavailable)")
        return None

    kept_text = " ".join(
        s["text"] for s in sorted(new_sentences, key=lambda s: s["start"]) if s["kept"]
    )
    if not kept_text:
        log("New clip has no kept content after cleanup; skipping placement suggestion.")
        return None

    topic = project.get("topic") or ""
    topic_line = f'Video topic: "{topic}"\n\n' if topic else ""
    prompt = (
        f"{topic_line}Existing clips, in narrative order:\n"
        + "\n".join(lines)
        + f"\n\nNEW CLIP transcript (kept sentences):\n{kept_text}"
    )

    from ..agents.agents import get_agent

    log("Asking the clip_placement agent where the new clip fits...")
    try:
        result = get_agent("clip_placement").run_sync(prompt).output
    except Exception as e:
        log(f"clip_placement failed, skipping: {e}")
        return None

    n = len(order)
    after = max(-1, min(result.placement_after_clip_index, n - 1))
    dup = result.duplicate_of_clip_index
    dup = dup if -1 <= dup < n else -1
    kind = "duplicate_clip" if dup != -1 else "placement"

    return {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "clip_id": clip["id"],
        "placement_after_clip_index": after,
        "duplicate_of_clip_index": dup,
        "confidence": max(1, min(5, result.confidence)),
        "message": (result.message or "")[:300],
        "proposed_action": "place",
        "sentence_ids": [],
        "status": "open",
    }


def run_analyze_clip(log, project: dict, payload: dict) -> None:
    """KIND_RUNNERS callable for queue kind "analyze_clip:<clip_id>"."""
    clip_id = payload.get("clip_id") or payload["_kind"].split(":", 1)[1]
    clip = store.get_clip(project, clip_id)

    log(f"Analyzing new clip {clip['filename']}...")
    # Idempotent full-project stage runners -- they only act on clips missing
    # info/wav/proxy/thumbs, which for a project whose pipeline already
    # completed means exactly this new clip (every other clip already has
    # them and is skipped as a no-op).
    ingest.run(log, project)
    thumbs.run(log, project)
    _transcribe_one(log, clip)
    store.save(project)

    if not clip.get("transcript"):
        log(f"{clip['filename']}: no audio to transcribe; skipping placement analysis.")
        return

    new_sentences = _analyze_new_sentences(log, project, clip)
    if new_sentences:
        project.setdefault("sentences", []).extend(new_sentences)
        store.save(project)
    kept = sum(1 for s in new_sentences if s["kept"])
    log(f"{clip['filename']}: {kept}/{len(new_sentences)} sentence(s) kept.")

    suggestion = _ask_placement(log, project, clip, new_sentences)
    if suggestion:
        project.setdefault("suggestions", []).append(suggestion)
        store.save(project)
        log(f"Placement suggestion created ({suggestion['kind']}).")


def apply_placement(project: dict, suggestion: dict) -> None:
    """Splice suggestion["clip_id"] into project["clip_order"] and
    project["edl"] at the boundary implied by placement_after_clip_index,
    WITHOUT touching any other clip's segments (spec 7.3: "existing edits to
    other segments preserved -- only insert, never reshuffle"). Used for both
    "placement" and "duplicate_clip" acceptance -- accepting a duplicate
    places it anyway, via the exact same splice."""
    clip_id = suggestion["clip_id"]
    order = list(project.get("clip_order") or [])
    if clip_id in order:
        return  # already placed (double-accept safety)

    n = len(order)
    after = max(-1, min(suggestion.get("placement_after_clip_index", -1), n - 1))

    if project.get("edl") is None:
        project["edl"] = ordering.build_edl(project) if project.get("sentences") else []
    insert_at = _edl_insert_index(project, after)
    new_segments = _clip_segments(project, clip_id)
    for seg in new_segments:
        seg.setdefault("transition", {"type": "none", "duration": 0.5})

    edl = list(project["edl"])
    project["edl"] = edl[:insert_at] + new_segments + edl[insert_at:]

    order.insert(after + 1, clip_id)
    project["clip_order"] = order


queue.register_runner("analyze_clip:*", run_analyze_clip)
