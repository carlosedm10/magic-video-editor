"""Speaker-centered cropping: sample a few frames across a time range, detect
faces (OpenCV Haar — bundled, fully local), take the median face center, and
compute the 9:16 crop window. No LLM involved — this is geometry."""

import tempfile
from pathlib import Path

import cv2
import numpy as np

from .. import ffmpeg_utils

_cascade = None


def _detector():
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _cascade


def face_center(video_path: str, t_start: float, t_end: float,
                samples: int = 7) -> tuple[float, float] | None:
    """Median face center across sampled frames, as (x, y) fractions of the frame.
    None if no face is found."""
    det = _detector()
    centers = []
    times = np.linspace(t_start + 0.2, max(t_start + 0.3, t_end - 0.2), samples)
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            frame_path = Path(td) / f"f{i}.jpg"
            try:
                ffmpeg_utils.extract_frame(video_path, float(t), str(frame_path))
            except ffmpeg_utils.FFmpegError:
                continue
            img = cv2.imread(str(frame_path))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(60, 60))
            if len(faces) == 0:
                continue
            # biggest face = the speaker
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            H, W = img.shape[:2]
            centers.append(((x + w / 2) / W, (y + h / 2) / H))
    if not centers:
        return None
    cx = float(np.median([c[0] for c in centers]))
    cy = float(np.median([c[1] for c in centers]))
    return cx, cy


def vertical_crop_filter(src_w: int, src_h: int, center: tuple[float, float] | None,
                         out_w: int, out_h: int) -> str:
    """ffmpeg crop filter string for a 9:16 window centered on the face
    (falls back to frame center), sized to the largest crop that fits."""
    target_ar = out_w / out_h
    crop_h = src_h
    crop_w = int(crop_h * target_ar)
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(crop_w / target_ar)
    cx = (center[0] if center else 0.5) * src_w
    cy = (center[1] if center else 0.45) * src_h
    x = int(min(max(cx - crop_w / 2, 0), src_w - crop_w))
    y = int(min(max(cy - crop_h / 2, 0), src_h - crop_h))
    return f"crop={crop_w}:{crop_h}:{x}:{y}"
