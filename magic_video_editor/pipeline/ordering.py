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
    for stage in ("order", "paragraphs", "render", "reels"):
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


def _globals_fallback_preset() -> dict:
    from .. import config

    return {
        "head_pad_s": config.SEGMENT_PAD,
        "merge_gap_s": config.MERGE_GAP,
        "tail_pad_s": config.SEGMENT_PAD,
    }


def resolve_pacing_preset(project: dict) -> dict:
    """Resolve the effective cutting-rhythm ("ritmo") knobs for `project`
    (owner feature, 2026-07-26 -- manual-vs-auto comparison found the auto
    cut too aggressive on head lead-in, mid-paragraph micro-breaths, and
    tail). `project["pacing"]` is one of config.PACING_PRESETS' keys
    ("tight"/"natural"/"airy" -- config.DEFAULT_PACING is "natural", tuned to
    match the human reference cut: keep some breathing room, don't split a
    1-2s breath mid-paragraph). Returns a dict with head_pad_s/merge_gap_s/
    tail_pad_s.

    A project with NO pacing set at all (the common case for every project
    that existed before this feature, and any caller/test that builds a
    project dict without the field -- e.g. scripts/test_intra_clip_order.py)
    falls back to a preset built from the bare config.SEGMENT_PAD/MERGE_GAP
    globals, i.e. IDENTICAL numeric behavior to before this feature existed --
    "fall back to globals if pacing unset", verbatim. `config.DEFAULT_PACING`
    ("natural") is the label a fresh project/the Settings UI defaults to
    going forward (see settings.py's "pacing" default) for an EXPLICIT
    selection; it is deliberately NOT silently substituted for a project that
    has no pacing field, so existing padding/merge behavior never shifts out
    from under a project that never opted in. An explicit but unrecognized
    value also falls back to the bare globals (never raises -- this is a pure
    read path, validation lives in api/projects.py's PATCH handler)."""
    key = project.get("pacing")
    if key is None:
        return _globals_fallback_preset()
    from .. import config

    preset = config.PACING_PRESETS.get(key)
    return preset if preset is not None else _globals_fallback_preset()


def _normalize_for_closer_match(text: str) -> str:
    """Lower + strip accents + strip everything but letters/digits/spaces,
    collapse whitespace -- for matching a segment's full text against
    config.TAIL_TRIM_CLOSERS. Deliberately whole-text (not substring): a
    closer phrase embedded inside a longer, substantive sentence must NOT
    trigger a trim."""
    import re
    import unicodedata

    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def _is_low_content_closer(text: str) -> bool:
    """True if `text` normalizes to (or ends in, after stripping a leading
    filler like "bueno"/"vale") one of config.TAIL_TRIM_CLOSERS, or is empty
    after normalization. Conservative: exact whole-string match against the
    closer list, not a substring/fuzzy match, so a real sentence that merely
    mentions e.g. "gracias" mid-content is never caught."""
    from .. import config

    norm = _normalize_for_closer_match(text)
    if not norm:
        return True
    return norm in config.TAIL_TRIM_CLOSERS


def _is_hallucination_loop(text: str) -> bool:
    """True if `text` looks like a whisper end-of-clip hallucination: the
    same single short token repeated over and over (e.g. "gracias gracias
    gracias gracias" or "si si si si si si"), with little else. Requires at
    least 4 repeats of one token and that token accounting for essentially
    the whole segment, so a legitimate sentence that merely repeats a word
    once or twice for emphasis is never caught."""
    norm = _normalize_for_closer_match(text)
    words = norm.split()
    if len(words) < 4:
        return False
    distinct = set(words)
    if len(distinct) > 2:
        return False
    from collections import Counter

    counts = Counter(words)
    most_common_word, most_common_n = counts.most_common(1)[0]
    return most_common_n >= 4 and most_common_n / len(words) >= 0.8


def _trim_trailing_low_content(segments: list[dict]) -> list[dict]:
    """TAIL = "cortar en la ultima frase con sentido" (owner feature,
    2026-07-26): drop trailing segments whose text is a low-content sign-off/
    goodbye, empty/garbage, or a repeated-token whisper hallucination, so the
    final EDL ends on the last sentence with real content -- regardless of
    `pacing`. Conservative by construction: only pops segments matching
    _is_low_content_closer/_is_hallucination_loop, working backwards from the
    end, and ALWAYS leaves at least one segment standing (never trims a
    project down to nothing, and never inspects/trims anything but a
    contiguous trailing run). No-ops entirely when config.TAIL_TRIM_ENABLED
    is False."""
    from .. import config

    if not config.TAIL_TRIM_ENABLED or not segments:
        return segments
    kept = list(segments)
    while len(kept) > 1:
        text = kept[-1].get("text", "")
        if _is_low_content_closer(text) or _is_hallucination_loop(text):
            kept.pop()
        else:
            break
    return kept


def build_edl(project: dict, paragraph_break_after: set[str] | None = None) -> list[dict]:
    """Ordered render plan: kept sentences grouped into contiguous segments
    (per clip, merging small gaps), following clip_order.

    `paragraph_break_after` (owner feature, 2026-07-25): a set of sentence
    ids after which a NON-DESTRUCTIVE paragraph/topic-change boundary must be
    kept -- pipeline/paragraphs.py's detection pass. When a would-be-merged
    sentence's PREVIOUS sentence id is in this set, the merge is suppressed
    (a segment boundary is forced there instead) even though the gap is
    small enough to normally merge; content, order, and timestamps are
    otherwise identical to a run with no breaks -- this only ever SPLITS,
    never re-merges or re-orders anything. Defaults to reading
    project["paragraph_break_after_ids"] so every existing caller (api/edl.py,
    pipeline/render.py) automatically picks up whatever paragraphs.run last
    computed without having to pass anything explicitly; pass an explicit set
    (e.g. in tests) to bypass the project entirely. The resulting segment
    that starts right after such a boundary is tagged `paragraph_break: True`
    -- a hint for the UI/render, never auto-applied (the segment's own
    `transition` stays whatever it already was / defaults to "none").

    Cutting rhythm ("ritmo", owner feature, 2026-07-26): `project["pacing"]`
    (see resolve_pacing_preset) supplies merge_gap_s (used for the
    adjacent-merge decision below, in place of the old flat config.MERGE_GAP)
    and head_pad_s/tail_pad_s (used ONLY for the padding of the very first and
    very last segment of the whole EDL -- the "head lead-in" and "tail"
    dimensions the owner tuned; every other segment's padding is still the
    original config.SEGMENT_PAD, unchanged). A paragraph break still forces a
    split regardless of merge_gap_s -- checked exactly as before. After
    assembly, a conservative tail-trim pass (_trim_trailing_low_content) drops
    trailing low-content sign-off/hallucination segments so the cut ends on
    the last MEANINGFUL sentence, independent of pacing."""
    from .. import config

    if paragraph_break_after is None:
        paragraph_break_after = set(project.get("paragraph_break_after_ids") or [])

    pacing = resolve_pacing_preset(project)
    merge_gap = pacing["merge_gap_s"]

    order = reconcile_clip_order(project)
    segments = []
    for cid in order:
        sents = sorted(
            (s for s in project["sentences"] if s["clip_id"] == cid and s["kept"]),
            key=lambda s: s["start"],
        )
        cur = None
        cur_last_id = None
        for s in sents:
            if (
                cur
                and s["start"] - cur["end"] <= merge_gap
                and cur_last_id not in paragraph_break_after
            ):
                cur["end"] = s["end"]
                cur["text"] += " " + s["text"]
                cur_last_id = s["id"]
            else:
                if cur:
                    segments.append(cur)
                cur = {
                    "clip_id": cid,
                    "start": s["start"],
                    "end": s["end"],
                    "text": s["text"],
                    "paragraph_break": cur_last_id is not None
                    and cur_last_id in paragraph_break_after,
                }
                cur_last_id = s["id"]
        if cur:
            segments.append(cur)

    last_idx = len(segments) - 1
    for i, seg in enumerate(segments):
        clip = store.get_clip(project, seg["clip_id"])
        pad_start = pacing["head_pad_s"] if i == 0 else config.SEGMENT_PAD
        pad_end = pacing["tail_pad_s"] if i == last_idx else config.SEGMENT_PAD
        seg["start"] = max(0.0, seg["start"] - pad_start)
        seg["end"] = min(clip["info"]["duration"], seg["end"] + pad_end)

    return _trim_trailing_low_content(segments)
