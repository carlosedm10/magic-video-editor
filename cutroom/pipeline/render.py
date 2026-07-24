"""Stage 6 — Render the main cut: EDL segments -> per-segment frame-accurate
re-encode (normalized to the main camera's format) -> lossless concat.
If a camera clip is synced with an external audio recording, its audio is
replaced by the aligned external track."""

import time

from .. import config, ffmpeg_utils, store
from . import ordering, sync


def _target_format(project: dict) -> tuple[int, int, float]:
    mains = [c for c in project["clips"] if c["is_main"] and c["info"] and c["info"]["has_video"]]
    cams = mains or [c for c in project["clips"] if c["info"] and c["info"]["has_video"]]
    if not cams:
        raise RuntimeError("No video clips in project.")
    info = cams[0]["info"]
    return info["width"], info["height"], info["fps"] or 30.0


def run(log, project: dict) -> None:
    segments = ordering.build_edl(project)
    if not segments:
        raise RuntimeError("EDL is empty — no kept sentences to render.")

    width, height, fps = _target_format(project)
    pdir = store.project_dir(project["id"])
    work = pdir / "work" / f"render_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)

    log(f"Rendering {len(segments)} segments at {width}x{height}@{fps:g}...")
    seg_paths = []
    for i, seg in enumerate(segments):
        clip = store.get_clip(project, seg["clip_id"])
        audio = sync.audio_source_for(project, seg["clip_id"])
        audio_src, audio_start = (None, None)
        if audio:
            audio_src, delta = audio
            audio_start = seg["start"] + delta
            if audio_start < 0:
                audio_src, audio_start = None, None  # external track starts later
        out = work / f"seg_{i:04d}.mp4"
        ffmpeg_utils.cut_segment(
            clip["path"], seg["start"], seg["end"], str(out),
            width, height, fps, audio_src=audio_src, audio_start=audio_start,
        )
        seg_paths.append(str(out))
        log.progress((i + 1) / (len(segments) + 1))
        log(f"seg {i + 1}/{len(segments)}: {clip['filename']} "
            f"{seg['start']:.1f}-{seg['end']:.1f}s"
            + (" [external audio]" if audio_src else ""))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    final = pdir / f"maincut_{stamp}.mp4"
    log("Concatenating...")
    ffmpeg_utils.concat_segments(seg_paths, str(final), work)

    total = sum(s["end"] - s["start"] for s in segments)
    project.setdefault("renders", []).append({
        "path": str(final), "at": stamp, "segments": len(segments),
        "duration": round(total, 1),
    })
    store.save(project)
    log(f"Done: {final.name} ({total:.0f}s from {len(segments)} segments).")
