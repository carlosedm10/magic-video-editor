"""Stage 4 — Take analysis: split transcripts into sentences, find repeated
takes of the same line across all clips, score each take, keep the best.

Scoring is heuristic v1 (transcript disfluencies + completeness + loudness
stability from the wav), with an optional LLM tiebreak for close calls.
"""

import re
import uuid

import numpy as np
from rapidfuzz import fuzz

from .. import config, ffmpeg_utils, llm, store

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
            sentences.append({
                "id": uuid.uuid4().hex[:8],
                "clip_id": clip["id"],
                "start": round(cur[0]["s"], 3),
                "end": round(cur[-1]["e"], 3),
                "text": text_so_far,
                "words": cur,
            })
            cur = []
    if cur:
        sentences.append({
            "id": uuid.uuid4().hex[:8], "clip_id": clip["id"],
            "start": round(cur[0]["s"], 3), "end": round(cur[-1]["e"], 3),
            "text": " ".join(x["w"] for x in cur), "words": cur,
        })
    return sentences


def _norm(text: str) -> str:
    t = FILLERS.sub(" ", text.lower())
    t = re.sub(r"[^\w\sáéíóúüñàèìòùç]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _rms_stability(wav: np.ndarray, start: float, end: float) -> float:
    sr = config.ANALYSIS_SR
    seg = wav[int(start * sr): int(end * sr)]
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
    restarts = sum(1 for k in range(len(toks) - 3)
                   if toks[k] == toks[k + 2] and toks[k + 1] == toks[k + 3])
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
    why = (f"fillers={fillers} restarts={restarts} complete={bool(complete)} "
           f"rate={rate:.1f}w/s stability={stability:.2f} take#{take_index + 1}")
    return round(score, 2), why


def _llm_tiebreak(candidates: list[dict]) -> str | None:
    """Ask the local LLM which near-tied take reads best. Returns sentence id or None."""
    try:
        listing = "\n".join(f'{i}: "{c["text"]}"' for i, c in enumerate(candidates))
        result = llm.chat_json(
            "You judge which take of the same spoken line is best for the final video: "
            "fluent, complete, natural, no false starts. Answer JSON: {\"best\": <index>}.",
            f"Takes of the same line:\n{listing}",
            timeout=60,
        )
        idx = int(result.get("best", -1))
        if 0 <= idx < len(candidates):
            return candidates[idx]["id"]
    except Exception:
        pass
    return None


def run(log, project: dict) -> None:
    clips = [c for c in project["clips"] if c.get("transcript") and c["role"] == "camera"]
    if not clips:
        raise RuntimeError("Run Transcribe first.")

    # In a multi-cam sync group, only analyze the main camera's transcript
    # (same audio content on every angle would look like duplicates).
    grouped_non_main = set()
    for g in project.get("sync_groups", []):
        members = [m["clip_id"] for m in g["members"]]
        cams = [m for m in members
                if store.get_clip(project, m)["role"] == "camera"]
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

    # Cluster near-duplicate sentences (repeated takes), greedy by similarity.
    log("Detecting repeated takes...")
    norm = {s["id"]: _norm(s["text"]) for s in sentences}
    assigned: dict[str, str] = {}
    groups: list[list[dict]] = []
    for i, s in enumerate(sentences):
        if s["id"] in assigned or len(norm[s["id"]].split()) < config.DUP_MIN_WORDS:
            continue
        cluster = [s]
        assigned[s["id"]] = s["id"]
        for t in sentences[i + 1:]:
            if t["id"] in assigned or len(norm[t["id"]].split()) < config.DUP_MIN_WORDS:
                continue
            if fuzz.token_sort_ratio(norm[s["id"]], norm[t["id"]]) >= config.DUP_SIMILARITY:
                cluster.append(t)
                assigned[t["id"]] = s["id"]
        if len(cluster) > 1:
            groups.append(cluster)

    use_llm = llm.available()
    log(f"{len(groups)} repeated-take group(s). LLM tiebreak: {'on' if use_llm else 'off (ollama down)'}")

    # Score everything; pick winners inside duplicate groups.
    take_counter: dict[str, int] = {}
    for s in sentences:
        root = assigned.get(s["id"], s["id"])
        idx = take_counter.get(root, 0)
        take_counter[root] = idx + 1
        s["score"], s["why"] = _score(s, wavs.get(s["clip_id"]), idx)
        s["kept"] = True
        s["dup_group"] = None
        s["reason"] = ""

    for gi, cluster in enumerate(groups):
        best = max(cluster, key=lambda s: s["score"])
        if use_llm and len(cluster) <= 6:
            top = sorted(cluster, key=lambda s: -s["score"])[:3]
            if len(top) > 1 and top[0]["score"] - top[1]["score"] < 1.5:
                pick = _llm_tiebreak(top)
                if pick:
                    best = next(s for s in cluster if s["id"] == pick)
        for s in cluster:
            s["dup_group"] = f"d{gi}"
            if s["id"] != best["id"]:
                s["kept"] = False
                s["reason"] = f"repeated take — kept better version (score {best['score']} vs {s['score']})"

    # Drop tiny fragments that survived (interjections, aborted starts).
    for s in sentences:
        if s["kept"] and len(norm[s["id"]].split()) < 3 and (s["end"] - s["start"]) < 1.2:
            s["kept"] = False
            s["reason"] = "fragment / aborted start"

    for s in sentences:
        s.pop("words", None)  # words stay in the clip transcript; keep project.json light

    project["sentences"] = sentences
    kept = sum(1 for s in sentences if s["kept"])
    store.save(project)
    log(f"Kept {kept}/{len(sentences)} sentences.")
