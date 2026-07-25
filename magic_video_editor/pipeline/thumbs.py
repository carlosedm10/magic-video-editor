"""Timeline assets (spec v4 #4): per-clip filmstrip sprite + audio peaks for
the pro timeline UI.

For each clip missing artifacts:
  (a) filmstrip: one ffmpeg call samples ~1 frame every ~2s (capped at
      MAX_FRAMES) into a single horizontally-tiled JPEG (FRAME_W x FRAME_H
      per tile), e.g. `fps=0.5,scale=160:90,tile=Nx1`. A sidecar JSON
      records the grid so the frontend can slice it with CSS
      background-position (frame_w/frame_h/interval_s/count). Long clips
      widen the sampling interval so the tile count never exceeds the cap.
  (b) peaks: ~PEAK_COUNT max-abs amplitude samples (0..1) over the clip's
      analysis wav, for a lightweight waveform strip under the timeline
      blocks.

run(log, project) is idempotent: it only (re)builds clips where
clip.get("thumbs") is falsy, and persists clip["thumbs"] =
{"strip", "meta", "peaks"} (strip/meta are None for clips with no video
track — peaks-only in that case).
"""

import json
import math

import cv2
import numpy as np

from .. import ffmpeg_utils, store

FRAME_W, FRAME_H = 160, 90
TARGET_INTERVAL_S = 2.0
MAX_FRAMES = 120
PEAK_COUNT = 2000


def _paths(thumbs_dir, clip_id: str):
    return (
        thumbs_dir / f"{clip_id}_strip.jpg",
        thumbs_dir / f"{clip_id}_strip.json",
        thumbs_dir / f"{clip_id}_peaks.json",
    )


def _plan(duration: float) -> tuple[float, int]:
    """Return (interval_s, frame_count): ~1 frame per TARGET_INTERVAL_S,
    widening the interval instead of exceeding MAX_FRAMES for long clips."""
    if duration <= 0:
        return TARGET_INTERVAL_S, 1
    count = math.ceil(duration / TARGET_INTERVAL_S)
    if count > MAX_FRAMES:
        return duration / MAX_FRAMES, MAX_FRAMES
    return TARGET_INTERVAL_S, max(1, count)


def _build_strip(clip: dict, strip_path, meta_path) -> None:
    duration = clip.get("info", {}).get("duration", 0.0)
    interval_s, count = _plan(duration)
    fps = 1.0 / interval_s if interval_s > 0 else 0.5
    vf = f"fps={fps},scale={FRAME_W}:{FRAME_H},tile={count}x1"
    ffmpeg_utils.run(
        [
            ffmpeg_utils.ffmpeg_bin(),
            "-y",
            "-i",
            clip["path"],
            "-vf",
            vf,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(strip_path),
        ]
    )
    # Read back the real sprite (cv2, no PIL dep) so the meta reflects what
    # ffmpeg actually produced -- short clips can yield fewer tiles than planned.
    img = cv2.imread(str(strip_path))
    if img is None:
        raise ffmpeg_utils.FFmpegError(f"filmstrip generation produced no image for {clip['id']}")
    w = img.shape[1]
    actual_count = max(1, round(w / FRAME_W))
    meta_path.write_text(
        json.dumps(
            {
                "frame_w": FRAME_W,
                "frame_h": FRAME_H,
                "interval_s": round(interval_s, 3),
                "count": actual_count,
            }
        )
    )


def _build_peaks(clip: dict, peaks_path) -> None:
    wav = clip.get("wav")
    if not wav:
        peaks_path.write_text(json.dumps([]))
        return
    samples = ffmpeg_utils.load_wav_mono(wav)
    if samples.size == 0:
        peaks_path.write_text(json.dumps([]))
        return
    n = min(PEAK_COUNT, samples.size)
    bounds = np.linspace(0, samples.size, n + 1).astype(np.int64)
    peaks = []
    for i in range(n):
        lo, hi = bounds[i], bounds[i + 1]
        chunk = samples[lo:hi] if hi > lo else samples[lo : lo + 1]
        # samples are already float32 in [-1, 1] (ffmpeg_utils.load_wav_mono),
        # so max-abs is already 0..1 -- no extra normalization needed.
        if chunk.size:
            peak = float(np.clip(np.max(np.abs(chunk)), 0.0, 1.0))
        else:
            peak = 0.0
        peaks.append(round(peak, 4))
    peaks_path.write_text(json.dumps(peaks))


def run(log, project: dict) -> None:
    thumbs_dir = store.project_dir(project["id"]) / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    changed = False
    for clip in project.get("clips", []):
        if clip.get("thumbs"):
            continue
        strip_path, meta_path, peaks_path = _paths(thumbs_dir, clip["id"])
        name = clip.get("filename", clip["id"])
        has_video = bool(clip.get("info", {}).get("has_video"))

        strip_out, meta_out = None, None
        if has_video:
            log(f"Generating filmstrip for {name}")
            _build_strip(clip, strip_path, meta_path)
            strip_out, meta_out = str(strip_path), str(meta_path)

        log(f"Computing audio peaks for {name}")
        _build_peaks(clip, peaks_path)

        clip["thumbs"] = {"strip": strip_out, "meta": meta_out, "peaks": str(peaks_path)}
        changed = True
        store.save(project)

    if not changed:
        log("Nothing to do: every clip already has thumbs.")


# Register with the queue's per-kind runner table (spec v4 #2). queue.py was
# being built in parallel by another agent -- this stays a no-op if it isn't
# present yet, so thumbs.py can ship independently either way.
try:
    from .. import queue

    queue.register_runner("thumbs", lambda log, project, payload=None: run(log, project))
except ImportError:
    pass
