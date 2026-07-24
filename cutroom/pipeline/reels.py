"""Stage 7 — Reels: propose ~20 short-form candidates from the kept content,
scored by the local LLM on the transcript (hook / clarity / payoff). Rendering
a candidate crops to 9:16 centered on the speaker's face and burns subtitles
built from whisper word timestamps."""

import time
import uuid

from .. import config, ffmpeg_utils, llm, store
from . import faces


def _candidate_windows(project: dict) -> list[dict]:
    """Sliding windows of consecutive kept sentences within one clip,
    bounded to REEL_MIN_S..REEL_MAX_S."""
    out = []
    for clip in project["clips"]:
        if clip["role"] != "camera":
            continue
        sents = sorted((s for s in project["sentences"]
                        if s["clip_id"] == clip["id"] and s["kept"]),
                       key=lambda s: s["start"])
        for i in range(len(sents)):
            acc, texts = [], []
            for j in range(i, len(sents)):
                if acc and sents[j]["start"] - acc[-1]["end"] > 4.0:
                    break  # big content hole — not one continuous thought
                acc.append(sents[j])
                texts.append(sents[j]["text"])
                dur = acc[-1]["end"] - acc[0]["start"]
                if dur > config.REEL_MAX_S:
                    break
                if dur >= config.REEL_MIN_S:
                    out.append({
                        "clip_id": clip["id"],
                        "start": acc[0]["start"],
                        "end": acc[-1]["end"],
                        "text": " ".join(texts),
                        "duration": round(dur, 1),
                    })
                    if dur >= config.REEL_MAX_S * 0.75:
                        break
    # thin out: keep at most ~80 for the LLM pass, longest-first per start point
    seen, thinned = set(), []
    for c in sorted(out, key=lambda c: (c["clip_id"], c["start"], -c["duration"])):
        key = (c["clip_id"], round(c["start"] / 5))
        if key in seen:
            continue
        seen.add(key)
        thinned.append(c)
    return thinned[:80]


def _overlap(a: dict, b: dict) -> float:
    if a["clip_id"] != b["clip_id"]:
        return 0.0
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return max(0.0, inter) / union


def suggest(log, project: dict) -> None:
    if not project.get("sentences"):
        raise RuntimeError("Run Take analysis first.")
    if not llm.available():
        raise RuntimeError("Ollama is not running — reel scoring needs the local LLM.")

    candidates = _candidate_windows(project)
    if not candidates:
        raise RuntimeError("No candidate windows (need >=15s of continuous kept speech).")
    log(f"Scoring {len(candidates)} candidate windows with {config.OLLAMA_MODEL}...")

    scored = []
    batch = 8
    for bi in range(0, len(candidates), batch):
        chunk = candidates[bi: bi + batch]
        listing = "\n\n".join(
            f'CANDIDATE {i} ({c["duration"]}s):\n"{c["text"][:900]}"'
            for i, c in enumerate(chunk)
        )
        try:
            result = llm.chat_json(
                "You pick short-form clips (Reels/TikTok) from long-form video transcripts. "
                "Score each candidate 0-10 on: hook (grabs attention in the first line), "
                "self_contained (understandable without context), payoff (delivers value/punchline). "
                'Answer JSON: {"scores": [{"i": idx, "hook": n, "self_contained": n, '
                '"payoff": n, "title": "catchy 5-8 word title"}]}',
                listing,
            )
            for s in result.get("scores", []):
                i = int(s["i"])
                if 0 <= i < len(chunk):
                    c = dict(chunk[i])
                    c["hook"] = float(s.get("hook", 0))
                    c["self_contained"] = float(s.get("self_contained", 0))
                    c["payoff"] = float(s.get("payoff", 0))
                    c["title"] = str(s.get("title", ""))[:80]
                    c["score"] = round(c["hook"] * 1.4 + c["self_contained"] + c["payoff"], 1)
                    scored.append(c)
        except llm.LLMError as e:
            log(f"batch {bi // batch}: LLM error, skipping ({e})")
        log.progress(min(0.95, (bi + batch) / len(candidates)))

    # top N with limited mutual overlap
    scored.sort(key=lambda c: -c["score"])
    picked = []
    for c in scored:
        if all(_overlap(c, p) < 0.45 for p in picked):
            picked.append(c)
        if len(picked) >= config.REEL_SUGGESTIONS:
            break

    project["reels"] = [{
        "id": uuid.uuid4().hex[:8],
        "rank": i + 1,
        "clip_id": c["clip_id"],
        "start": c["start"], "end": c["end"], "duration": c["duration"],
        "title": c.get("title", ""), "score": c["score"],
        "hook": c["hook"], "self_contained": c["self_contained"], "payoff": c["payoff"],
        "text": c["text"],
        "path": None, "status": "suggested",
    } for i, c in enumerate(picked)]
    store.save(project)
    log(f"Suggested {len(picked)} reels (from {len(scored)} scored candidates).")


def _srt_for_window(clip: dict, start: float, end: float, srt_path: str) -> None:
    """Karaoke-ish subtitles: cues of ~4 words from whisper word timestamps,
    re-based to the reel's local time."""
    words = []
    for seg in clip["transcript"]["segments"]:
        words.extend(w for w in seg["words"] if start <= w["s"] < end)

    def ts(t: float) -> str:
        t = max(0.0, t - start)
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"

    lines, cue = [], []
    idx = 1
    for i, w in enumerate(words):
        cue.append(w)
        nxt_gap = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 99
        if len(cue) >= 4 or nxt_gap > 0.6:
            lines.append(f"{idx}\n{ts(cue[0]['s'])} --> {ts(cue[-1]['e'])}\n"
                         f"{' '.join(x['w'] for x in cue)}\n")
            cue, idx = [], idx + 1
    with open(srt_path, "w") as f:
        f.write("\n".join(lines))


def render_reel(log, project: dict, reel_id: str) -> None:
    reel = next((r for r in project["reels"] if r["id"] == reel_id), None)
    if not reel:
        raise RuntimeError(f"reel {reel_id} not found")
    clip = store.get_clip(project, reel["clip_id"])
    pdir = store.project_dir(project["id"])
    reels_dir = pdir / "reels"
    reels_dir.mkdir(exist_ok=True)

    log("Analyzing frames for speaker position...")
    center = faces.face_center(clip["path"], reel["start"], reel["end"])
    log(f"Face center: {center if center else 'not found — using frame center'}")
    log.progress(0.3)

    crop = faces.vertical_crop_filter(
        clip["info"]["width"], clip["info"]["height"], center,
        config.REEL_W, config.REEL_H)

    srt = str(pdir / "work" / f"reel_{reel_id}.srt")
    _srt_for_window(clip, reel["start"], reel["end"], srt)

    out = reels_dir / f"reel_{reel['rank']:02d}_{reel_id}.mp4"
    log(f"Rendering {config.REEL_W}x{config.REEL_H} with subtitles...")
    ffmpeg_utils.cut_segment(
        clip["path"], reel["start"], reel["end"], str(out),
        config.REEL_W, config.REEL_H, min(clip["info"]["fps"] or 30.0, 60.0),
        vf_extra=crop, srt_path=srt,
    )
    reel["path"] = str(out)
    reel["status"] = "rendered"
    reel["rendered_at"] = time.strftime("%H:%M:%S")
    store.save(project)
    log(f"Done: {out.name}")
