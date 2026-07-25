"""Reel social safe zones + deterministic face safety (spec v7.7).

Owner decision (locked): reel safety is DETERMINISTIC geometry — face bbox
(OpenCV Haar, via faces.face_bbox_at) intersected with published per-platform
UI safe-zone rectangles. NO vision-LLM / screenshot judging.

## Zone specs (fractions of a 1080x1920 canvas)

Researched 2026-07-25 (web search — no official machine-readable spec is
published by any of the three platforms; these are the converged consensus
numbers across current creator/agency safe-zone guides, cited per platform
below). Each zone is a rectangle {x, y, w, h} as a fraction of 1080x1920, in
the same fraction space the frontend mockups and this module's geometry both
use — the UI agent can render its CSS/SVG overlay directly from these
fractions without knowing the 1080x1920 pixel base.

- TikTok: top ~130px profile bar / status area, bottom ~484px caption +
  sound attribution + engagement row, right ~140px like/comment/share/
  bookmark icon column (icon column runs roughly the middle-to-lower half of
  the frame, above the caption band, per the sources below).
  Sources: quso.ai "TikTok Dimensions 2026: The Ultimate Spec & Safe Zone
  Guide" (top 130px / bottom 484px / right 140px); Kreatli "TikTok Safe Zone
  (2026)"; EzUGC "TikTok Safe Zone Guide 2026" (top ~140px, bottom ~324px
  caption band + separate engagement column, right ~164px icon column).
- Instagram Reels: top ~108px, bottom ~320px caption/handle/audio band,
  right ~120px like/comment/share/remix icon column (icon column spans
  roughly the lower two-thirds, above the caption band).
  Sources: Outfy "Instagram Safe Zone Guide 2026" (top 108 / bottom 320 /
  left 60 / right 120px); Kreatli "Instagram Reels Safe Zone Guide"
  (right ~90-120px icon column, bottom-left ~200px caption block).
- YouTube Shorts: top ~180px search/nav bar, bottom ~390px channel info +
  music ticker, right ~120px subscribe/like/comment/share column.
  Sources: youtubetoolkit.com "YouTube Shorts Dimensions: The Safe Zone
  Pixel Map"; Somake AI "YouTube Shorts Aspect Ratio 2026" (variants cited:
  Google's official vertical safe-zone overlay puts the CONTENT-safe area at
  840x960 upper-center, i.e. a much larger reserved margin for Shorts ads
  specifically — we use the more common creator-facing numbers above for
  organic Shorts UI, since that's what the reel actually plays behind).

## Existing open-source safe-zone template search (per spec: "search first")

Found: github.com/Creative-Crafter/davinci-shortform-overlays (MIT license) —
a DaVinci Resolve .drfx template bundling TikTokOverlay.png /
YouTubeShortsOverlay.png / InstagramReelsOverlay.png toggle overlays. NOT
vendored: (1) it ships opaque PNG overlays baked for DaVinci Resolve's
inspector, not the zone-fraction data our web frontend needs to draw its own
CSS/SVG mockup or that this module needs to do geometry against; (2) no
numeric spec is published alongside it (the margins are only implicit in the
raster art) — extracting fractions back out of the PNGs would be less
reliable than the cited numeric guides above. Nothing else turned up in
open-source/permissive form (the rest of the search results are all
proprietary SaaS "safe zone checker" tools, not licensed assets). Conclusion:
hand-built zone rectangles below, sourced from the cited guides, are the
better fit — the UI agent gets clean fractions instead of a foreign raster
asset to reverse-engineer.

## Face-safety geometry

`analyze()` samples ~9 frames across the reel's effective window (segments,
in/out overrides — same resolution reels.py's renderer uses), gets each
sampled frame's face bbox in SOURCE fraction coords, maps it into 9:16
OUTPUT fraction coords honoring the reel's crop center (crop_x override or
face-detected center) and fit_mode/fit_scale (spec v7.7's "fill" vs
"fit_blur" one-click fix), then intersects against the platform's zones.
"""

from __future__ import annotations

from .. import store
from . import faces
from . import reels as _reels

REEL_W, REEL_H = 1080, 1920

SAMPLE_COUNT = 9
FIT_SCALE_MIN, FIT_SCALE_MAX, FIT_SCALE_STEP = 0.6, 1.0, 0.05

PLATFORMS: dict[str, dict] = {
    "tiktok": {
        "label": "TikTok",
        "zones": [
            {"name": "top_bar", "x": 0.0, "y": 0.0, "w": 1.0, "h": 130 / REEL_H},
            {
                "name": "right_rail",
                "x": 1.0 - 140 / REEL_W,
                "y": 0.28,
                "w": 140 / REEL_W,
                "h": 0.75 - 0.28,
            },
            {
                "name": "bottom_caption",
                "x": 0.0,
                "y": 1.0 - 484 / REEL_H,
                "w": 1.0,
                "h": 484 / REEL_H,
            },
        ],
    },
    "reels": {
        "label": "Instagram Reels",
        "zones": [
            {"name": "top_bar", "x": 0.0, "y": 0.0, "w": 1.0, "h": 108 / REEL_H},
            {
                "name": "right_rail",
                "x": 1.0 - 120 / REEL_W,
                "y": 0.30,
                "w": 120 / REEL_W,
                "h": (1.0 - 320 / REEL_H) - 0.30,
            },
            {
                "name": "bottom_caption",
                "x": 0.0,
                "y": 1.0 - 320 / REEL_H,
                "w": 1.0,
                "h": 320 / REEL_H,
            },
        ],
    },
    "shorts": {
        "label": "YouTube Shorts",
        "zones": [
            {"name": "top_bar", "x": 0.0, "y": 0.0, "w": 1.0, "h": 180 / REEL_H},
            {
                "name": "right_rail",
                "x": 1.0 - 120 / REEL_W,
                "y": 0.25,
                "w": 120 / REEL_W,
                "h": (1.0 - 390 / REEL_H) - 0.25,
            },
            {
                "name": "bottom_caption",
                "x": 0.0,
                "y": 1.0 - 390 / REEL_H,
                "w": 1.0,
                "h": 390 / REEL_H,
            },
        ],
    },
}


def _rects_intersect(a: tuple[float, float, float, float], b: dict) -> bool:
    ax, ay, aw, ah = a
    return ax < b["x"] + b["w"] and ax + aw > b["x"] and ay < b["y"] + b["h"] and ay + ah > b["y"]


def _hit_zone(bbox: tuple[float, float, float, float], zones: list[dict]) -> str | None:
    for z in zones:
        if _rects_intersect(bbox, z):
            return z["name"]
    return None


def _crop_rect(
    src_w: int, src_h: int, center: tuple[float, float] | None
) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the 9:16 crop window in SOURCE pixels — mirrors
    faces.vertical_crop_filter's own math exactly (that function returns an
    ffmpeg filter string; this returns the numbers analyze() needs)."""
    target_ar = REEL_W / REEL_H
    crop_h = src_h
    crop_w = int(crop_h * target_ar)
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(crop_w / target_ar)
    cx = (center[0] if center else 0.5) * src_w
    cy = (center[1] if center else 0.45) * src_h
    x = int(min(max(cx - crop_w / 2, 0), src_w - crop_w))
    y = int(min(max(cy - crop_h / 2, 0), src_h - crop_h))
    return x, y, crop_w, crop_h


def _bbox_to_crop_relative(
    bbox_src_frac: tuple[float, float, float, float],
    src_w: int,
    src_h: int,
    crop_rect: tuple[int, int, int, int],
) -> tuple[float, float, float, float] | None:
    """Face bbox (source-frame fractions) -> fraction of the CROP window
    (i.e. what "fill" mode maps 1:1 onto the 1080x1920 output). None if the
    face falls entirely outside the crop (cropped away, nothing to check)."""
    fx, fy, fw, fh = bbox_src_frac
    px, py, pw, ph = fx * src_w, fy * src_h, fw * src_w, fh * src_h
    cx, cy, cw, ch = crop_rect
    rx = (px - cx) / cw
    ry = (py - cy) / ch
    rw = pw / cw
    rh = ph / ch
    if rx + rw <= 0 or rx >= 1 or ry + rh <= 0 or ry >= 1:
        return None
    # clamp partial overlaps into the visible 0..1 window
    x0, x1 = max(0.0, rx), min(1.0, rx + rw)
    y0, y1 = max(0.0, ry), min(1.0, ry + rh)
    return x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0)


def _crop_relative_to_output(
    rel: tuple[float, float, float, float], fit_mode: str, fit_scale: float
) -> tuple[float, float, float, float]:
    """Crop-relative fraction -> OUTPUT (1080x1920) fraction, honoring
    fit_mode. "fill" (default): the crop maps 1:1 onto the full output
    canvas, so output fraction == crop-relative fraction. "fit_blur": the
    same crop is scaled down to `fit_scale` and centered on the canvas (the
    rest is blurred background per spec) — foreground occupies
    [(1-scale)/2 .. (1-scale)/2 + scale] on each axis."""
    rx, ry, rw, rh = rel
    if fit_mode != "fit_blur" or fit_scale >= 1.0:
        return rx, ry, rw, rh
    margin = (1.0 - fit_scale) / 2.0
    return margin + rx * fit_scale, margin + ry * fit_scale, rw * fit_scale, rh * fit_scale


def _sample_times(reel: dict, project: dict) -> list[tuple[dict, float, float]]:
    """~SAMPLE_COUNT (clip, source_time, timeline_time) samples spread across
    the reel's effective window (all segments concatenated in order),
    honoring in/out overrides exactly like render_reel does."""
    _reels.ensure_segments(reel)
    segments = reel["segments"]
    windows = []
    total = 0.0
    for seg in segments:
        clip = store.get_clip(project, seg["clip_id"])
        start, end = _reels._effective_segment_window(clip, seg)
        dur = max(0.0, end - start)
        windows.append((clip, start, end, dur))
        total += dur
    if total <= 0:
        return []
    samples = []
    for j in range(SAMPLE_COUNT):
        t_frac = (j + 0.5) / SAMPLE_COUNT * total
        offset = 0.0
        for idx, (clip, start, _end, dur) in enumerate(windows):
            is_last = idx == len(windows) - 1
            if t_frac <= offset + dur or is_last:
                local = min(max(t_frac - offset, 0.0), dur)
                samples.append((clip, start + local, t_frac))
                break
            offset += dur
    return samples


def _effective_center(project: dict, reel: dict, windows_first: tuple[dict, float, float]):
    clip, start, end = windows_first
    return _reels._effective_crop_center(lambda *_a, **_k: None, clip, reel, start, end)


def _merge_intervals(hits: list[tuple[float, float, str]]) -> list[dict]:
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h[0])
    out = [{"t0": hits[0][0], "t1": hits[0][1], "zone": hits[0][2]}]
    for t0, t1, zone in hits[1:]:
        last = out[-1]
        if t0 <= last["t1"] and zone == last["zone"]:
            last["t1"] = max(last["t1"], t1)
        else:
            out.append({"t0": t0, "t1": t1, "zone": zone})
    return out


def analyze(project: dict, reel: dict, platform: str) -> dict:
    """Deterministic reel safety check for `platform` (one of PLATFORMS'
    keys). Returns {safe, coverage_pct, intervals, suggested_fit_scale}.

    coverage_pct = % of SAMPLED instants (with a detected, visible face)
    where the face intersects an occupied zone — a simple, well-defined
    proxy given ~9 discrete samples rather than continuous frame coverage.
    intervals merge consecutive unsafe samples into {t0, t1, zone} spans on
    the reel's own concatenated timeline (0 = reel start), each padded by
    half the sampling step so short unsafe runs show a visible window
    ("0:04-0:12" style UI badges) instead of a single instant.
    """
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    zones = PLATFORMS[platform]["zones"]

    _reels.ensure_segments(reel)
    samples = _sample_times(reel, project)
    if not samples:
        return {"safe": True, "coverage_pct": 0.0, "intervals": [], "suggested_fit_scale": None}

    first_seg = reel["segments"][0]
    first_clip_obj = store.get_clip(project, first_seg["clip_id"])
    fs_start, fs_end = _reels._effective_segment_window(first_clip_obj, first_seg)
    center = _effective_center(project, reel, (first_clip_obj, fs_start, fs_end))

    fit_mode = reel.get("fit_mode", "fill")
    fit_scale = float(reel.get("fit_scale") or 1.0) if fit_mode == "fit_blur" else 1.0

    step = None
    if len(samples) > 1:
        # timeline spacing between consecutive samples, for interval padding
        step = samples[1][2] - samples[0][2]
    half_step = (step or 1.0) / 2.0

    per_sample_rel: list[tuple[float, float, float, float] | None] = []
    unsafe_hits: list[tuple[float, float, str]] = []
    detected = 0
    unsafe_count = 0
    for clip, src_t, tl_t in samples:
        bbox = faces.face_bbox_at(clip["path"], src_t)
        if bbox is None:
            per_sample_rel.append(None)
            continue
        crop_rect = _crop_rect(clip["info"]["width"], clip["info"]["height"], center)
        rel = _bbox_to_crop_relative(
            bbox, clip["info"]["width"], clip["info"]["height"], crop_rect
        )
        per_sample_rel.append(rel)
        if rel is None:
            continue
        detected += 1
        out_bbox = _crop_relative_to_output(rel, fit_mode, fit_scale)
        zone = _hit_zone(out_bbox, zones)
        if zone:
            unsafe_count += 1
            unsafe_hits.append((max(0.0, tl_t - half_step), tl_t + half_step, zone))

    coverage_pct = round(100.0 * unsafe_count / detected, 1) if detected else 0.0
    safe = unsafe_count == 0
    intervals = _merge_intervals(unsafe_hits)

    suggested_fit_scale = None
    if not safe:
        scale = FIT_SCALE_MAX
        # Walk from the least-aggressive zoom-out (1.0) down to the most
        # (FIT_SCALE_MIN): the first scale (largest / least intrusive) under
        # which every sampled face clears every zone is the suggestion — the
        # smallest ZOOM-OUT needed to clear the zones, i.e. the least
        # distortion that still fixes it (going smaller than necessary only
        # adds unneeded blur border). If nothing in range clears it, there's
        # no fix via this lever and we report None.
        while scale >= FIT_SCALE_MIN - 1e-9:
            all_clear = True
            for rel in per_sample_rel:
                if rel is None:
                    continue
                out_bbox = _crop_relative_to_output(rel, "fit_blur", scale)
                if _hit_zone(out_bbox, zones):
                    all_clear = False
                    break
            if all_clear:
                suggested_fit_scale = round(scale, 2)
                break
            scale -= FIT_SCALE_STEP

    return {
        "safe": safe,
        "coverage_pct": coverage_pct,
        "intervals": intervals,
        "suggested_fit_scale": suggested_fit_scale,
    }
