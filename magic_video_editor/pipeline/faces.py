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
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _cascade


def face_center(
    video_path: str, t_start: float, t_end: float, samples: int = 7
) -> tuple[float, float] | None:
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
            faces = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
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


def face_bbox_at(video_path: str, t: float) -> tuple[float, float, float, float] | None:
    """Single-frame biggest-face bounding box as (x, y, w, h) fractions of the
    SOURCE frame, at time `t`. A single-sample sibling of `face_center`'s
    multi-sample median center — added for pipeline/safezones.py (spec v7.7),
    which needs the actual box (not just a center point) to intersect against
    platform safe zones per sampled frame. None if no face is found or the
    frame can't be extracted."""
    det = _detector()
    with tempfile.TemporaryDirectory() as td:
        frame_path = Path(td) / "f.jpg"
        try:
            ffmpeg_utils.extract_frame(video_path, float(t), str(frame_path))
        except ffmpeg_utils.FFmpegError:
            return None
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        if len(found) == 0:
            return None
        x, y, w, h = max(found, key=lambda f: f[2] * f[3])
        H, W = img.shape[:2]
        return (x / W, y / H, w / W, h / H)


def manual_center(crop_x: float) -> tuple[float, float]:
    """(x, y) center fraction for a manual `crop_x` override (Reel Editor
    framing drag, spec v5): x = crop_x clamped to 0..1, y = the same 0.45
    default used when no face is detected. Callers pass the result straight
    into `vertical_crop_filter` in place of `face_center(...)`'s output,
    bypassing face detection entirely when the user has set an explicit
    horizontal center."""
    return (max(0.0, min(1.0, float(crop_x))), 0.45)


def vertical_crop_filter(
    src_w: int, src_h: int, center: tuple[float, float] | None, out_w: int, out_h: int
) -> str:
    """ffmpeg crop filter string for a 9:16 window centered on the face
    (falls back to frame center), sized to the largest crop that fits.

    Kept as-is (spec v7.11 doesn't touch it) -- it's exactly
    `transform_crop_rect(..., zoom=1.0, offset_x=0.0, offset_y=0.0, ...)`,
    still used by anything that hasn't moved onto the transform model. See
    `transform_crop_rect` below for the generalized version reels.py's
    renderer now uses."""
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


# ---------- transform model (spec v7.11 "Reel framing v2") ----------
#
# Replaces the {crop_x, fit_mode, fit_scale} trio with reel["transform"] =
# {zoom, offset_x, offset_y}. This is the SINGLE SOURCE OF TRUTH for turning
# a transform into source-frame crop geometry -- pipeline/reels.py's
# render_reel (fg crop+scale, blurred cover bg underlay) and the Reel
# Editor's live CSS preview (ui/editor/reeleditor.js) both derive their
# numbers from `transform_crop_rect`/`transform_needs_blur` below, and
# pipeline/safezones.py's face-safety mapping is meant to adopt the same
# functions (see that module's docstring / this module's usage from
# reels.py for the exact call shape -- not wired here, safezones.py is owned
# by a different in-flight task).
#
# Geometry, derived once and reused by every caller:
#   base_w, base_h = the classic zoom=1.0 "full-height 9:16 crop" (exactly
#     `vertical_crop_filter`'s own crop_w/crop_h before centering) -- by
#     construction base_h == src_h whenever the source is wider than 9:16
#     (the overwhelmingly common camera/screen-recording case), so there is
#     no unused vertical margin at zoom=1.0: the crop is already as tall as
#     the source can give it.
#   zoom > 1.0 ("punches in"): crop_w = base_w/zoom, crop_h = base_h/zoom,
#     both shrink -- the crop keeps the SAME 9:16 aspect ratio, so it always
#     maps 1:1 onto the 1080x1920 output with no blur needed.
#   zoom < 1.0 ("opens the window wider than the frame"): crop_h = base_h/
#     zoom would need to exceed src_h, but the crop can never be taller than
#     the actual source frame -- so crop_h clamps at src_h while crop_w keeps
#     growing (up to src_w). The resulting crop is now WIDER than 9:16 (not
#     the same shape anymore) -- exactly "the source window exceeds the
#     frame" from the spec. Because base_h == src_h already at zoom==1.0,
#     the clamp kicks in for ANY zoom < 1.0 -- the cover threshold is simply
#     1.0, no separate constant to compute or drift out of sync.
#   offset_x/offset_y (-1..1): pan the window's center by up to half of
#     whatever room is left over (src_w - crop_w)/2, (src_h - crop_h)/2 --
#     naturally 0 room (no-op) whenever a dimension is already maxed out
#     (e.g. crop_w clamped to src_w at an extreme zoom-out).

TRANSFORM_ZOOM_MIN, TRANSFORM_ZOOM_MAX = 0.5, 3.0
TRANSFORM_OFFSET_MIN, TRANSFORM_OFFSET_MAX = -1.0, 1.0
# Zoom below this needs the blurred cover-background underlay -- see the
# module note above: with base_h == src_h (the normal case), this is always
# exactly 1.0, so it's a plain constant rather than something computed per
# source. `transform_needs_blur` is still the one place that decides this
# (from the actual crop aspect ratio) so a caller never has to duplicate the
# "is it wider than 9:16" comparison itself.
TRANSFORM_COVER_THRESHOLD_ZOOM = 1.0


def transform_crop_rect(
    src_w: int,
    src_h: int,
    center: tuple[float, float] | None,
    zoom: float,
    offset_x: float,
    offset_y: float,
    out_w: int,
    out_h: int,
) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the source-frame crop window for {zoom, offset_x,
    offset_y} (spec v7.11) -- generalizes `vertical_crop_filter`'s fixed
    9:16-crop math (that's exactly zoom=1.0, offset=0, offset=0, unchanged
    bit-for-bit). See the module note above for the derivation. `center` is
    the same (x, y) source-fraction point `vertical_crop_filter` takes
    (face_center(...) result or manual_center(crop_x)); None falls back to
    frame center (0.5, 0.45), same default."""
    target_ar = out_w / out_h
    base_h = src_h
    base_w = int(base_h * target_ar)
    if base_w > src_w:
        base_w = src_w
        base_h = int(base_w / target_ar)

    z = min(TRANSFORM_ZOOM_MAX, max(TRANSFORM_ZOOM_MIN, float(zoom)))
    crop_w = min(src_w, max(2, round(base_w / z)))
    crop_h = min(src_h, max(2, round(base_h / z)))

    cx = (center[0] if center else 0.5) * src_w
    cy = (center[1] if center else 0.45) * src_h

    ox = min(TRANSFORM_OFFSET_MAX, max(TRANSFORM_OFFSET_MIN, float(offset_x)))
    oy = min(TRANSFORM_OFFSET_MAX, max(TRANSFORM_OFFSET_MIN, float(offset_y)))
    room_x = max(0.0, (src_w - crop_w) / 2.0)
    room_y = max(0.0, (src_h - crop_h) / 2.0)

    x = cx - crop_w / 2.0 + ox * room_x
    y = cy - crop_h / 2.0 + oy * room_y
    x = int(min(max(x, 0.0), src_w - crop_w))
    y = int(min(max(y, 0.0), src_h - crop_h))
    return x, y, crop_w, crop_h


def transform_needs_blur(zoom: float) -> bool:
    """True when `zoom` is below TRANSFORM_COVER_THRESHOLD_ZOOM (the source
    window opens wider than the 9:16 frame) and a blurred cover-background
    underlay is needed to fill the top/bottom gap.

    Deliberately compares the raw `zoom` float rather than re-deriving the
    same question from `transform_crop_rect`'s ROUNDED pixel output
    (crop_w/crop_h): at zoom values right at the boundary (1.0, or a clean
    2:1 punch-in like 2.0), rounding either dimension to the nearest pixel
    can nudge crop_w/crop_h a fraction of a percent past the exact target
    ratio in either direction -- comparing zoom itself against the exact,
    unrounded threshold sidesteps that pixel-rounding noise entirely."""
    return float(zoom) < TRANSFORM_COVER_THRESHOLD_ZOOM - 1e-9


def transform_vertical_crop_filter(
    src_w: int,
    src_h: int,
    center: tuple[float, float] | None,
    zoom: float,
    offset_x: float,
    offset_y: float,
    out_w: int,
    out_h: int,
) -> str:
    """ffmpeg filtergraph fragment for one segment's {zoom, offset_x,
    offset_y} transform (spec v7.11), slotting into `_encode_segment`'s
    vf_extra exactly like `vertical_crop_filter`'s plain crop string did.

    zoom >= cover threshold (crop keeps the 9:16 aspect ratio): a bare
    `crop=w:h:x:y` -- `_encode_segment` appends its own
    scale=out_w:out_h,pad=...,fps=... chain right after via a plain comma,
    same as before.

    zoom < cover threshold (crop is wider than 9:16 -- "the source window
    exceeds the frame"): a split -> blurred/darkened cover-fill background
    (same crop content, scaled to fill out_w x out_h and cropped) + the crop
    scaled to fit out_w's width (preserving its own now-wider aspect ratio),
    centered -- one filtergraph string (split -> two labeled chains ->
    overlay), mirroring the shape of the old (spec v7.7) `_fit_blur_vf` in
    pipeline/reels.py, which this supersedes."""
    x, y, crop_w, crop_h = transform_crop_rect(
        src_w, src_h, center, zoom, offset_x, offset_y, out_w, out_h
    )
    crop = f"crop={crop_w}:{crop_h}:{x}:{y}"
    if not transform_needs_blur(zoom):
        return crop
    return (
        f"{crop},split=2[trbg][trfg];"
        f"[trbg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},boxblur=20:1,eq=brightness=-0.08[trbgout];"
        f"[trfg]scale={out_w}:-2:force_original_aspect_ratio=decrease[trfgout];"
        "[trbgout][trfgout]overlay=(W-w)/2:(H-h)/2"
    )
