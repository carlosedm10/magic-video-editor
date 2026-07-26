"""Stage 4 — Take analysis: split transcripts into sentences, find repeated
takes of the same line across all clips, score each take, keep the best.

Scoring is heuristic v1 (transcript disfluencies + completeness + loudness
stability from the wav), with an optional LLM tiebreak for close calls.
"""

import re
import uuid

import numpy as np
from rapidfuzz import fuzz

from .. import config, ffmpeg_utils, llm, settings, store
from . import token_budget
from .speakers import speaker_prefix

FILLERS = re.compile(
    r"\b(uh+|um+|erm+|eh+|mmm+|hmm+|like,|you know,|o sea,|este,|pues,|eeh+|ehm+)\b",
    re.IGNORECASE,
)
SENT_END = re.compile(r"[.!?…]$")
PAUSE_SPLIT = 0.9  # seconds of silence between words that force a sentence break


def _sentences_from_clip(clip: dict) -> list[dict]:
    """Group whisper words into sentences by punctuation and long pauses."""
    words: list[dict] = []
    for seg in clip["transcript"]["segments"]:
        words.extend(seg["words"])
    sentences, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 99
        text_so_far = " ".join(x["w"] for x in cur)
        if SENT_END.search(w["w"]) or gap > PAUSE_SPLIT or len(cur) >= 60:
            sentences.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "clip_id": clip["id"],
                    "start": round(cur[0]["s"], 3),
                    "end": round(cur[-1]["e"], 3),
                    "text": text_so_far,
                    "words": cur,
                }
            )
            cur = []
    if cur:
        sentences.append(
            {
                "id": uuid.uuid4().hex[:8],
                "clip_id": clip["id"],
                "start": round(cur[0]["s"], 3),
                "end": round(cur[-1]["e"], 3),
                "text": " ".join(x["w"] for x in cur),
                "words": cur,
            }
        )
    return sentences


def _norm(text: str) -> str:
    t = FILLERS.sub(" ", text.lower())
    t = re.sub(r"[^\w\sáéíóúüñàèìòùç]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _normalize_exact(text: str) -> str:
    """Normalize for the EXACT-repeat pre-pass only: lowercase, drop
    punctuation entirely, collapse whitespace -- enough so "Bueno,
    empezamos." and "bueno empezamos" match. Deliberately NOT `_norm` above:
    `_norm` also strips filler words for the fuzzy/LLM passes, which would
    make this an approximate match. Exact-repeat must stay exact-only —
    forgive case/punctuation/whitespace, never forgive a different word."""
    t = text.strip().lower()
    t = re.sub(r"[^\w\sáéíóúüñàèìòùç]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _cut_exact_repeat_run(
    log,
    run: list[dict],
    key: str,
    cut_ids: set[str],
    keeper_ids: set[str] | None = None,
) -> None:
    keeper = run[-1]
    if keeper_ids is not None:
        keeper_ids.add(keeper["id"])
    for s in run[:-1]:
        cut_ids.add(s["id"])
    log(
        f'exact repeat: "{key}" said {len(run)}x in a row in clip '
        f'{keeper["clip_id"]} -- keeping the last take (id={keeper["id"]}), '
        f"cutting {len(run) - 1} earlier one(s) as blooper(s)"
    )


def _exact_repeat_dedup_clip(
    log, clip_sentences: list[dict], keeper_ids: set[str] | None = None
) -> set[str]:
    """Deterministic, LLM-free pre-pass (owner heuristic, 2026-07-25): "si se
    ha dicho EXACTAMENTE lo mismo DOS VECES SEGUIDAS, quedate con la ULTIMA."
    Walks one clip's sentences in time order (never crosses clip boundaries —
    that's `_cross_clip_dedup`'s job). A run of STRICTLY CONSECUTIVE sentences
    whose `_normalize_exact` text is identical is an unambiguous flub-then-
    redo: keep only the last occurrence, cut every earlier one. Exact-only —
    no fuzzy matching, and this intentionally does NOT reach past a single
    adjacent gap (that's the near-duplicate fuzzy grouping further down in
    `run()`), so a different sentence between two identical ones breaks the
    run and both survive this pass untouched.

    `keeper_ids`, if given, is populated (in place) with the id of the
    surviving (last) sentence of every cut run -- Fix 1 (2026-07-26): callers
    use this to protect that survivor from ever being cut by a LATER pass
    (transcript_cleaner/take_sequencer/context_check/dedup_judge), which was
    the root cause of a duplicated CTA line losing every delivery."""
    cut_ids: set[str] = set()
    if len(clip_sentences) < 2:
        return cut_ids

    run = [clip_sentences[0]]
    run_key = _normalize_exact(clip_sentences[0]["text"])
    for s in clip_sentences[1:]:
        key = _normalize_exact(s["text"])
        if key and key == run_key:
            run.append(s)
            continue
        if len(run) > 1:
            _cut_exact_repeat_run(log, run, run_key, cut_ids, keeper_ids)
        run = [s]
        run_key = key
    if len(run) > 1:
        _cut_exact_repeat_run(log, run, run_key, cut_ids, keeper_ids)
    return cut_ids


def _rms_stability(wav: np.ndarray, start: float, end: float) -> float:
    sr = config.ANALYSIS_SR
    seg = wav[int(start * sr) : int(end * sr)]
    if len(seg) < sr // 4:
        return 0.0
    hop = sr // 10
    n = len(seg) // hop
    rms = np.sqrt((seg[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    rms = rms[rms > 1e-4]
    if len(rms) < 2:
        return 0.0
    return float(1.0 / (1.0 + np.std(np.log10(rms + 1e-6))))


def _score(sent: dict, wav: np.ndarray | None, take_index: int) -> tuple[float, str]:
    text = sent["text"]
    n_words = max(1, len(sent["words"]))
    fillers = len(FILLERS.findall(text))
    # word-restarts: "so we- so we can" => repeated bigrams
    toks = _norm(text).split()
    restarts = sum(
        1 for k in range(len(toks) - 3) if toks[k] == toks[k + 2] and toks[k + 1] == toks[k + 3]
    )
    complete = 1.0 if SENT_END.search(text.strip()) else 0.0
    dur = max(0.3, sent["end"] - sent["start"])
    rate = n_words / dur  # ~2-3.3 w/s is comfortable speech
    rate_score = 1.0 - min(1.0, abs(rate - 2.7) / 2.7)
    stability = _rms_stability(wav, sent["start"], sent["end"]) if wav is not None else 0.5

    score = (
        -2.0 * fillers
        - 2.5 * restarts
        + 2.0 * complete
        + 1.5 * rate_score
        + 1.5 * stability
        + 0.4 * take_index  # later takes are usually the intended one
    )
    why = (
        f"fillers={fillers} restarts={restarts} complete={bool(complete)} "
        f"rate={rate:.1f}w/s stability={stability:.2f} take#{take_index + 1}"
    )
    return round(score, 2), why


CLEANER_CHUNK_SIZE = 40
CLEANER_CHUNK_OVERLAP = 5


def _transcript_cleanup_chunk(log, chunk: list[dict], project: dict) -> set[str]:
    """Ask the transcript_cleaner agent which sentences in this chunk are
    restart markers / abandoned takes / meta-asides. Fail-open: on any error,
    log and return an empty set so the pipeline keeps going untouched.
    v5.8c: sentences gain a "<Speaker>: " prefix when the project has
    diarized speakers, so the agent doesn't read cross-speaker turns as a
    single confused voice."""
    from ..agents.agents import get_agent

    numbered = "\n".join(
        f'{i + 1}: "{speaker_prefix(project, s)}{s["text"]}"' for i, s in enumerate(chunk)
    )
    try:
        result = get_agent("transcript_cleaner").run_sync(
            f"Numbered sentences from one clip, in order:\n{numbered}"
        ).output
        cut: set[str] = set()
        for n in result.cut_ids:
            idx = n - 1
            if 0 <= idx < len(chunk):
                cut.add(chunk[idx]["id"])
        return cut
    except Exception as exc:
        log(f"transcript_cleaner chunk failed, skipping: {exc}")
        return set()


def _transcript_cleanup(log, clip_sentences: list[dict], project: dict) -> set[str]:
    """Chunk one clip's sentences (<=40, 5-sentence overlap) and union the
    cut ids the transcript_cleaner agent flags across all chunks."""
    if not clip_sentences:
        return set()
    cut_ids: set[str] = set()
    step = CLEANER_CHUNK_SIZE - CLEANER_CHUNK_OVERLAP
    i = 0
    n = len(clip_sentences)
    while i < n:
        chunk = clip_sentences[i : i + CLEANER_CHUNK_SIZE]
        cut_ids |= _transcript_cleanup_chunk(log, chunk, project)
        if i + CLEANER_CHUNK_SIZE >= n:
            break
        i += step
    return cut_ids


SEQUENCER_WINDOW_SIZE = 12
SEQUENCER_WINDOW_OVERLAP = 2


def _take_sequencer_window(
    log, clip_sentences: list[dict], window: list[dict], start_idx: int, project: dict
) -> set[str]:
    """Ask the take_sequencer agent for stuck-take runs inside this window.
    Sentences are numbered 1..len(window) with their timestamp and the gap
    (seconds of silence) before them, computed against the FULL per-clip
    sequence so the gap is meaningful even at a window boundary. Fail-open:
    on any error, log and return an empty set for this window only.
    v5.8c: sentences gain a "<Speaker>: " prefix when the project has
    diarized speakers (see speaker_prefix)."""
    from ..agents.agents import get_agent

    lines = []
    for i, s in enumerate(window):
        global_idx = start_idx + i
        gap = 0.0
        if global_idx > 0:
            gap = max(0.0, s["start"] - clip_sentences[global_idx - 1]["end"])
        lines.append(f'{i + 1} ({gap:.1f}s): "{speaker_prefix(project, s)}{s["text"]}"')
    numbered = "\n".join(lines)
    try:
        result = get_agent("take_sequencer").run_sync(
            "Sliding window of consecutive sentences from one clip "
            f"(id, gap before, text):\n{numbered}"
        ).output
        cut: set[str] = set()
        for run in result.cut_runs:
            a, b = run.start_id, run.end_id
            if a > b:
                a, b = b, a
            for n in range(a, b + 1):
                idx = n - 1
                if 0 <= idx < len(window):
                    cut.add(window[idx]["id"])
        return cut
    except Exception as exc:
        log(f"take_sequencer window failed, skipping: {exc}")
        return set()


def _take_sequencer_clip(log, clip_sentences: list[dict], project: dict) -> set[str]:
    """Slide a ~12-sentence, 2-sentence-overlap window over one clip's
    sentences and union the stuck-take-run cut ids the take_sequencer agent
    flags across all windows."""
    if not clip_sentences:
        return set()
    cut_ids: set[str] = set()
    step = SEQUENCER_WINDOW_SIZE - SEQUENCER_WINDOW_OVERLAP
    i = 0
    n = len(clip_sentences)
    while i < n:
        window = clip_sentences[i : i + SEQUENCER_WINDOW_SIZE]
        cut_ids |= _take_sequencer_window(log, clip_sentences, window, i, project)
        if i + SEQUENCER_WINDOW_SIZE >= n:
            break
        i += step
    return cut_ids


def _compute_topic(log, sentences: list[dict]) -> str:
    """Cheap agent call over the (truncated) full transcript: one-line video
    topic, used to judge out-of-context asides and cross-clip duplicates.
    Fail-open: on error, return "" and the callers just skip topic-aware
    checks."""
    from ..agents.agents import get_agent

    if not sentences:
        return ""
    full_text = " ".join(s["text"] for s in sentences)[: config.TOPIC_INPUT_CHARS]
    try:
        result = get_agent("video_topic").run_sync(f"Transcript:\n{full_text}").output
        return result.topic.strip()
    except Exception as exc:
        log(f"video_topic failed, continuing without a topic: {exc}")
        return ""


def _context_check_chunk(
    log, chunk: list[dict], project: dict, topic: str
) -> list[dict]:
    """Ask the context_check agent which sentences in this chunk are
    meta-asides / out-of-context for the given topic, batched (one call for
    up to CONTEXT_CHECK_CHUNK_SIZE sentences instead of one call per
    sentence), same numbered/flat pattern as `_transcript_cleanup_chunk`.
    Each flagged sentence carries a confidence (1-5) so the caller can gate
    auto-cut vs. suggestion. Fail-open: on any error, log and return no
    flags for this chunk only."""
    from ..agents.agents import get_agent

    numbered = "\n".join(
        f'{i + 1}: "{speaker_prefix(project, s)}{s["text"]}"' for i, s in enumerate(chunk)
    )
    prompt = f'Video topic: "{topic}"\n\nNumbered sentences from one clip, in order:\n{numbered}'
    try:
        result = get_agent("context_check").run_sync(prompt).output
        flags: list[dict] = []
        for f in result.out_of_context:
            idx = f.id - 1
            if 0 <= idx < len(chunk):
                flags.append(
                    {"id": chunk[idx]["id"], "confidence": f.confidence, "reason": f.reason}
                )
        return flags
    except Exception as exc:
        log(f"context_check chunk failed, skipping: {exc}")
        return []


def _context_check_clip(
    log, clip_sentences: list[dict], topic: str, project: dict
) -> tuple[set[str], list[dict]]:
    """Chunk one clip's sentences (<=CONTEXT_CHECK_CHUNK_SIZE, small overlap)
    and batch the "does this sentence belong in a video about <topic>?"
    judgement, exactly like `_transcript_cleanup` / `_take_sequencer_clip`
    chunk their passes -- this cuts context_check's per-run LLM call count
    from O(sentences) to O(sentences / CONTEXT_CHECK_CHUNK_SIZE), which was
    the root cause of takes never finishing on constrained hardware.

    Confidence-gated ("suggest, don't delete"): flags at or above
    CONTEXT_CHECK_AUTOCUT_CONFIDENCE auto-cut; flags at or above
    CONTEXT_CHECK_SUGGEST_CONFIDENCE (but below autocut) become an open
    suggestion instead of a silent cut, matching how `_cross_clip_dedup`
    already splits dedup_judge's confidence. A sentence flagged twice by
    overlapping chunks is only counted once (first verdict wins)."""
    if not clip_sentences:
        return set(), []
    autocut: set[str] = set()
    suggestions: list[dict] = []
    seen: set[str] = set()
    step = config.CONTEXT_CHECK_CHUNK_SIZE - config.CONTEXT_CHECK_CHUNK_OVERLAP
    i = 0
    n = len(clip_sentences)
    while i < n:
        chunk = clip_sentences[i : i + config.CONTEXT_CHECK_CHUNK_SIZE]
        for flag in _context_check_chunk(log, chunk, project, topic):
            sid = flag["id"]
            if sid in seen:
                continue
            seen.add(sid)
            if flag["confidence"] >= config.CONTEXT_CHECK_AUTOCUT_CONFIDENCE:
                autocut.add(sid)
            elif flag["confidence"] >= config.CONTEXT_CHECK_SUGGEST_CONFIDENCE:
                suggestions.append(
                    {
                        "id": uuid.uuid4().hex[:8],
                        "kind": "off_topic",
                        "sentence_ids": [sid],
                        "message": (flag["reason"] or "possibly out of context")[:300],
                        "proposed_action": "cut",
                        "status": "open",
                    }
                )
        if i + config.CONTEXT_CHECK_CHUNK_SIZE >= n:
            break
        i += step
    return autocut, suggestions


def _large_overlapping_chunks(sentences: list[dict], n_chunks: int) -> list[list[dict]]:
    """Split `sentences` into EXACTLY `n_chunks` large, overlapping pieces
    covering the whole clip -- the full-clip review's fallback path when the
    entire clip doesn't fit the resolved model's context window.
    Deliberately NOT the tiny 12/15/40-sentence sliding windows the other
    passes use (a sliding window with a small step produces far MORE than
    `n_chunks` pieces): this divides the clip into `n_chunks` roughly equal,
    non-overlapping base slices first, then grows each slice's boundary
    into its neighbor's territory so adjacent chunks actually overlap --
    a pair of takes split across two base slices still shows up together in
    at least one chunk. Degenerates to a single chunk (the whole list) when
    there's nothing meaningful to split."""
    n = len(sentences)
    if n_chunks <= 1 or n <= n_chunks:
        return [sentences]
    base_size = n // n_chunks
    remainder = n % n_chunks
    overlap = max(3, base_size // 5)
    bounds = []
    start = 0
    for i in range(n_chunks):
        size = base_size + (1 if i < remainder else 0)
        end = n if i == n_chunks - 1 else start + size
        bounds.append((start, end))
        start = end
    chunks = []
    for i, (s, e) in enumerate(bounds):
        lo = max(0, s - overlap) if i > 0 else s
        hi = min(n, e + overlap) if i < n_chunks - 1 else e
        chunks.append(sentences[lo:hi])
    return chunks


def _verify_superseded_by(
    chunk: list[dict],
    flagged_numbers: set[int],
    sentence_number: int,
    superseded_by_number: int,
    sent: dict,
) -> float | None:
    """Code-verifies one BlooperFlag's `superseded_by` claim before it's
    trusted at all (precision fix, 2026-07-26 -- a live-verification run
    found the model auto-cutting topic-setup/transition sentences with NO
    genuine later restatement; see BlooperFlag's docstring in
    agents/schemas.py). The claimed later sentence must:
      (a) resolve to a real sentence in this SAME numbered chunk,
      (b) be strictly LATER than the flagged sentence,
      (c) not itself be one of this call's OTHER flagged (about-to-be-cut)
          sentence numbers, and still be `kept` -- it can't be "the kept,
          better version" if it's itself on the chopping block, and
      (d) have real textual similarity to the flagged sentence.

    Returns the rapidfuzz token_set_ratio similarity (0-100, same _norm()
    normalization `_cross_clip_dedup` already uses for its candidate pairs)
    when (a)-(c) hold, or None when any of them fails outright -- there's
    nothing meaningful to compare, so the caller treats None as "structurally
    unverifiable" rather than a similarity score of 0."""
    idx = superseded_by_number - 1
    if not (0 <= idx < len(chunk)):
        return None
    if superseded_by_number <= sentence_number:
        return None
    if superseded_by_number in flagged_numbers:
        return None
    sup_sentence = chunk[idx]
    if not sup_sentence.get("kept", True):
        return None
    return float(fuzz.token_set_ratio(_norm(sent["text"]), _norm(sup_sentence["text"])))


def _full_clip_review_chunk(
    log, chunk: list[dict], project: dict, protected_survivor_ids: set[str]
) -> list[dict]:
    """One blooper_reviewer call over `chunk` (either an entire clip's
    not-yet-cut sentences, or one large fallback chunk of it -- see
    `_full_clip_review`). Same numbered/flat pattern as
    `_context_check_chunk`, plus an explicit "off-limits" line listing any
    sentence in `protected_survivor_ids` that appears in this chunk, so the
    prompt itself tells the model never to flag a protected duplicate-
    cluster survivor. Hard guard in code (not just the prompt): any flag the
    model returns for a protected id is dropped here before the caller ever
    sees it.

    Precision fix (2026-07-26): each flag's required `superseded_by` claim is
    code-verified here via `_verify_superseded_by` (real, later, kept, not
    itself flagged) and its rapidfuzz similarity to the flagged sentence is
    attached (`similarity`: a 0-100 float, or None if the claim failed
    verification outright) -- `_full_clip_review` uses this to decide
    autocut vs. suggestion vs. drop; this function itself never decides
    that. Fail-open: on any error, log and return no flags for this chunk
    only."""
    from ..agents.agents import get_agent

    numbered = "\n".join(
        f'{i + 1}: "{speaker_prefix(project, s)}{s["text"]}"' for i, s in enumerate(chunk)
    )
    protected_local = sorted(
        i + 1 for i, s in enumerate(chunk) if s["id"] in protected_survivor_ids
    )
    protected_line = (
        f"Off-limits sentence numbers (never flag these): {protected_local}\n\n"
        if protected_local
        else ""
    )
    prompt = f"{protected_line}Numbered sentences from one entire clip, in order:\n{numbered}"
    try:
        result = get_agent("blooper_reviewer").run_sync(prompt).output
    except Exception as exc:
        log(f"blooper_reviewer chunk failed, skipping: {exc}")
        return []

    flagged_numbers = {f.sentence_number for f in result.flags}
    flags: list[dict] = []
    for f in result.flags:
        idx = f.sentence_number - 1
        if not (0 <= idx < len(chunk)):
            continue
        sent = chunk[idx]
        if sent["id"] in protected_survivor_ids:
            continue  # hard guard: never trust the model on a protected id
        similarity = _verify_superseded_by(
            chunk, flagged_numbers, f.sentence_number, f.superseded_by, sent
        )
        flags.append(
            {
                "id": sent["id"],
                "confidence": f.confidence,
                "reason": f.reason,
                "similarity": similarity,
            }
        )
    return flags


def _full_clip_review(
    log, clip_sentences: list[dict], project: dict, protected_survivor_ids: set[str]
) -> tuple[set[str], list[dict]]:
    """Whole-clip single-prompt reasoning pass (WS-C, spec point 3): the
    chunked windows above (transcript_cleaner <=40, take_sequencer ~12,
    context_check <=15 sentences) each only ever see a small slice of one
    clip, so a bad take whose better retake lands far outside every window
    slips through all three. This sends `clip_sentences` -- the clip's
    ENTIRE not-yet-cut sentence list, as left by those three passes -- to
    the blooper_reviewer agent in ONE prompt, unless the full text doesn't
    fit the resolved model's context window (token_budget.fits_context), in
    which case it falls back to config.FULL_CLIP_REVIEW_FALLBACK_CHUNKS
    large overlapping chunks (see `_large_overlapping_chunks`) -- NEVER the
    tiny 12/15/40-sentence windows the other passes use.

    Strictly per-clip: never called across clip boundaries, so the sacred
    chronological-order invariant is trivially safe here (this pass never
    reorders anything, only flags cut candidates).

    Precision fix (2026-07-26): a live-verification run against
    deepseek-r1:14b auto-cut two topic-setup/transition sentences that had NO
    genuine later restatement -- the model's confidence alone wasn't a
    reliable enough gate. Every flag's `superseded_by` claim is now
    code-verified (`_verify_superseded_by`, real/later/kept/not-itself-
    flagged/textually-similar via rapidfuzz token_set_ratio) before it's
    trusted: verification passing AND confidence >= FULL_CLIP_REVIEW_
    AUTOCUT_CONFIDENCE -> auto-cut (never a `protected_survivor_ids` id --
    hard guard in code, on top of the chunk-level guard and the prompt's
    off-limits list); verification failing (or passing at only suggest-level
    confidence) -> an open suggestion instead, UNLESS the claimed
    supersession is so dissimilar (similarity < FULL_CLIP_REVIEW_SUPERSEDE_
    DROP_SIMILARITY) that even a suggestion would be noise, in which case the
    flag is dropped outright. Fail-open throughout."""
    if len(clip_sentences) < 2:
        return set(), []

    model_name = settings.model_for("blooper_reviewer") or config.OLLAMA_MODEL
    numbered_all = "\n".join(
        f'{i + 1}: "{speaker_prefix(project, s)}{s["text"]}"'
        for i, s in enumerate(clip_sentences)
    )
    total_tokens = token_budget.estimate_tokens(numbered_all)

    if token_budget.fits_context(total_tokens, model_name):
        raw_flags = _full_clip_review_chunk(log, clip_sentences, project, protected_survivor_ids)
    else:
        n_chunks = max(1, config.FULL_CLIP_REVIEW_FALLBACK_CHUNKS)
        log(
            f"Full clip text ({total_tokens} est. tokens) doesn't fit "
            f"{model_name}'s context window -- falling back to {n_chunks} "
            "large overlapping chunk(s) instead of the tiny windows"
        )
        raw_flags = []
        seen_ids: set[str] = set()
        for chunk in _large_overlapping_chunks(clip_sentences, n_chunks):
            for flag in _full_clip_review_chunk(log, chunk, project, protected_survivor_ids):
                if flag["id"] in seen_ids:
                    continue
                seen_ids.add(flag["id"])
                raw_flags.append(flag)

    autocut: set[str] = set()
    suggestions: list[dict] = []
    for flag in raw_flags:
        sid = flag["id"]
        if sid in protected_survivor_ids:
            continue  # belt-and-suspenders on top of the chunk-level guard

        similarity = flag["similarity"]  # None (unverifiable) or a 0-100 float
        drop_floor = config.FULL_CLIP_REVIEW_SUPERSEDE_DROP_SIMILARITY
        if similarity is not None and similarity < drop_floor:
            continue  # claimed supersession too dissimilar -- noise even as a suggestion
        verified = (
            similarity is not None and similarity >= config.FULL_CLIP_REVIEW_SUPERSEDE_SIMILARITY
        )

        if verified and flag["confidence"] >= config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE:
            autocut.add(sid)
        elif flag["confidence"] >= config.FULL_CLIP_REVIEW_SUGGEST_CONFIDENCE:
            suggestions.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "kind": "repeated_idea",
                    "sentence_ids": [sid],
                    "message": (flag["reason"] or "possibly a superseded take said better later")[
                        :300
                    ],
                    "proposed_action": "cut",
                    "status": "open",
                }
            )
    return autocut, suggestions


def _neighbor_context(clip_sentences: list[dict], idx: int) -> str:
    if idx > 0:
        return clip_sentences[idx - 1]["text"]
    if idx + 1 < len(clip_sentences):
        return clip_sentences[idx + 1]["text"]
    return ""


def _rare_keyword_buckets(kept: list[dict], norm: dict[str, str]) -> dict[str, set[str]]:
    """Bucket sentences by their RARE keywords (words at most
    CROSS_DEDUP_KEYWORD_MAX_DF sentences long enough to matter use
    project-wide) so `_cross_clip_dedup` only fuzzy-compares pairs that
    plausibly discuss the same specific thing, instead of every kept-sentence
    pair -- on a long recording, O(kept^2) token_set_ratio calls explodes
    into hundreds of thousands of comparisons. Never implemented in the v4
    write-up; this is that pre-filter."""
    word_ids: dict[str, set[str]] = {}
    for s in kept:
        for w in set(norm[s["id"]].split()):
            if len(w) < config.CROSS_DEDUP_KEYWORD_MIN_LEN:
                continue
            word_ids.setdefault(w, set()).add(s["id"])
    rare_words = {w for w, ids in word_ids.items() if len(ids) <= config.CROSS_DEDUP_KEYWORD_MAX_DF}
    buckets: dict[str, set[str]] = {}
    for s in kept:
        for w in set(norm[s["id"]].split()):
            if w in rare_words:
                buckets.setdefault(w, set()).add(s["id"])
    return buckets


def _cross_clip_recency_hint(project: dict, a: dict, b: dict) -> str | None:
    """Spec point 5's cross-file clause: re-recording shortly after stopping
    a clip is often a redo fixing a mistake in the earlier one. If both
    clip_a and clip_b have `recorded_at` set (optional -- added by another
    workstream at ingest; never fabricated when missing), and clip_b started
    within [0, CROSS_CLIP_RECENCY_WINDOW_S] seconds of clip_a finishing
    (recorded_at + its info duration), return a one-line hint for the
    dedup_judge user message. Judgment is still by content first -- this is
    a nudge, not a verdict, and never touches the DedupJudge schema."""
    clip_a = store.get_clip(project, a["clip_id"])
    clip_b = store.get_clip(project, b["clip_id"])
    recorded_a = clip_a.get("recorded_at")
    recorded_b = clip_b.get("recorded_at")
    if recorded_a is None or recorded_b is None:
        return None
    duration_a = (clip_a.get("info") or {}).get("duration") or 0.0
    gap_s = recorded_b - (recorded_a + duration_a)
    if 0 <= gap_s <= config.CROSS_CLIP_RECENCY_WINDOW_S:
        return (
            f"Clip B started recording ~{gap_s:.0f}s after clip A stopped -- "
            "often a redo fixing a mistake in A, but judge by content first."
        )
    return None


def _cross_clip_dedup(
    log, sentences: list[dict], topic: str, project: dict
) -> tuple[set[str], list[dict]]:
    """Cross-clip semantic dedup with auto-cut (v4 section 1): candidate
    pairs are currently-kept sentences from DIFFERENT clips with rapidfuzz
    token_set_ratio in [CROSS_DEDUP_MIN_SIM, CROSS_DEDUP_MAX_SIM], capped at
    CROSS_DEDUP_MAX_PAIRS (highest similarity first). Each pair is judged by
    dedup_judge with one neighboring sentence of context per side and the
    topic. same_content and confidence>=CROSS_DEDUP_AUTOCUT_CONFIDENCE ->
    auto-cut the non-kept sentence; confidence in [CROSS_DEDUP_SUGGEST_
    CONFIDENCE, autocut) -> an open suggestion instead. Fail-open per pair."""
    from ..agents.agents import get_agent

    by_clip: dict[str, list[dict]] = {}
    for s in sentences:
        by_clip.setdefault(s["clip_id"], []).append(s)
    idx_by_id: dict[str, int] = {}
    for clip_list in by_clip.values():
        for i, s in enumerate(clip_list):
            idx_by_id[s["id"]] = i

    kept = [s for s in sentences if s["kept"]]
    norm = {s["id"]: _norm(s["text"]) for s in kept}
    eligible = [s for s in kept if len(norm[s["id"]].split()) >= config.DUP_MIN_WORDS]
    by_id = {s["id"]: s for s in eligible}

    # Pre-filter (perf fix): bucket by shared rare keywords BEFORE the fuzzy
    # loop, so we only run token_set_ratio on pairs that plausibly discuss
    # the same specific thing, not every kept-sentence pair.
    buckets = _rare_keyword_buckets(eligible, norm)
    candidate_ids: set[tuple[str, str]] = set()
    for ids in buckets.values():
        ids_list = list(ids)
        for x in range(len(ids_list)):
            for y in range(x + 1, len(ids_list)):
                a_id, b_id = ids_list[x], ids_list[y]
                if by_id[a_id]["clip_id"] == by_id[b_id]["clip_id"]:
                    continue
                candidate_ids.add((a_id, b_id) if a_id < b_id else (b_id, a_id))

    pairs: list[tuple[float, dict, dict]] = []
    for a_id, b_id in candidate_ids:
        a, b = by_id[a_id], by_id[b_id]
        sim = fuzz.token_set_ratio(norm[a_id], norm[b_id])
        if config.CROSS_DEDUP_MIN_SIM <= sim <= config.CROSS_DEDUP_MAX_SIM:
            pairs.append((sim, a, b))
    pairs.sort(key=lambda p: -p[0])
    pairs = pairs[: config.CROSS_DEDUP_MAX_PAIRS]
    log(
        f"{len(candidate_ids)} rare-keyword candidate pair(s), "
        f"{len(pairs)} cross-clip candidate pair(s) for dedup_judge"
    )

    agent = get_agent("dedup_judge")
    autocut: set[str] = set()
    suggestions: list[dict] = []
    for _sim, a, b in pairs:
        if a["id"] in autocut or b["id"] in autocut:
            continue
        a_ctx = _neighbor_context(by_clip[a["clip_id"]], idx_by_id[a["id"]])
        b_ctx = _neighbor_context(by_clip[b["clip_id"]], idx_by_id[b["id"]])
        recency_hint = _cross_clip_recency_hint(project, a, b)
        prompt = (
            f'Video topic: "{topic}"\n\n'
            f'Context before A: "{a_ctx}"\n'
            f'A: "{speaker_prefix(project, a)}{a["text"]}"\n\n'
            f'Context before B: "{b_ctx}"\n'
            f'B: "{speaker_prefix(project, b)}{b["text"]}"'
        )
        if recency_hint:
            prompt += f"\n\n{recency_hint}"
        try:
            result = agent.run_sync(prompt).output
        except Exception as exc:
            log(f"dedup_judge failed for a pair, skipping: {exc}")
            continue
        if not result.same_content:
            continue
        loser = b if result.keep == "a" else a
        if result.confidence >= config.CROSS_DEDUP_AUTOCUT_CONFIDENCE:
            autocut.add(loser["id"])
        elif result.confidence >= config.CROSS_DEDUP_SUGGEST_CONFIDENCE:
            suggestions.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "kind": "redundant",
                    "sentence_ids": [a["id"], b["id"]],
                    "message": (result.reason or "possible duplicate content across clips")[:300],
                    "proposed_action": "cut",
                    "status": "open",
                }
            )
    return autocut, suggestions


def _llm_tiebreak(candidates: list[dict]) -> str | None:
    """Ask the take-judge agent which near-tied take reads best. Returns
    sentence id or None."""
    from ..agents.agents import get_agent

    try:
        listing = "\n".join(f'{i}: "{c["text"]}"' for i, c in enumerate(candidates))
        pick = get_agent("take_judge").run_sync(f"Takes of the same line:\n{listing}").output
        if 0 <= pick.best < len(candidates):
            return candidates[pick.best]["id"]
    except Exception:
        pass
    return None


def _take_completeness(sent: dict) -> float:
    """Proxy for how COMPLETE/finished a take reads (Fix 2, 2026-07-26):
    word count plus a bonus for actually ending on sentence-final
    punctuation. Needed because `SENT_END` alone can't tell a full thought
    from a truncated one that still happens to end in a period -- e.g. an
    earlier take "...y termina costando el triple." (ends in ".", but cuts
    the sentence off) versus the later, more complete retake "...costando el
    triple cuando por fin se hace." (also ends in ".", but actually finishes
    the clause). Longer + properly terminated wins as "more complete"."""
    n_words = len(sent["words"]) if sent.get("words") else len(sent["text"].split())
    ends_full = 1.0 if SENT_END.search(sent["text"].strip()) else 0.0
    return n_words + config.TAKE_COMPLETENESS_END_BONUS * ends_full


def _select_cluster_winner(cluster: list[dict]) -> dict:
    """Pick the survivor of a duplicate/near-duplicate cluster (Fix 2,
    2026-07-26 -- owner heuristic: "si dicen la misma linea, quedate con la
    ultima"). `_score` (with its `take_index` recency term) is still the
    base quality signal -- a mumbled/garbled later take shouldn't
    automatically beat a clean earlier one -- but a real manual-vs-auto
    comparison found it can favor a shorter/earlier take over a later, more
    complete retake purely on the heuristic disfluency/rate/stability terms.
    Fix: among takes whose score is within TAKE_WINNER_SCORE_MARGIN of the
    best, prefer the chronologically LATER one, then the more COMPLETE one
    (longer / fuller ending), instead of just the highest raw score. Assumes
    every sentence already has `take_index` set (scoring loop in `run()`)."""
    top_score = max(s["score"] for s in cluster)
    close = [s for s in cluster if top_score - s["score"] <= config.TAKE_WINNER_SCORE_MARGIN]
    return max(close, key=lambda s: (s["take_index"], _take_completeness(s), s["score"]))


def run(log, project: dict) -> None:
    clips = [c for c in project["clips"] if c.get("transcript") and c["role"] == "camera"]
    if not clips:
        raise RuntimeError("Run Transcribe first.")

    # In a multi-cam sync group, only analyze the main camera's transcript
    # (same audio content on every angle would look like duplicates).
    grouped_non_main = set()
    for g in project.get("sync_groups", []):
        members = [m["clip_id"] for m in g["members"]]
        cams = [m for m in members if store.get_clip(project, m)["role"] == "camera"]
        mains = [m for m in cams if store.get_clip(project, m)["is_main"]]
        keep = mains[0] if mains else (cams[0] if cams else None)
        grouped_non_main.update(m for m in cams if m != keep)

    log("Splitting transcripts into sentences...")
    sentences: list[dict] = []
    wavs: dict[str, np.ndarray] = {}
    for clip in clips:
        if clip["id"] in grouped_non_main:
            log(f"{clip['filename']}: secondary angle of a sync group, skipping text analysis")
            continue
        sents = _sentences_from_clip(clip)
        sentences.extend(sents)
        if clip.get("wav"):
            wavs[clip["id"]] = ffmpeg_utils.load_wav_mono(clip["wav"])
        log(f"{clip['filename']}: {len(sents)} sentences")

    by_clip: dict[str, list[dict]] = {}
    for s in sentences:
        by_clip.setdefault(s["clip_id"], []).append(s)

    # Duplicate-cluster survivor guard (Fix 1, 2026-07-26): every duplicate/
    # near-duplicate cluster (exact-repeat run, or fuzzy-dedup group further
    # below) designates exactly ONE survivor. Collected as we go and enforced
    # at the very end of run() -- no later pass (cleaner/sequencer/
    # context_check/cross-clip dedup_judge/fragment-drop) may ever leave a
    # cluster with zero kept=True members. Real bug this closes: a CTA line
    # delivered twice had BOTH deliveries independently cut by different
    # passes, and the final cut lost the line entirely.
    protected_survivor_ids: set[str] = set()

    # Deterministic, LLM-free pre-pass (owner heuristic): the same line said
    # EXACTLY twice (or more) in a row within one clip is an unambiguous
    # flub-then-redo -- keep only the LAST occurrence. Runs FIRST (before the
    # topic/cleaner/sequencer LLM passes and the fuzzy dedup below) so those
    # passes see fewer duplicates and never re-litigate a call this exact-
    # match rule already made. Never crosses clip boundaries.
    exact_repeat_cut_ids: set[str] = set()
    if config.EXACT_REPEAT_DEDUP_ENABLED:
        log("Checking for exact consecutive repeats (blooper -> redo)...")
        for clip_sentences in by_clip.values():
            exact_repeat_cut_ids |= _exact_repeat_dedup_clip(
                log, clip_sentences, protected_survivor_ids
            )
        if exact_repeat_cut_ids:
            log(f"exact-repeat pre-pass cut {len(exact_repeat_cut_ids)} sentence(s)")

    # LLM passes BEFORE fuzzy dedup: catch restart markers / abandoned takes /
    # bloopers retaken with different wording / meta-asides that
    # string-similarity dedup below can't see. Fail-open, skipped entirely
    # when ollama is down.
    topic = ""
    cleaner_cut_ids: set[str] = set()
    sequencer_cut_ids: set[str] = set()
    context_cut_ids: set[str] = set()
    context_suggestions: list[dict] = []
    full_review_cut_ids: set[str] = set()
    full_review_suggestions: list[dict] = []
    if llm.available():
        log("Summarizing video topic...")
        topic = _compute_topic(log, sentences)
        if topic:
            log(f"Topic: {topic}")

        log("Running transcript cleaner (restarts / abandoned takes / bloopers)...")
        for clip_sentences in by_clip.values():
            cleaner_cut_ids |= _transcript_cleanup(log, clip_sentences, project)
        if cleaner_cut_ids:
            log(f"transcript cleaner flagged {len(cleaner_cut_ids)} sentence(s)")

        # v5.6 take_sequencer: sliding-window pass over stuck take RUNS
        # (halting/repeated attempts ending in a self-encouragement marker,
        # or the same line repeated many times) that the per-sentence
        # cleaner above can miss because no single sentence looks wrong in
        # isolation. Runs AFTER the cleaner, BEFORE fuzzy dedup clustering.
        log("Running take sequencer (stuck take runs)...")
        for clip_sentences in by_clip.values():
            sequencer_cut_ids |= _take_sequencer_clip(log, clip_sentences, project)
        if sequencer_cut_ids:
            log(f"take sequencer flagged {len(sequencer_cut_ids)} sentence(s)")

        if topic:
            log("Running context pass (out-of-context asides)...")
            # Hard cap (mirrors CROSS_DEDUP_MAX_PAIRS): a huge project can't
            # explode context_check into an unbounded number of chunked
            # calls either. Sentences beyond the cap are simply skipped by
            # this pass (still eligible for cleaner/sequencer/dedup).
            budget = config.CONTEXT_CHECK_MAX_SENTENCES
            for clip_sentences in by_clip.values():
                if budget <= 0:
                    log(
                        "context pass: hit CONTEXT_CHECK_MAX_SENTENCES "
                        f"({config.CONTEXT_CHECK_MAX_SENTENCES}), skipping remaining clips"
                    )
                    break
                clip_slice = clip_sentences[:budget]
                budget -= len(clip_slice)
                cut_ids, sugg = _context_check_clip(log, clip_slice, topic, project)
                context_cut_ids |= cut_ids
                context_suggestions.extend(sugg)
            if context_cut_ids:
                log(f"context pass flagged {len(context_cut_ids)} sentence(s)")
            if context_suggestions:
                log(f"context pass added {len(context_suggestions)} suggestion(s)")

        # WS-C full-clip blooper-review pass: runs AFTER transcript_cleaner /
        # take_sequencer / context_check above, over each clip's already-
        # reduced (not-yet-cut) sentence list, so it only has to catch what
        # those chunked/windowed passes structurally can't -- a repeat far
        # enough apart that no single window ever held both takes together.
        # Strictly per-clip, one entire clip's remaining sentences per call
        # (see _full_clip_review for the context-window fallback).
        log("Running full-clip blooper review (whole-clip single-prompt pass)...")
        already_cut = cleaner_cut_ids | sequencer_cut_ids | context_cut_ids | exact_repeat_cut_ids
        for clip_sentences in by_clip.values():
            reduced = [s for s in clip_sentences if s["id"] not in already_cut]
            cut_ids, sugg = _full_clip_review(log, reduced, project, protected_survivor_ids)
            full_review_cut_ids |= cut_ids
            full_review_suggestions.extend(sugg)
        if full_review_cut_ids:
            log(f"full-clip blooper review flagged {len(full_review_cut_ids)} sentence(s)")
        if full_review_suggestions:
            log(f"full-clip blooper review added {len(full_review_suggestions)} suggestion(s)")
    else:
        log("transcript cleaner skipped (ollama unavailable)")
    project["topic"] = topic

    # Cluster near-duplicate sentences (repeated takes), greedy by similarity.
    # Sentences already resolved by the exact-repeat pre-pass are excluded
    # here -- that deterministic rule already picked a winner (the last take)
    # and must not be re-litigated by this heuristic, score-based grouping.
    log("Detecting repeated takes...")
    norm = {s["id"]: _norm(s["text"]) for s in sentences}
    assigned: dict[str, str] = {}
    groups: list[list[dict]] = []
    for i, s in enumerate(sentences):
        if s["id"] in exact_repeat_cut_ids:
            continue
        if s["id"] in assigned or len(norm[s["id"]].split()) < config.DUP_MIN_WORDS:
            continue
        cluster = [s]
        assigned[s["id"]] = s["id"]
        for t in sentences[i + 1 :]:
            if t["id"] in exact_repeat_cut_ids:
                continue
            if t["id"] in assigned or len(norm[t["id"]].split()) < config.DUP_MIN_WORDS:
                continue
            if fuzz.token_sort_ratio(norm[s["id"]], norm[t["id"]]) >= config.DUP_SIMILARITY:
                cluster.append(t)
                assigned[t["id"]] = s["id"]
        if len(cluster) > 1:
            groups.append(cluster)

    use_llm = llm.available()
    tiebreak = "on" if use_llm else "off (ollama down)"
    log(f"{len(groups)} repeated-take group(s). LLM tiebreak: {tiebreak}")

    # Score everything; pick winners inside duplicate groups.
    take_counter: dict[str, int] = {}
    for s in sentences:
        root = assigned.get(s["id"], s["id"])
        idx = take_counter.get(root, 0)
        take_counter[root] = idx + 1
        s["score"], s["why"] = _score(s, wavs.get(s["clip_id"]), idx)
        s["take_index"] = idx
        s["kept"] = True
        s["dup_group"] = None
        s["reason"] = ""

    for gi, cluster in enumerate(groups):
        # Fix 2 (2026-07-26): winner-selection tie-break is "later + more
        # complete", not just the highest raw heuristic score -- see
        # _select_cluster_winner. The LLM tiebreak (take_judge) still gets a
        # say on genuinely close calls, but only when it disagrees with a
        # score close enough to be worth asking.
        best = _select_cluster_winner(cluster)
        if use_llm and len(cluster) <= 6:
            top = sorted(cluster, key=lambda s: -s["score"])[:3]
            if len(top) > 1 and top[0]["score"] - top[1]["score"] < config.TAKE_WINNER_SCORE_MARGIN:
                pick = _llm_tiebreak(top)
                if pick:
                    best = next(s for s in cluster if s["id"] == pick)
        protected_survivor_ids.add(best["id"])
        for s in cluster:
            s["dup_group"] = f"d{gi}"
            if s["id"] != best["id"]:
                s["kept"] = False
                s["reason"] = (
                    f"repeated take — kept better version (score {best['score']} vs {s['score']})"
                )

    # Enforce the exact-repeat pre-pass verdict (highest priority — it's a
    # deterministic rule, not a heuristic score): every id it flagged must end
    # up kept=False here regardless of what the score-based grouping above
    # did (it already excluded these ids from clustering, but this is the
    # single place that guarantees it — nothing after this point may flip an
    # id back to kept=True).
    for s in sentences:
        if s["id"] in exact_repeat_cut_ids:
            s["kept"] = False
            s["reason"] = "repetición exacta — se conserva la última toma (blooper)"

    # Apply the transcript-cleaner verdicts (restart markers / abandoned
    # takes / bloopers, then stuck take runs, then out-of-context asides) —
    # after scoring/dedup so nothing re-flips them. Fix 1 guard: never let
    # any of these cut a duplicate cluster's protected survivor (the final
    # catch-all pass below would restore it anyway, but skipping here avoids
    # a spurious AI reason ever being recorded for it).
    for s in sentences:
        if s["id"] in exact_repeat_cut_ids:
            continue  # already resolved above; don't let AI passes overwrite the reason
        if s["id"] in protected_survivor_ids:
            continue  # duplicate-cluster survivor guard (Fix 1)
        if s["id"] in cleaner_cut_ids:
            s["kept"] = False
            s["reason"] = "restart/abandoned take (AI)"
        elif s["id"] in sequencer_cut_ids:
            s["kept"] = False
            s["reason"] = "stuck take run (AI)"
        elif s["id"] in context_cut_ids:
            s["kept"] = False
            s["reason"] = "out-of-context aside (AI)"
        elif s["id"] in full_review_cut_ids:
            s["kept"] = False
            s["reason"] = "superseded take found in full-clip review (AI)"

    # Drop tiny fragments that survived (interjections, aborted starts).
    for s in sentences:
        if s["kept"] and len(norm[s["id"]].split()) < 3 and (s["end"] - s["start"]) < 1.2:
            s["kept"] = False
            s["reason"] = "fragment / aborted start"

    # Cross-clip semantic dedup with auto-cut (v4 section 1): runs AFTER
    # per-clip cleaning, on top of whatever is still kept.
    new_suggestions: list[dict] = list(context_suggestions) + list(full_review_suggestions)
    if llm.available():
        log("Running cross-clip duplicate check...")
        dedup_autocut, dedup_suggestions = _cross_clip_dedup(log, sentences, topic, project)
        new_suggestions.extend(dedup_suggestions)
        if dedup_autocut:
            for s in sentences:
                if s["id"] in dedup_autocut and s["kept"]:
                    if s["id"] in protected_survivor_ids:
                        continue  # duplicate-cluster survivor guard (Fix 1)
                    s["kept"] = False
                    s["reason"] = "duplicate content across clips (AI)"
            log(f"cross-clip dedup auto-cut {len(dedup_autocut)} sentence(s)")
        if dedup_suggestions:
            log(f"cross-clip dedup added {len(dedup_suggestions)} suggestion(s)")
    else:
        log("cross-clip dedup skipped (ollama unavailable)")

    # Duplicate-cluster survivor guard, final catch-all (Fix 1, 2026-07-26):
    # belt-and-suspenders on top of the inline skips above -- whichever pass
    # cut a protected survivor (including the tiny-fragment drop, or any
    # future pass added here), restore it now. This is the single place that
    # GUARANTEES every duplicate/near-duplicate cluster keeps at least one
    # kept=True member; nothing after this point may cut it again.
    for s in sentences:
        if s["id"] in protected_survivor_ids and not s["kept"]:
            log(
                f'duplicate-cluster guard: restoring protected survivor '
                f'{s["id"]} (was cut: {s["reason"]!r})'
            )
            s["kept"] = True
            s["reason"] = ""

    # Duplicate-cluster survivor guard applies to SUGGESTIONS too (Fix 1
    # follow-up, 2026-07-26): the autocut paths above are already safe --
    # every autocut-application loop runs after `protected_survivor_ids` is
    # fully populated (exact-repeat pre-pass ids PLUS the fuzzy-dedup cluster
    # winner added per-cluster in the winner-selection loop above), and each
    # checks membership before cutting. But context_check/_full_clip_review/
    # dedup_judge SUGGESTIONS are collected into `new_suggestions` as they're
    # produced -- before winner selection has run for full_review_suggestions
    # in particular -- and were never re-filtered against the final protected
    # set. That let a settled cluster winner (the take we just decided to
    # KEEP) show up in the suggestions list proposing to cut it. Drop any
    # suggestion that references a protected id now that the set is final.
    if new_suggestions:
        before = len(new_suggestions)
        new_suggestions = [
            x for x in new_suggestions if not (set(x["sentence_ids"]) & protected_survivor_ids)
        ]
        dropped = before - len(new_suggestions)
        if dropped:
            log(
                f"dropped {dropped} suggestion(s) referencing a protected "
                "duplicate-cluster survivor"
            )

    if new_suggestions:
        existing = project.get("suggestions", [])
        existing_keys = {(x["kind"], tuple(sorted(x["sentence_ids"]))) for x in existing}
        deduped_new = [
            x
            for x in new_suggestions
            if (x["kind"], tuple(sorted(x["sentence_ids"]))) not in existing_keys
        ]
        project["suggestions"] = existing + deduped_new

    for s in sentences:
        s.pop("words", None)  # words stay in the clip transcript; keep project.json light

    project["sentences"] = sentences
    project["edl"] = None  # re-analyzed takes invalidate any previously computed EDL
    # Sentence ids are regenerated every run (uuid4 in _sentences_from_clip) --
    # any previously-recorded paragraph-break ids can no longer match anything,
    # so drop them rather than leave dangling ids sitting in the project. The
    # "paragraphs" stage naturally needs a re-run after this anyway.
    project["paragraph_break_after_ids"] = []
    kept = sum(1 for s in sentences if s["kept"])
    store.save(project)
    log(f"Kept {kept}/{len(sentences)} sentences.")
