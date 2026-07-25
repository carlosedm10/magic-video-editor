"""Reel social safe zones + deterministic face safety (spec v7.7).

Owner decision (locked): reel safety is DETERMINISTIC geometry -- face bbox
(OpenCV Haar, via faces.face_bbox_at) intersected with published per-platform
UI safe-zone rectangles. NO vision-LLM / screenshot judging.

## Zone specs (fractions of a 1080x1920 canvas)

Researched 2026-07-25 (web search -- no official machine-readable spec is
published by any of the three platforms; these are the converged consensus
numbers across current creator/agency safe-zone guides, cited per platform
below). Each zone is a rectangle {x, y, w, h} as a fraction of 1080x1920, in
the same fraction space the frontend mockups and this module's geometry both
use -- the UI agent can render its CSS/SVG overlay directly from these
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
  specifically -- we use the more common creator-facing numbers above for
  organic Shorts UI, since that's what the reel actually plays behind).

## Existing open-source safe-zone template search (per spec: "search first")

Found: github.com/Creative-Crafter/davinci-shortform-overlays (MIT license) --
a DaVinci Resolve .drfx template bundling TikTokOverlay.png /
YouTubeShortsOverlay.png / InstagramReelsOverlay.png toggle overlays. NOT
vendored: (1) it ships opaque PNG overlays baked for DaVinci Resolve's
inspector, not the zone-fraction data our web frontend needs to draw its own
CSS/SVG mockup or that this module needs to do geometry against; (2) no
numeric spec is published alongside it (the margins are only implicit in the
raster art) -- extracting fractions back out of the PNGs would be less
reliable than the cited numeric guides above. Nothing else turned up in
open-source/permissive form (the rest of the search results are all
proprietary SaaS "safe zone checker" tools, not licensed assets). Conclusion:
hand-built zone rectangles below, sourced from the cited guides, are the
better fit -- the UI agent gets clean fractions instead of a foreign raster
asset to reverse-engineer.

## Face-safety geometry

`analyze()` samples ~9 frames across the reel's effective window (segments,
in/out overrides -- same resolution reels.py's renderer uses), gets each
sampled frame's face bbox in SOURCE fraction coords, maps it into 9:16
OUTPUT fraction coords honoring the reel's crop center (face-detected, same
as the renderer) and the reel's framing **transform** (spec v7.11 `{zoom,
offset_x, offset_y}` -- see faces.transform_crop_rect /
faces.transform_vertical_crop_filter, the SAME math render_reel and the Reel
Editor's live preview use), then intersects against the platform's zones.

## Root-cause fix (geometry source, 2026-07-25 follow-up): stale reel fields

`_reel_fit()` used to read the RETIRED `reel["fit_mode"]`/`reel["fit_scale"]`
fields directly -- reels created (or edited) after spec v7.11 shipped carry
`reel["transform"]` instead, so any panned/zoomed reel was silently analyzed
as if it were a plain centered "fill" crop (fit_mode defaults to "fill" when
absent), computing safety verdicts against the WRONG geometry. Fixed: crop
geometry is now derived from `reel["transform"]` via
`faces.transform_crop_rect` (the exact function `_compose_reel` calls to
render), with old reels that predate the transform model bridged onto it
the same way everything else does -- `_segment_windows` (called before this
module ever reads `reel["transform"]`) already runs `_reels.ensure_segments`,
which runs `_reels._normalize_transform` (the one, single migration formula
from the legacy `{crop_x, fit_mode, fit_scale}` trio -- see that function's
own docstring); this module never re-derives or duplicates that mapping.
`suggested_fit_scale` (kept under its historical name for backward
compatibility -- see below) is likewise now a suggested **zoom** value,
found by re-deriving the crop rect (and thus the face's position in OUTPUT
coords) at each candidate zoom via the same `transform_crop_rect`, rather
than the old symmetric "shrink toward center on both axes" fit_blur formula
-- the new transform model only adds a vertical margin when zooming out
(the crop window itself grows horizontally to capture a wider field of
view -- see `faces.transform_crop_rect`'s module note), so the two axes are
no longer scaled the same way.

Backward compatibility: `ui/editor/safezones-ui.js`'s one-click fix button
PATCHes the RETIRED `{fit_mode: "fit_blur", fit_scale}` shape (it hasn't
been migrated to PATCH `transform` directly), which `api/reels.py`'s
`_legacy_fields_to_transform` bridges onto `zoom = fit_scale` -- so
`suggested_fit_scale` keeps meaning "the zoom value to PATCH via the legacy
fields", unchanged from the caller's point of view, and keeps the same
[FIT_SCALE_MIN, FIT_SCALE_MAX] = [0.6, 1.0] range so it never falls outside
`ReelPatch.fit_scale`'s own `ge=0.6, le=1.0` validation bound.

## Root-cause fix (field bug, 2026-07-25): false positive on a clearly-safe
frame

Reported: face plainly inside the safe area, yet the check flagged "La cara
queda tapada por la UI de TikTok en 0:13-0:15 (cobertura 11.1%)". Three
independent bugs in the old criterion, all now fixed:

1. **Any-touch, not overlap-magnitude.** The old `_hit_zone` flagged a zone
   the instant the FULL Haar face bbox geometrically touched it at all --
   `_rects_intersect` had no area/fraction threshold. A Haar frontal-face box
   runs forehead-to-chin (often grazing the upper neck), so a talking-head
   framed dead center routinely has its bbox's bottom edge brush the top of
   a bottom_caption zone while the actual face (eyes/nose/mouth) sits well
   clear of it -- exactly the reported shape. Fixed: the criterion now (a)
   shrinks to the INNER face region (central 60% of the bbox -- see
   `_inner_region`) before testing, and (b) requires the intersection area
   to exceed 25% of that inner region's own area (`_hit_zone` /
   `OVERLAP_THRESHOLD`), not merely touch it.
2. **Single-sample trigger.** One flagged sample (out of ~9) was enough to
   emit a warning interval -- a single stray Haar false positive (a
   necklace, a hand, a shadow briefly read as a "face") could not be told
   apart from a real, sustained framing problem. Fixed: `_sustained_hits`
   only turns a run of same-zone hits into a reported interval once it spans
   >=2 consecutive samples (~>1s at 9 samples/reel).
3. **No damping of erratic Haar detections.** A per-frame detector with no
   cross-frame consistency check will happily flag a different-sized object
   near the speaker (necklace, raised hand) as "the face" on an odd sample.
   Fixed: `_stabilize` compares each sample's box area against the MEDIAN
   detected area across the reel window and discards (treats as
   no-detection) any sample whose box is a wildly different size --
   `SIZE_OUTLIER_LOW`/`SIZE_OUTLIER_HIGH` -- damping exactly the
   necklace/hand case the spec calls out, while leaving the real, recurring
   speaker-face size alone.

Transparency (spec v7.7 follow-up): `analyze()` now also returns
`debug_samples` (per-sample face box in OUTPUT coords + which zone, if any,
it was judged to hit, plus whether a raw detection existed / was discarded
as a stabilization outlier) and each `intervals[]` entry carries its own
representative `face_box` + `zone_rect` so the UI can draw exactly what the
checker saw, instead of asking the owner to trust a bare percentage. When a
face was found in fewer than half the samples, `insufficient_face_data` is
set so the UI can say "we don't have enough signal" instead of asserting
"safe" with unfounded confidence. `face_box_at_time()` powers an on-demand
single-frame lookup (new GET .../safety/face-at endpoint) so the Reel
Editor's "Ver zonas" toggle can draw the CURRENT face box live as the
playhead moves, not just at the ~9 analyze() sample instants.
"""

from __future__ import annotations

from .. import store
from . import faces
from . import reels as _reels

REEL_W, REEL_H = 1080, 1920

SAMPLE_COUNT = 9
FIT_SCALE_MIN, FIT_SCALE_MAX, FIT_SCALE_STEP = 0.6, 1.0, 0.05

# Inner face region: central fraction of the raw Haar bbox treated as the
# actual "face" (eyes/nose/mouth) for zone-overlap purposes -- the outer
# bbox's forehead/chin/ear margins are excluded so a box merely grazing a
# zone with its edge no longer counts (see module docstring, bug #1).
INNER_REGION_FRACTION = 0.6

# A zone must cover more than this fraction of the INNER face region's area
# to count as "occupied" -- any-touch is no longer sufficient (bug #1).
OVERLAP_THRESHOLD = 0.25

# A hit must persist across at least this many consecutive samples (same
# zone) to be reported -- a lone flagged instant is treated as noise, not a
# real framing problem (bug #2).
MIN_CONSECUTIVE_HITS = 2

# Stabilization: a sample's detected box area is compared to the MEDIAN
# detected area across the reel window; ratios outside this range are
# treated as a probable non-face detection (necklace, hand, ...) and
# discarded (bug #3). Needs >=3 detections in the window to compute a
# meaningful median -- below that, nothing is discarded.
SIZE_OUTLIER_LOW, SIZE_OUTLIER_HIGH = 0.4, 2.5
MIN_DETECTIONS_FOR_STABILIZATION = 3

# A face found in fewer than this fraction of samples means "we don't have
# enough signal" rather than "confirmed safe".
MIN_DETECTION_RATIO_FOR_CONFIDENCE = 0.5

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


# ---------------------------------------------------------------------------
# Pure geometry helpers (unit-tested directly with synthetic boxes -- see
# scripts/test_safezones.py)
# ---------------------------------------------------------------------------


def _rect_area(r: tuple[float, float, float, float]) -> float:
    return max(0.0, r[2]) * max(0.0, r[3])


def _intersection_area(a: tuple[float, float, float, float], zone: dict) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = zone["x"], zone["y"], zone["w"], zone["h"]
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    return max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)


def _inner_region(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Central `INNER_REGION_FRACTION` sub-rectangle of a face bbox -- the
    eyes/nose/mouth core, excluding the forehead/chin/ear margins a raw Haar
    box includes. This shrink is a pure per-axis scale-around-center, which
    commutes with every mapping this module applies afterwards (crop-relative
    and fit_blur are both per-axis affine: position and width/height scale by
    the same factor) -- so it doesn't matter whether it's applied to the
    source-frame bbox, the crop-relative bbox, or the output bbox; all three
    give the same inner region. Applied at the crop-relative stage."""
    x, y, w, h = bbox
    margin_frac = (1.0 - INNER_REGION_FRACTION) / 2.0
    return (
        x + w * margin_frac,
        y + h * margin_frac,
        w * INNER_REGION_FRACTION,
        h * INNER_REGION_FRACTION,
    )


def _hit_zone(
    inner_bbox: tuple[float, float, float, float], zones: list[dict]
) -> str | None:
    """Name of the zone whose intersection with the INNER face region
    exceeds OVERLAP_THRESHOLD of that region's own area -- the best (highest
    overlap fraction) zone crossing the threshold, or None if none does. A
    bbox merely grazing a zone's edge no longer qualifies (see bug #1)."""
    inner_area = _rect_area(inner_bbox)
    if inner_area <= 0:
        return None
    best_name = None
    best_frac = OVERLAP_THRESHOLD
    for z in zones:
        frac = _intersection_area(inner_bbox, z) / inner_area
        if frac > best_frac:
            best_frac = frac
            best_name = z["name"]
    return best_name


def _stabilize(
    rel_list: list[tuple[float, float, float, float] | None],
) -> list[tuple[float, float, float, float] | None]:
    """Damp spurious per-frame Haar detections (necklaces, hands, stray
    shadows) that don't match the recurring speaker-face size: compare each
    sample's box area to the MEDIAN area across all detections in the window
    and discard (-> None) any sample whose ratio to that median falls
    outside [SIZE_OUTLIER_LOW, SIZE_OUTLIER_HIGH]. Needs at least
    MIN_DETECTIONS_FOR_STABILIZATION real detections to compute a meaningful
    median; below that there isn't enough signal to call anything an
    outlier, so nothing is discarded (see bug #3)."""
    areas = [(idx, _rect_area(r)) for idx, r in enumerate(rel_list) if r is not None]
    if len(areas) < MIN_DETECTIONS_FOR_STABILIZATION:
        return list(rel_list)
    sorted_areas = sorted(a for _, a in areas)
    n = len(sorted_areas)
    median = (
        sorted_areas[n // 2]
        if n % 2
        else (sorted_areas[n // 2 - 1] + sorted_areas[n // 2]) / 2.0
    )
    if median <= 0:
        return list(rel_list)
    out = list(rel_list)
    for idx, area in areas:
        ratio = area / median
        if ratio < SIZE_OUTLIER_LOW or ratio > SIZE_OUTLIER_HIGH:
            out[idx] = None
    return out


def _sustained_hits(
    zone_per_sample: list[str | None],
) -> list[tuple[int, int, str]]:
    """(start_idx, end_idx, zone) for each run of >=MIN_CONSECUTIVE_HITS
    consecutive samples hitting the SAME zone -- a lone flagged instant is
    noise, not a reported framing problem (see bug #2)."""
    runs = []
    i, n = 0, len(zone_per_sample)
    while i < n:
        zone = zone_per_sample[i]
        if zone is None:
            i += 1
            continue
        j = i
        while j < n and zone_per_sample[j] == zone:
            j += 1
        if j - i >= MIN_CONSECUTIVE_HITS:
            runs.append((i, j - 1, zone))
        i = j
    return runs


def _box_dict(b: tuple[float, float, float, float]) -> dict:
    x, y, w, h = b
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


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
    rel: tuple[float, float, float, float],
    zoom: float,
    crop_w: int,
    crop_h: int,
) -> tuple[float, float, float, float]:
    """Crop-relative fraction -> OUTPUT (1080x1920) fraction, honoring the
    reel's framing transform (spec v7.11) -- mirrors
    faces.transform_vertical_crop_filter's two branches exactly (that
    function returns an ffmpeg filtergraph fragment; this returns the
    fraction-space numbers analyze() needs).

    zoom >= the cover threshold (faces.transform_needs_blur(zoom) is False,
    crop keeps the 9:16 aspect ratio): the crop maps 1:1 onto the output
    canvas -- output fraction == crop-relative fraction, exactly like the
    classic "fill" crop.

    zoom below the threshold (crop is wider than 9:16, needs the blurred
    cover-background underlay): the foreground is scaled to fill the output
    WIDTH exactly (`scale=out_w:-2` in the ffmpeg fragment -- no horizontal
    margin) and centered vertically with top/bottom margins, per
    `overlay=(W-w)/2:(H-h)/2`. Unlike the old (retired) fit_blur math, which
    shrank both axes by the same `fit_scale` and centered symmetrically, only
    the VERTICAL axis gets a margin here -- the crop window itself already
    grew horizontally (`crop_w` > the classic 9:16 `base_w`) to capture a
    wider field of view, so there's nothing left to margin on that axis (see
    faces.transform_crop_rect's module note)."""
    rx, ry, rw, rh = rel
    if not faces.transform_needs_blur(zoom):
        return rx, ry, rw, rh
    fg_h_frac = (crop_h / crop_w) * (REEL_W / REEL_H)
    margin_frac = max(0.0, (1.0 - fg_h_frac) / 2.0)
    return rx, margin_frac + ry * fg_h_frac, rw, rh * fg_h_frac


def _segment_windows(
    reel: dict, project: dict
) -> tuple[list[tuple[dict, float, float, float]], float]:
    """[(clip, start, end, dur), ...] per segment in order, plus the total
    effective duration -- shared by `_sample_times` (the ~9 analyze()
    samples) and `face_box_at_time` (an arbitrary on-demand instant)."""
    _reels.ensure_segments(reel)
    windows = []
    total = 0.0
    for seg in reel["segments"]:
        clip = store.get_clip(project, seg["clip_id"])
        start, end = _reels._effective_segment_window(clip, seg)
        dur = max(0.0, end - start)
        windows.append((clip, start, end, dur))
        total += dur
    return windows, total


def _locate(
    windows: list[tuple[dict, float, float, float]], t_frac: float
) -> tuple[dict, float] | None:
    """(clip, source_time) at `t_frac` reel-timeline seconds (0 = reel
    start), clamped into the last segment past the end."""
    offset = 0.0
    for idx, (clip, start, _end, dur) in enumerate(windows):
        is_last = idx == len(windows) - 1
        if t_frac <= offset + dur or is_last:
            local = min(max(t_frac - offset, 0.0), dur)
            return clip, start + local
        offset += dur
    return None


def _sample_times(reel: dict, project: dict) -> list[tuple[dict, float, float]]:
    """~SAMPLE_COUNT (clip, source_time, timeline_time) samples spread across
    the reel's effective window (all segments concatenated in order),
    honoring in/out overrides exactly like render_reel does."""
    windows, total = _segment_windows(reel, project)
    if total <= 0:
        return []
    samples = []
    for j in range(SAMPLE_COUNT):
        t_frac = (j + 0.5) / SAMPLE_COUNT * total
        located = _locate(windows, t_frac)
        if located is None:
            continue
        clip, src_t = located
        samples.append((clip, src_t, t_frac))
    return samples


def _effective_center(project: dict, reel: dict, windows_first: tuple[dict, float, float]):
    clip, start, end = windows_first
    return _reels._effective_crop_center(lambda *_a, **_k: None, clip, reel, start, end)


def _reel_transform(reel: dict) -> tuple[float, float, float]:
    """(zoom, offset_x, offset_y) from reel["transform"] (spec v7.11) --
    always present and already clamped/defaulted by the time this is called,
    since every caller runs `_segment_windows` (-> `_reels.ensure_segments`
    -> `_reels._normalize_transform`) first, which bridges old reels that
    still only carry the retired {crop_x, fit_mode, fit_scale} trio onto an
    equivalent transform (see that function's docstring for the exact
    mapping -- not duplicated here). A bare fallback default is kept only in
    case this is ever called before that normalization (defensive, not the
    expected path)."""
    t = reel.get("transform") or {"zoom": 1.0, "offset_x": 0.0, "offset_y": 0.0}
    return (
        float(t.get("zoom", 1.0)),
        float(t.get("offset_x", 0.0)),
        float(t.get("offset_y", 0.0)),
    )


def face_box_at_time(
    project: dict, reel: dict, timeline_t: float, platform: str | None = None
) -> dict:
    """On-demand single-frame face box lookup at an arbitrary point on the
    reel's own concatenated timeline (0 = reel start), in OUTPUT (1080x1920)
    fraction coords -- independent of analyze()'s ~9 discrete samples. Powers
    the Reel Editor's "Ver zonas" live face-box tracking (spec v7.7
    transparency follow-up): the UI can call this as the playhead moves and
    always see the CURRENT frame's detected box, not just the last analyzed
    sample near it. Returns {t, face_box (OUTPUT-coords dict, or None if no
    face was found), zone (name of the zone it hits per the same
    inner-region/threshold criterion as analyze(), or None -- only computed
    when `platform` is a known key)}."""
    windows, total = _segment_windows(reel, project)
    if total <= 0:
        return {"t": timeline_t, "face_box": None, "zone": None}
    t = max(0.0, min(float(timeline_t), total))
    located = _locate(windows, t)
    if located is None:
        return {"t": t, "face_box": None, "zone": None}
    clip, src_t = located

    first_seg = reel["segments"][0]
    first_clip_obj = store.get_clip(project, first_seg["clip_id"])
    fs_start, fs_end = _reels._effective_segment_window(first_clip_obj, first_seg)
    center = _effective_center(project, reel, (first_clip_obj, fs_start, fs_end))
    zoom, offset_x, offset_y = _reel_transform(reel)

    bbox = faces.face_bbox_at(clip["path"], src_t)
    if bbox is None:
        return {"t": t, "face_box": None, "zone": None}
    crop_rect = faces.transform_crop_rect(
        clip["info"]["width"],
        clip["info"]["height"],
        center,
        zoom,
        offset_x,
        offset_y,
        REEL_W,
        REEL_H,
    )
    crop_w, crop_h = crop_rect[2], crop_rect[3]
    rel = _bbox_to_crop_relative(bbox, clip["info"]["width"], clip["info"]["height"], crop_rect)
    if rel is None:
        return {"t": t, "face_box": None, "zone": None}
    face_box_out = _crop_relative_to_output(rel, zoom, crop_w, crop_h)

    zone = None
    if platform in PLATFORMS:
        inner_out = _crop_relative_to_output(_inner_region(rel), zoom, crop_w, crop_h)
        zone = _hit_zone(inner_out, PLATFORMS[platform]["zones"])

    return {"t": t, "face_box": _box_dict(face_box_out), "zone": zone}


def analyze(project: dict, reel: dict, platform: str) -> dict:
    """Deterministic reel safety check for `platform` (one of PLATFORMS'
    keys). Returns {safe, coverage_pct, intervals, suggested_fit_scale,
    debug_samples, face_detection_ratio, insufficient_face_data}.

    Criterion (fixed 2026-07-25, see module docstring "Root-cause fix"): a
    sample is "unsafe" only when the INNER face region (central 60% of the
    detected bbox) overlaps a zone by more than OVERLAP_THRESHOLD of the
    inner region's own area, AND that same zone is hit on
    >=MIN_CONSECUTIVE_HITS consecutive samples. Erratic single-frame Haar
    detections (necklaces, hands) are damped via `_stabilize` before the
    criterion is ever applied.

    coverage_pct = % of SAMPLED instants (with a detected, visible,
    non-discarded face) that belong to a reported (sustained) unsafe run.
    intervals[] merge each sustained run into {t0, t1, zone, face_box,
    zone_rect} spans on the reel's own concatenated timeline (0 = reel
    start), each padded by half the sampling step so short unsafe runs show
    a visible window ("0:04-0:12" style UI badges) instead of a single
    instant; face_box/zone_rect are the representative (middle-of-run)
    sample's detected box and the offending zone's rectangle, both in OUTPUT
    coords, so the UI can draw exactly what triggered the warning.
    debug_samples carries the SAME per-sample data for every analyzed
    sample (not just flagged ones), including samples with no detection or
    with a detection discarded as a stabilization outlier, for full
    transparency.
    """
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    zones = PLATFORMS[platform]["zones"]

    samples = _sample_times(reel, project)
    if not samples:
        return {
            "safe": True,
            "coverage_pct": 0.0,
            "intervals": [],
            "suggested_fit_scale": None,
            "debug_samples": [],
            "face_detection_ratio": 0.0,
            "insufficient_face_data": True,
        }

    first_seg = reel["segments"][0]
    first_clip_obj = store.get_clip(project, first_seg["clip_id"])
    fs_start, fs_end = _reels._effective_segment_window(first_clip_obj, first_seg)
    center = _effective_center(project, reel, (first_clip_obj, fs_start, fs_end))
    zoom, offset_x, offset_y = _reel_transform(reel)

    step = samples[1][2] - samples[0][2] if len(samples) > 1 else 1.0
    half_step = step / 2.0

    # raw_bbox_src keeps each sample's SOURCE-frame bbox around (rather than
    # only the crop-relative fraction derived from it at the reel's CURRENT
    # zoom) -- the suggested_fit_scale search below needs to re-derive the
    # crop-relative position at OTHER candidate zooms, and the crop window
    # itself (crop_w/crop_h) changes with zoom (see
    # faces.transform_crop_rect), so the mapping can't just be re-scaled from
    # the already-computed rel value.
    raw_bbox_src: list[tuple[float, float, float, float] | None] = []
    raw_rel: list[tuple[float, float, float, float] | None] = []
    crop_dims: list[tuple[int, int] | None] = []
    for clip, src_t, _tl_t in samples:
        bbox = faces.face_bbox_at(clip["path"], src_t)
        raw_bbox_src.append(bbox)
        if bbox is None:
            raw_rel.append(None)
            crop_dims.append(None)
            continue
        crop_rect = faces.transform_crop_rect(
            clip["info"]["width"],
            clip["info"]["height"],
            center,
            zoom,
            offset_x,
            offset_y,
            REEL_W,
            REEL_H,
        )
        rel = _bbox_to_crop_relative(
            bbox, clip["info"]["width"], clip["info"]["height"], crop_rect
        )
        raw_rel.append(rel)
        crop_dims.append((crop_rect[2], crop_rect[3]))

    detected = sum(1 for r in raw_rel if r is not None)
    stabilized_rel = _stabilize(raw_rel)

    zone_per_sample: list[str | None] = []
    debug_samples: list[dict] = []
    for (_clip, _src_t, tl_t), raw, stab, dims in zip(
        samples, raw_rel, stabilized_rel, crop_dims, strict=True
    ):
        face_box_out = (
            _crop_relative_to_output(raw, zoom, *dims) if raw is not None else None
        )
        zone = None
        if stab is not None:
            inner_out = _crop_relative_to_output(_inner_region(stab), zoom, *dims)
            zone = _hit_zone(inner_out, zones)
        zone_per_sample.append(zone)
        debug_samples.append(
            {
                "t": round(tl_t, 3),
                "face_box": _box_dict(face_box_out) if face_box_out is not None else None,
                "zone": zone,
                "detected": raw is not None,
                "discarded": raw is not None and stab is None,
            }
        )

    runs = _sustained_hits(zone_per_sample)
    unsafe_sample_count = sum(j - i + 1 for i, j, _zone in runs)
    coverage_pct = round(100.0 * unsafe_sample_count / detected, 1) if detected else 0.0
    safe = len(runs) == 0

    intervals = []
    for i, j, zone in runs:
        t0 = max(0.0, samples[i][2] - half_step)
        t1 = samples[j][2] + half_step
        zone_rect = next((z for z in zones if z["name"] == zone), None)
        anchor = debug_samples[(i + j) // 2]
        intervals.append(
            {
                "t0": t0,
                "t1": t1,
                "zone": zone,
                "face_box": anchor["face_box"],
                "zone_rect": zone_rect,
            }
        )

    detected_ratio = detected / len(samples) if samples else 0.0
    insufficient_face_data = detected_ratio < MIN_DETECTION_RATIO_FOR_CONFIDENCE

    suggested_fit_scale = None
    if not safe:
        scale = min(FIT_SCALE_MAX, zoom)
        # Walk from the least-aggressive zoom-out (the reel's current zoom,
        # capped at 1.0 -- punching IN further never helps) down to the most
        # (FIT_SCALE_MIN): the first zoom (largest / least intrusive) under
        # which every stabilized sample's inner face region clears every zone
        # is the suggestion -- the smallest zoom-out needed to clear the
        # zones, i.e. the least distortion that still fixes it (going
        # smaller than necessary only adds unneeded blur border). Each
        # candidate re-derives the crop rect (faces.transform_crop_rect) at
        # that zoom -- the crop window itself changes shape/size with zoom,
        # so the face's OUTPUT-coords position must be recomputed from its
        # original SOURCE bbox at every candidate, not rescaled from the
        # current zoom's already-computed position. If nothing in range
        # clears it, there's no fix via this lever and we report None. Kept
        # under the historical name `suggested_fit_scale` / the historical
        # [FIT_SCALE_MIN, FIT_SCALE_MAX] range -- see module docstring
        # "Backward compatibility": the Reel Editor's one-click fix button
        # still PATCHes it through the legacy {fit_mode, fit_scale} bridge.
        while scale >= FIT_SCALE_MIN - 1e-9:
            all_clear = True
            for idx, stab in enumerate(stabilized_rel):
                if stab is None:
                    continue
                clip = samples[idx][0]
                bbox_src = raw_bbox_src[idx]
                candidate_rect = faces.transform_crop_rect(
                    clip["info"]["width"],
                    clip["info"]["height"],
                    center,
                    scale,
                    offset_x,
                    offset_y,
                    REEL_W,
                    REEL_H,
                )
                candidate_rel = _bbox_to_crop_relative(
                    bbox_src, clip["info"]["width"], clip["info"]["height"], candidate_rect
                )
                if candidate_rel is None:
                    continue  # face falls outside the crop at this zoom -- nothing to flag
                inner_out = _crop_relative_to_output(
                    _inner_region(candidate_rel), scale, candidate_rect[2], candidate_rect[3]
                )
                if _hit_zone(inner_out, zones):
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
        "debug_samples": debug_samples,
        "face_detection_ratio": round(detected_ratio, 3),
        "insufficient_face_data": insufficient_face_data,
    }
