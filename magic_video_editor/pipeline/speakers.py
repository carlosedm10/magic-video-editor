"""Speaker diarization (spec v5.8c): local voice-embedding clustering over
transcript segments, run right after transcribe (see transcribe.py's
integration hook) when the project declares more than one speaker.

- `project["speaker_count"]`: 1 | 2 | 3 | 4 | "auto" (default 1, PATCHable
  via api/projects.py). N=1 (or missing) skips this pass entirely.
- Embeddings: resemblyzer d-vectors (MIT, no gated models), one per whisper
  transcript segment, computed over that segment's window of the clip's
  analysis wav -- already 16kHz mono (config.ANALYSIS_SR), matching
  resemblyzer's expected sample rate, so no resampling pass is needed.
- Clustering: agglomerative (cosine distance, average linkage) with K fixed
  when the project declares a count -- a known speaker count makes this far
  more reliable than estimating it. "auto" instead scans K in
  AUTO_K_RANGE and keeps the best silhouette score (falls back to a single
  speaker when nothing clusters convincingly).
- Output: `segment["speaker"] = "S1"/"S2"/...` on every transcript segment
  that got embedded (segments too short to embed are left unlabeled -- see
  `label_for` for the lookup used by callers) and
  `project["speakers"] = [{id, label, color}]` (default palette, editable
  from the Subs tab via PATCH /api/projects/{pid}). Re-running preserves any
  user-edited label/color for ids that persist across runs.
"""

import numpy as np

from .. import config, ffmpeg_utils, store

# resemblyzer needs enough voiced audio for a stable d-vector; whisper
# segments shorter than this are left unlabeled rather than fed a near-empty
# window (label_for falls back to time-overlap against neighboring labeled
# segments for those).
MIN_SEGMENT_SECONDS = 0.5

AUTO_K_RANGE = (2, 3, 4)

DEFAULT_PALETTE = ["#FFC93C", "#4FD1C5", "#F472B6", "#818CF8"]

VALID_SPEAKER_COUNTS = (1, 2, 3, 4, "auto")


def _encoder():
    from resemblyzer import VoiceEncoder

    return VoiceEncoder()


def _embed_segments(
    log, clips: list[dict]
) -> tuple[list[tuple[dict, dict]], np.ndarray]:
    """[(clip, segment), ...] and their matching embeddings, in clip/
    transcript order. Segments shorter than MIN_SEGMENT_SECONDS, or whose
    embedding fails for any reason, are skipped (left unlabeled)."""
    from resemblyzer import preprocess_wav

    encoder = _encoder()
    sr = config.ANALYSIS_SR
    min_samples = int(MIN_SEGMENT_SECONDS * sr)
    wavs: dict[str, np.ndarray] = {}
    pairs: list[tuple[dict, dict]] = []
    vectors: list[np.ndarray] = []

    for clip in clips:
        if clip["id"] not in wavs:
            wavs[clip["id"]] = ffmpeg_utils.load_wav_mono(clip["wav"])
        wav = wavs[clip["id"]]
        for seg in clip["transcript"]["segments"]:
            start, end = seg["start"], seg["end"]
            if end - start < MIN_SEGMENT_SECONDS:
                continue
            window = wav[int(start * sr) : int(end * sr)]
            if len(window) < min_samples:
                continue
            try:
                processed = preprocess_wav(window, source_sr=sr)
                if len(processed) < min_samples // 2:
                    continue
                vec = encoder.embed_utterance(processed)
            except Exception as exc:
                log(f"speaker embedding failed for a segment, leaving unlabeled: {exc}")
                continue
            pairs.append((clip, seg))
            vectors.append(vec)

    vectors_arr = np.stack(vectors) if vectors else np.empty((0, 256), dtype=np.float32)
    return pairs, vectors_arr


def _cluster_fixed(vectors: np.ndarray, k: int) -> np.ndarray:
    from sklearn.cluster import AgglomerativeClustering

    n = len(vectors)
    if k <= 1 or n <= k:
        return np.zeros(n, dtype=int)
    model = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average")
    return model.fit_predict(vectors)


def _cluster_auto(log, vectors: np.ndarray) -> tuple[np.ndarray, int]:
    """Scan AUTO_K_RANGE, keep the K with the best cosine silhouette score.
    Falls back to a single speaker (all-zero labels) when there aren't
    enough segments to trust clustering or nothing separates convincingly --
    the "auto" variance-heuristic fallback from the spec."""
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score

    n = len(vectors)
    if n < 4:
        return np.zeros(n, dtype=int), 1

    best_labels, best_k, best_score = np.zeros(n, dtype=int), 1, -1.0
    for k in AUTO_K_RANGE:
        if k >= n:
            continue
        model = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average")
        labels = model.fit_predict(vectors)
        if len(set(labels)) < 2:
            continue
        try:
            score = silhouette_score(vectors, labels, metric="cosine")
        except ValueError:
            continue
        if score > best_score:
            best_labels, best_k, best_score = labels, k, score
    if best_score < 0:
        log("auto speaker-count: no convincing split found, treating as a single speaker")
    return best_labels, best_k


def _build_speaker_list(project: dict, speaker_ids: list[str]) -> list[dict]:
    """[{id, label, color}] for `speaker_ids` (in first-appearance order),
    preserving any label/color the user already edited for ids that persist
    across a re-run."""
    existing = {sp["id"]: sp for sp in (project.get("speakers") or [])}
    out = []
    for i, sid in enumerate(speaker_ids):
        prev = existing.get(sid)
        out.append(
            {
                "id": sid,
                "label": prev["label"] if prev else f"Speaker {i + 1}",
                "color": prev["color"] if prev else DEFAULT_PALETTE[i % len(DEFAULT_PALETTE)],
            }
        )
    return out


def run(log, project: dict) -> None:
    raw_count = project.get("speaker_count", 1)
    if raw_count in (1, "1", None):
        log("Speaker diarization skipped (speaker_count=1).")
        return
    if raw_count not in VALID_SPEAKER_COUNTS:
        log(f"Speaker diarization skipped: invalid speaker_count {raw_count!r}.")
        return

    clips = [c for c in project["clips"] if c.get("transcript") and c.get("wav")]
    if not clips:
        log("Speaker diarization skipped: no transcribed clips with analysis audio.")
        return

    log("Extracting voice embeddings for speaker diarization...")
    pairs, vectors = _embed_segments(log, clips)
    if len(pairs) < 2:
        log("Speaker diarization skipped: not enough voiced segments to cluster.")
        return

    if raw_count == "auto":
        labels, k = _cluster_auto(log, vectors)
        log(f"Auto speaker-count estimate: {k}")
    else:
        k = max(1, min(int(raw_count), len(pairs)))
        labels = _cluster_fixed(vectors, k)

    # Stable S1/S2/... naming by first appearance (clip order, then time).
    seen: list[int] = []
    for lbl in labels:
        lbl = int(lbl)
        if lbl not in seen:
            seen.append(lbl)
    id_by_label = {lbl: f"S{i + 1}" for i, lbl in enumerate(seen)}

    for (_clip, seg), lbl in zip(pairs, labels, strict=True):
        seg["speaker"] = id_by_label[int(lbl)]

    speaker_ids = [id_by_label[lbl] for lbl in seen]
    project["speakers"] = _build_speaker_list(project, speaker_ids)
    store.save(project)
    log(f"Speaker diarization: {len(speaker_ids)} speaker(s) across {len(pairs)} segment(s).")


def label_for(project: dict, clip_id: str, start: float, end: float) -> str | None:
    """User-facing label (e.g. "Speaker 1") for the speaker who talks most
    during [start, end) of `clip_id`'s transcript, or None when the project
    has no diarized speakers / no labeled segment overlaps the window."""
    if not project.get("speakers"):
        return None
    try:
        clip = store.get_clip(project, clip_id)
    except KeyError:
        return None

    segments = (clip.get("transcript") or {}).get("segments", [])
    best_id, best_overlap = None, 0.0
    for seg in segments:
        spk = seg.get("speaker")
        if not spk:
            continue
        overlap = min(end, seg["end"]) - max(start, seg["start"])
        if overlap > best_overlap:
            best_overlap, best_id = overlap, spk

    if best_id is None:
        mid = (start + end) / 2
        for seg in segments:
            if seg.get("speaker") and seg["start"] <= mid <= seg["end"]:
                best_id = seg["speaker"]
                break

    if best_id is None:
        return None
    by_id = {sp["id"]: sp["label"] for sp in project["speakers"]}
    return by_id.get(best_id, best_id)


def speaker_prefix(project: dict, sentence: dict) -> str:
    """"<Label>: " prefix for a takes.py/review.py sentence dict (needs
    clip_id/start/end), or "" when the project has no diarized speakers or
    no speaker could be resolved for this sentence -- so cross-speaker
    "repetition" (e.g. host echoing guest) reads as distinct lines to the
    take_sequencer/reviewer/dedup_judge agents instead of looking like a
    duplicate take."""
    label = label_for(project, sentence["clip_id"], sentence["start"], sentence["end"])
    return f"{label}: " if label else ""
