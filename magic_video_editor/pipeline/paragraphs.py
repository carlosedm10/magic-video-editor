"""Stage 5b — Paragraph-break detection (owner feature, 2026-07-25):
NON-DESTRUCTIVE cuts that remove no content -- they just mark a segment
boundary where the conversation changes topic/paragraph ("punto y aparte",
i.e. a genuine new paragraph, NEVER every "punto y seguido"/sentence end).
These are the spots where an editor would drop a transition, an intro, or an
effect.

Runs AFTER takes + order, over the KEPT sentences, windowed per clip (same
sliding-window shape as takes.py's take_sequencer/context_check). Only
records WHICH sentence-boundaries are genuine paragraph breaks
(project["paragraph_break_after_ids"]); it never touches project["sentences"]
or project["clip_order"] itself. ordering.build_edl reads that id set (see
its `paragraph_break_after` parameter) and forces a segment boundary there
instead of merging across it -- content, order, and timestamps stay exactly
what they'd otherwise be; the new junction is tagged `paragraph_break: True`
so the UI can suggest a transition there, and its `transition.type` stays
"none" (a suggestion, never auto-applied).

Fail-open throughout, matching every other LLM pass in takes.py: ollama down
or the feature disabled just means no breaks get recorded (falls back to
today's EDL, byte-for-byte)."""

from .. import config, llm, store
from . import ordering


def _kept_sentences_by_clip(project: dict) -> dict[str, list[dict]]:
    """Camera clips' kept sentences, per clip, sorted by start -- the SAME
    ordering build_edl itself groups by, so the local window numbering here
    lines up with what will actually be merged into segments."""
    order = ordering.reconcile_clip_order(project)
    by_clip: dict[str, list[dict]] = {}
    for cid in order:
        sents = sorted(
            (s for s in project["sentences"] if s["clip_id"] == cid and s["kept"]),
            key=lambda s: s["start"],
        )
        if sents:
            by_clip[cid] = sents
    return by_clip


def _window_breaks(log, window: list[dict], project: dict) -> list[dict]:
    """Ask the paragraph_break agent for boundaries inside this window.
    Returns a list of {sentence_id, confidence, reason} for boundaries
    STRICTLY interior to the window (never the last sentence -- there is
    nothing after it in this window to judge). Fail-open: on any error, log
    and return no breaks for this window only."""
    from ..agents.agents import get_agent
    from .speakers import speaker_prefix

    numbered = "\n".join(
        f'{i + 1}: "{speaker_prefix(project, s)}{s["text"]}"' for i, s in enumerate(window)
    )
    try:
        result = get_agent("paragraph_break").run_sync(
            f"Numbered sentences from one clip, in order:\n{numbered}"
        ).output
        breaks: list[dict] = []
        for b in result.breaks:
            idx = b.after_id - 1
            # Strictly interior: after_id must name a real sentence AND
            # leave at least one more sentence after it in this window --
            # a boundary at the very last sentence is a window-edge
            # artifact, not a judged boundary (the sliding overlap will
            # give a LATER window the chance to see it as interior instead).
            if 0 <= idx < len(window) - 1:
                breaks.append(
                    {
                        "sentence_id": window[idx]["id"],
                        "confidence": b.confidence,
                        "reason": b.reason,
                    }
                )
        return breaks
    except Exception as exc:
        log(f"paragraph_break window failed, skipping: {exc}")
        return []


def _detect_clip(log, clip_sentences: list[dict], project: dict) -> set[str]:
    """Slide a window over one clip's kept sentences and union the
    high-confidence break-after sentence ids the paragraph_break agent
    flags across all (overlapping) windows."""
    if len(clip_sentences) < 2:
        return set()
    break_ids: set[str] = set()
    size = config.PARAGRAPH_BREAK_WINDOW_SIZE
    overlap = config.PARAGRAPH_BREAK_WINDOW_OVERLAP
    step = max(1, size - overlap)
    i = 0
    n = len(clip_sentences)
    while i < n:
        window = clip_sentences[i : i + size]
        for b in _window_breaks(log, window, project):
            if b["confidence"] >= config.PARAGRAPH_BREAK_MIN_CONFIDENCE:
                break_ids.add(b["sentence_id"])
        if i + size >= n:
            break
        i += step
    return break_ids


def run(log, project: dict) -> None:
    if not project.get("sentences"):
        raise RuntimeError("Run Take analysis first.")

    if not config.PARAGRAPH_BREAK_ENABLED:
        project["paragraph_break_after_ids"] = []
        project["edl"] = None
        store.save(project)
        log("Paragraph-break detection disabled (config.PARAGRAPH_BREAK_ENABLED) — skipping.")
        return

    if not llm.available():
        project["paragraph_break_after_ids"] = []
        project["edl"] = None
        store.save(project)
        log("Ollama not reachable; skipping paragraph-break detection.")
        return

    by_clip = _kept_sentences_by_clip(project)
    if not by_clip:
        project["paragraph_break_after_ids"] = []
        project["edl"] = None
        store.save(project)
        log("No kept sentences — nothing to check for paragraph breaks.")
        return

    log("Looking for paragraph/topic-change boundaries (conservative, suggestion-only)...")
    break_ids: set[str] = set()
    for clip_sentences in by_clip.values():
        break_ids |= _detect_clip(log, clip_sentences, project)

    project["paragraph_break_after_ids"] = sorted(break_ids)
    # A changed break set changes where build_edl will (and won't) merge --
    # invalidate the cached EDL so the next read/render rebuilds with it,
    # same as every other stage that can move segment boundaries.
    project["edl"] = None
    store.save(project)
    if break_ids:
        log(f"Found {len(break_ids)} paragraph-break point(s) — marked as suggested cuts.")
    else:
        log("No confident paragraph/topic changes found.")
