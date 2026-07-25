#!/usr/bin/env python3
"""Unit tests for the reel safe-zone face-safety criterion
(magic_video_editor/pipeline/safezones.py, spec v7.7 + the 2026-07-25
false-positive fix + the spec v7.11 transform-geometry fix).

No pytest in this project's dependency set (see pyproject.toml) -- stdlib
unittest, same "no build step" spirit as scripts/test_updater.py. Everything
here is a pure/synthetic unit test: no real video, no real ffmpeg frame
extraction, no real Haar detector -- `faces.face_bbox_at` is monkeypatched
with hand-picked synthetic boxes chosen to reproduce the exact reported bug
shape, plus a genuine true-positive shape, plus the lower-level geometry
helpers in isolation.

Transform-aware cases (spec v7.11 fix -- `ZoomedFramingTests` /
`LegacyReelMigrationTests`): `_SafezonesAnalyzeHarness`'s clip is exactly
1080x1920 (9:16), which can't exercise the "crop grows wider than the
frame" zoom<1 branch (both crop dimensions clamp to the source regardless of
zoom when the source has no extra width to give -- see
faces.transform_crop_rect's module note); those tests use a separate 16:9
`_landscape_clip()` fixture instead, and verify geometry against
safezones's own helpers (faces.transform_crop_rect /
`_crop_relative_to_output`) rather than hand-derived numbers, so expectations
can't silently drift from the implementation. `LegacyReelMigrationTests`
additionally does NOT mock `_reels.ensure_segments`/`_normalize_transform`,
exercising the real legacy {crop_x, fit_mode, fit_scale} -> transform
migration end to end for a reel that predates spec v7.11.

Usage:
    uv run python scripts/test_safezones.py
    uv run python scripts/test_safezones.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from magic_video_editor.pipeline import safezones  # noqa: E402

TIKTOK_ZONES = safezones.PLATFORMS["tiktok"]["zones"]
BOTTOM_CAPTION = next(z for z in TIKTOK_ZONES if z["name"] == "bottom_caption")
# TikTok bottom_caption zone: y in [1 - 484/1920, 1.0] = [~0.7479, 1.0]


def _fake_clip(path: str = "/fake/clip.mp4") -> dict:
    return {"path": path, "info": {"width": 1080, "height": 1920, "duration": 9.0}}


class _SafezonesAnalyzeHarness(unittest.TestCase):
    """Common monkeypatching for a single-segment, 9-second fake reel where
    the crop window maps 1:1 onto the frame (center=(0.5, 0.45), 9:16 source
    == 9:16 output) -- so a raw bbox given as a fraction of the 1080x1920
    frame IS the OUTPUT-coords bbox `analyze()` tests against zones, with no
    extra crop/fit arithmetic to account for in the test's expected numbers.
    """

    def setUp(self):
        self.project = {"clips": {"c1": _fake_clip()}}
        self.reel = {
            "id": "r1",
            "segments": [{"clip_id": "c1", "start": 0.0, "end": 9.0}],
            "fit_mode": "fill",
        }
        patchers = [
            mock.patch.object(safezones._reels, "ensure_segments", lambda _reel: None),
            mock.patch.object(
                safezones._reels, "_effective_segment_window", lambda _clip, _seg: (0.0, 9.0)
            ),
            mock.patch.object(
                safezones._reels,
                "_effective_crop_center",
                lambda _log, _clip, _reel, _start, _end: (0.5, 0.45),
            ),
            mock.patch.object(
                safezones.store, "get_clip", lambda _project, _clip_id: _fake_clip()
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _patch_bboxes(self, by_sample_index: dict):
        """`faces.face_bbox_at` returns `by_sample_index[j]` for the j-th of
        the 9 analyze() samples (t_frac = j + 0.5 for a 9s reel), None for
        any index not present."""

        def fake_face_bbox_at(_path, t):
            j = int(round(t - 0.5))
            return by_sample_index.get(j)

        p = mock.patch.object(safezones.faces, "face_bbox_at", side_effect=fake_face_bbox_at)
        p.start()
        self.addCleanup(p.stop)


class ReportedFalsePositiveShapeTests(_SafezonesAnalyzeHarness):
    """The exact field-bug shape: a Haar full-face bbox centered in the
    frame, tall enough that its bottom edge (chin/neck margin) grazes the
    top of TikTok's bottom_caption zone -- but the actual face (eyes/nose/
    mouth, i.e. the inner 60%) never comes close. Must be SAFE."""

    GRAZING_BBOX = (0.35, 0.30, 0.30, 0.50)  # y: 0.30..0.80; zone starts ~0.748

    def test_old_any_touch_criterion_would_have_flagged_this(self):
        # Sanity check on the fixture itself: the FULL bbox does geometrically
        # touch the zone (this is what the old `_rects_intersect`/`_hit_zone`
        # any-touch logic keyed off -- confirming the fixture reproduces the
        # actual old false-positive trigger, not something unrelated).
        area = safezones._intersection_area(self.GRAZING_BBOX, BOTTOM_CAPTION)
        self.assertGreater(area, 0.0)

    def test_inner_region_clears_the_zone(self):
        inner = safezones._inner_region(self.GRAZING_BBOX)
        # inner y range should be 0.40..0.70, strictly above the zone's 0.748 start
        self.assertAlmostEqual(inner[1] + inner[3], 0.70, places=6)
        self.assertLess(inner[1] + inner[3], BOTTOM_CAPTION["y"])
        self.assertIsNone(safezones._hit_zone(inner, TIKTOK_ZONES))

    def test_analyze_reports_safe_for_every_sample_grazing(self):
        self._patch_bboxes({j: self.GRAZING_BBOX for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(result["safe"], f"expected safe, got {result}")
        self.assertEqual(result["intervals"], [])
        self.assertEqual(result["coverage_pct"], 0.0)
        # transparency: every sample still has its raw detected box recorded,
        # just judged not to hit any zone
        self.assertEqual(len(result["debug_samples"]), safezones.SAMPLE_COUNT)
        self.assertTrue(all(s["face_box"] is not None for s in result["debug_samples"]))
        self.assertTrue(all(s["zone"] is None for s in result["debug_samples"]))


class TruePositiveShapeTests(_SafezonesAnalyzeHarness):
    """A face genuinely lowered into the caption band (inner-region overlap
    well over 25%), sustained across >=2 consecutive samples, must be
    flagged -- and a single ISOLATED occurrence of the same shape must NOT
    be (damping single-sample noise, bug #2)."""

    SAFE_BBOX = (0.35, 0.10, 0.30, 0.30)  # frame-center-ish, nowhere near any zone
    # lowered: inner region overlaps bottom_caption > 25%. Kept within y+h <= 1.0
    # (a real detected bbox can never exceed the frame) so it passes through
    # `_bbox_to_crop_relative` unclamped -- the fixture's hand-computed
    # geometry then matches analyze()'s internal numbers exactly.
    HIT_BBOX = (0.35, 0.50, 0.30, 0.45)

    def test_hit_bbox_inner_overlap_exceeds_threshold(self):
        inner = safezones._inner_region(self.HIT_BBOX)
        inner_area = safezones._rect_area(inner)
        overlap = safezones._intersection_area(inner, BOTTOM_CAPTION)
        self.assertGreater(overlap / inner_area, safezones.OVERLAP_THRESHOLD)
        self.assertEqual(safezones._hit_zone(inner, TIKTOK_ZONES), "bottom_caption")

    def test_isolated_single_hit_is_not_flagged(self):
        boxes = {j: self.SAFE_BBOX for j in range(safezones.SAMPLE_COUNT)}
        boxes[3] = self.HIT_BBOX  # a single, non-consecutive hit
        self._patch_bboxes(boxes)
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(result["safe"], f"a lone hit sample should be damped, got {result}")
        self.assertEqual(result["intervals"], [])

    def test_two_consecutive_hits_are_flagged(self):
        boxes = {j: self.SAFE_BBOX for j in range(safezones.SAMPLE_COUNT)}
        boxes[3] = self.HIT_BBOX  # isolated -- should NOT contribute to the flagged run
        boxes[5] = self.HIT_BBOX  # sustained run starts here
        boxes[6] = self.HIT_BBOX  # ... and continues here (2 consecutive)
        self._patch_bboxes(boxes)
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertFalse(result["safe"])
        self.assertEqual(len(result["intervals"]), 1)
        iv = result["intervals"][0]
        self.assertEqual(iv["zone"], "bottom_caption")
        self.assertIsNotNone(iv["face_box"])
        self.assertEqual(iv["zone_rect"]["name"], "bottom_caption")
        # coverage_pct counts only the sustained run's 2 samples out of 9 detected
        self.assertAlmostEqual(result["coverage_pct"], 100.0 * 2 / 9, places=1)
        # the suggested fix, if any, must actually clear the zone at that
        # scale -- this fixture's clip is already exactly 9:16 (1080x1920),
        # so its crop always clamps to the full source frame regardless of
        # zoom (see faces.transform_crop_rect's module note: only a source
        # WIDER than 9:16 has room to grow), matching this harness's crop
        # dims (1080, 1920).
        if result["suggested_fit_scale"] is not None:
            inner_out = safezones._crop_relative_to_output(
                safezones._inner_region(self.HIT_BBOX), result["suggested_fit_scale"], 1080, 1920
            )
            self.assertIsNone(safezones._hit_zone(inner_out, TIKTOK_ZONES))


class NoFaceDetectedTests(_SafezonesAnalyzeHarness):
    def test_no_face_in_most_samples_is_flagged_as_insufficient(self):
        # Only 2 of 9 samples detect anything -- below the 50% confidence floor.
        boxes = {0: (0.35, 0.10, 0.30, 0.30), 1: (0.35, 0.10, 0.30, 0.30)}
        self._patch_bboxes(boxes)
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(result["insufficient_face_data"])
        self.assertAlmostEqual(result["face_detection_ratio"], 2 / 9, places=3)

    def test_no_face_at_all_is_safe_but_flagged_insufficient(self):
        self._patch_bboxes({})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(result["safe"])
        self.assertTrue(result["insufficient_face_data"])
        self.assertEqual(result["face_detection_ratio"], 0.0)


class FaceBoxAtTimeTests(_SafezonesAnalyzeHarness):
    """The on-demand live lookup used by the Reel Editor's "Ver zonas" live
    tracking -- same mapping/criterion as analyze(), single instant."""

    def test_returns_output_coords_box_and_zone(self):
        self._patch_bboxes({7: (0.35, 0.55, 0.30, 0.50)})  # arbitrary index, t=7.5
        result = safezones.face_box_at_time(self.project, self.reel, 7.5, platform="tiktok")
        self.assertAlmostEqual(result["t"], 7.5)
        self.assertIsNotNone(result["face_box"])
        self.assertEqual(result["face_box"]["y"], 0.55)  # fill mode, 1:1 crop -> unchanged
        self.assertEqual(result["zone"], "bottom_caption")

    def test_no_face_returns_none_box(self):
        self._patch_bboxes({})
        result = safezones.face_box_at_time(self.project, self.reel, 4.0, platform="tiktok")
        self.assertIsNone(result["face_box"])
        self.assertIsNone(result["zone"])

    def test_platform_omitted_skips_zone_computation(self):
        self._patch_bboxes({0: (0.35, 0.55, 0.30, 0.50)})
        result = safezones.face_box_at_time(self.project, self.reel, 0.5, platform=None)
        self.assertIsNotNone(result["face_box"])
        self.assertIsNone(result["zone"])


def _landscape_clip(path: str = "/fake/landscape.mp4") -> dict:
    """A 16:9 clip -- the common camera case where zoom<1 actually GROWS the
    crop width past the classic 9:16 base_w (see
    faces.transform_crop_rect's module note); the other fixtures in this
    file use an already-9:16 clip, which can't exercise that branch (both
    dimensions clamp to the source at zoom<=1 when the source has no extra
    width to give)."""
    return {"path": path, "info": {"width": 1920, "height": 1080, "duration": 9.0}}


class _LandscapeTransformHarness(unittest.TestCase):
    """Harness for the spec v7.11 fix (safezones.py must derive crop geometry
    from reel["transform"], not the retired fit_mode/fit_scale fields):
    same monkeypatching shape as `_SafezonesAnalyzeHarness` above, but with
    a 16:9 landscape source and a reel whose "transform" each test sets
    explicitly (this harness does NOT touch `_reels._normalize_transform` --
    see `LegacyReelMigrationTests` below for the end-to-end migration path)."""

    def setUp(self):
        self.project = {"clips": {"c1": _landscape_clip()}}
        self.reel = {
            "id": "r1",
            "segments": [{"clip_id": "c1", "start": 0.0, "end": 9.0}],
        }
        patchers = [
            mock.patch.object(safezones._reels, "ensure_segments", lambda _reel: None),
            mock.patch.object(
                safezones._reels, "_effective_segment_window", lambda _clip, _seg: (0.0, 9.0)
            ),
            mock.patch.object(
                safezones._reels,
                "_effective_crop_center",
                lambda _log, _clip, _reel, _start, _end: (0.5, 0.45),
            ),
            mock.patch.object(
                safezones.store, "get_clip", lambda _project, _clip_id: _landscape_clip()
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _patch_bboxes(self, by_sample_index: dict):
        def fake_face_bbox_at(_path, t):
            j = int(round(t - 0.5))
            return by_sample_index.get(j)

        p = mock.patch.object(safezones.faces, "face_bbox_at", side_effect=fake_face_bbox_at)
        p.start()
        self.addCleanup(p.stop)


class ZoomedFramingTests(_LandscapeTransformHarness):
    """The bug this task fixes: `_reel_fit()` used to read the RETIRED
    fit_mode/fit_scale fields, so ANY reel using the new {zoom, offset_x,
    offset_y} transform was analyzed as if it were a plain centered "fill"
    crop, regardless of its actual framing. These cases pin down that
    zoom now actually changes the computed geometry (verified independently
    against faces.transform_crop_rect / safezones's own helpers, not just
    hand-derived numbers -- see the module-level BBOX_PX comment)."""

    # SOURCE-frame pixel bbox (of the 1920x1080 clip) placed low in frame --
    # squarely inside TikTok's bottom_caption zone at the classic zoom=1.0
    # full-height 9:16 crop.
    BBOX_PX = (750, 850, 120, 180)

    def _bbox_frac(self) -> tuple[float, float, float, float]:
        x, y, w, h = self.BBOX_PX
        return x / 1920, y / 1080, w / 1920, h / 1080

    def test_zoom_one_default_is_unsafe(self):
        # transform omitted -> _reel_transform's fallback default (1.0, 0, 0)
        # -- same geometry as the old "fill" crop, so this is the baseline
        # the zoom-out/zoom-in cases below are compared against.
        self._patch_bboxes({j: self._bbox_frac() for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertFalse(
            result["safe"], f"expected the zoom=1.0 baseline to be unsafe, got {result}"
        )

    def test_zoom_out_changes_blur_bar_geometry_and_clears(self):
        # Zooming OUT (spec v7.11: "the source window exceeds the frame")
        # adds a vertical margin in OUTPUT space (see
        # `_crop_relative_to_output`) that pulls the face's inner region
        # clear of the caption band -- this is the exact mechanism the old
        # (retired) `_reel_fit`/fit_blur code path could never reach because
        # it never looked at reel["transform"] at all.
        self.reel["transform"] = {"zoom": 0.6, "offset_x": 0.0, "offset_y": 0.0}
        self._patch_bboxes({j: self._bbox_frac() for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(result["safe"], f"expected zoom=0.6 to clear the zone, got {result}")

    def test_zoom_in_moves_the_face_out_of_the_crop_entirely(self):
        # Zooming IN shrinks the crop window around the (unchanged) face
        # center -- a face positioned away from center can end up entirely
        # OUTSIDE the tighter crop (cropped away, nothing to check). Before
        # the fix this reel's zoom was never consulted, so the geometry
        # would incorrectly still be the full zoom=1.0 crop and this face
        # would be reported as always visible at the same relative spot.
        self.reel["transform"] = {"zoom": 2.0, "offset_x": 0.0, "offset_y": 0.0}
        self._patch_bboxes({j: self._bbox_frac() for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(result["insufficient_face_data"])
        self.assertEqual(result["face_detection_ratio"], 0.0)
        self.assertTrue(all(s["face_box"] is None for s in result["debug_samples"]))
        self.assertTrue(all(not s["detected"] for s in result["debug_samples"]))

    def test_suggested_zoom_actually_clears_the_zone(self):
        # `suggested_fit_scale` (spec v7.11: a suggested ZOOM, kept under its
        # historical name/range for the legacy PATCH bridge -- see module
        # docstring) must be re-derived via the same
        # faces.transform_crop_rect + _crop_relative_to_output pipeline
        # analyze() itself uses, not the old symmetric fit_blur formula.
        self._patch_bboxes({j: self._bbox_frac() for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertFalse(result["safe"])
        scale = result["suggested_fit_scale"]
        self.assertIsNotNone(scale)
        self.assertGreaterEqual(scale, safezones.FIT_SCALE_MIN)
        self.assertLessEqual(scale, safezones.FIT_SCALE_MAX)
        clip = safezones.store.get_clip(self.project, "c1")
        crop_rect = safezones.faces.transform_crop_rect(
            clip["info"]["width"],
            clip["info"]["height"],
            (0.5, 0.45),
            scale,
            0.0,
            0.0,
            safezones.REEL_W,
            safezones.REEL_H,
        )
        rel = safezones._bbox_to_crop_relative(
            self._bbox_frac(), clip["info"]["width"], clip["info"]["height"], crop_rect
        )
        self.assertIsNotNone(rel)
        inner_out = safezones._crop_relative_to_output(
            safezones._inner_region(rel), scale, crop_rect[2], crop_rect[3]
        )
        self.assertIsNone(safezones._hit_zone(inner_out, TIKTOK_ZONES))


class LegacyReelMigrationTests(unittest.TestCase):
    """A reel that predates spec v7.11 -- and even spec v5.8b (no "segments"
    key, just the legacy top-level clip_id/start/end + fit_mode/fit_scale/
    crop_x fields) -- must still analyze correctly. Unlike every harness
    above, this one does NOT mock `_reels.ensure_segments` or
    `_reels._normalize_transform`: safezones.py deliberately does not
    re-derive the legacy-fields-to-transform mapping itself (see module
    docstring "Root-cause fix") -- it relies on the REAL migration already
    having run (via `_segment_windows` -> `ensure_segments` ->
    `_normalize_transform`, mirroring exactly what api/reels.py's PATCH
    handler and render_reel also go through) by the time this module reads
    reel["transform"]. This test exercises that real path end to end."""

    def setUp(self):
        self.project = {"clips": {"c1": _landscape_clip()}}
        self.reel = {
            "id": "r1",
            "clip_id": "c1",
            "start": 0.0,
            "end": 9.0,
            "in_override": None,
            "out_override": None,
            "fit_mode": "fill",
            "fit_scale": None,
            "crop_x": None,
        }
        patchers = [
            mock.patch.object(
                safezones._reels,
                "_effective_crop_center",
                lambda _log, _clip, _reel, _start, _end: (0.5, 0.45),
            ),
            mock.patch.object(
                safezones.store, "get_clip", lambda _project, _clip_id: _landscape_clip()
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _patch_bboxes(self, by_sample_index: dict):
        def fake_face_bbox_at(_path, t):
            j = int(round(t - 0.5))
            return by_sample_index.get(j)

        p = mock.patch.object(safezones.faces, "face_bbox_at", side_effect=fake_face_bbox_at)
        p.start()
        self.addCleanup(p.stop)

    def _bbox_frac(self) -> tuple[float, float, float, float]:
        return 750 / 1920, 850 / 1080, 120 / 1920, 180 / 1080

    def test_legacy_fill_reel_migrates_to_zoom_one_and_analyzes(self):
        self._patch_bboxes({j: self._bbox_frac() for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        # fit_mode="fill" (or missing) migrates to zoom=1.0 -- same baseline
        # geometry as ZoomedFramingTests.test_zoom_one_default_is_unsafe.
        self.assertFalse(
            result["safe"], f"expected the legacy 'fill' migration to match zoom=1.0, got {result}"
        )
        self.assertEqual(
            self.reel["transform"], {"zoom": 1.0, "offset_x": 0.0, "offset_y": 0.0}
        )

    def test_legacy_fit_blur_reel_migrates_zoom_and_clears(self):
        self.reel["fit_mode"] = "fit_blur"
        self.reel["fit_scale"] = 0.6
        self._patch_bboxes({j: self._bbox_frac() for j in range(safezones.SAMPLE_COUNT)})
        result = safezones.analyze(self.project, self.reel, "tiktok")
        self.assertTrue(
            result["safe"], f"expected the legacy fit_blur(0.6) migration to clear, got {result}"
        )
        self.assertAlmostEqual(self.reel["transform"]["zoom"], 0.6)


class GeometryHelperTests(unittest.TestCase):
    """Lower-level pure functions, independent of analyze()'s control flow."""

    def test_inner_region_is_centered_60_percent(self):
        x, y, w, h = safezones._inner_region((0.2, 0.3, 0.4, 0.5))
        self.assertAlmostEqual(w, 0.4 * 0.6)
        self.assertAlmostEqual(h, 0.5 * 0.6)
        self.assertAlmostEqual(x, 0.2 + 0.4 * 0.2)
        self.assertAlmostEqual(y, 0.3 + 0.5 * 0.2)

    def test_hit_zone_requires_more_than_threshold_not_any_touch(self):
        zones = [{"name": "z", "x": 0.0, "y": 0.9, "w": 1.0, "h": 0.1}]
        # a bbox mostly OUTSIDE the zone, only its bottom edge crossing into
        # it (~4.8% of the bbox's own area) -> geometrically touches, but not
        # a real hit under the new overlap-fraction criterion
        grazing = (0.0, 0.88, 0.2, 0.021)
        self.assertGreater(safezones._intersection_area(grazing, zones[0]), 0.0)  # does touch
        self.assertIsNone(safezones._hit_zone(grazing, zones))
        # a bbox mostly inside the zone -> hit
        deep = (0.0, 0.92, 0.2, 0.08)
        self.assertEqual(safezones._hit_zone(deep, zones), "z")

    def test_hit_zone_picks_best_overlapping_zone(self):
        zones = [
            {"name": "small_overlap", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.30},
            {"name": "big_overlap", "x": 0.0, "y": 0.25, "w": 1.0, "h": 0.75},
        ]
        bbox = (0.0, 0.0, 1.0, 1.0)
        self.assertEqual(safezones._hit_zone(bbox, zones), "big_overlap")

    def test_stabilize_discards_size_outliers(self):
        face = (0.4, 0.4, 0.2, 0.2)  # area 0.04, repeats -> the "real" face
        necklace = (0.45, 0.7, 0.05, 0.05)  # area 0.0025, tiny -> outlier
        boxes = [face, face, face, necklace, face]
        out = safezones._stabilize(boxes)
        self.assertEqual(out[0], face)
        self.assertEqual(out[2], face)
        self.assertIsNone(out[3])  # the necklace-sized outlier got damped
        self.assertEqual(out[4], face)

    def test_stabilize_leaves_uniform_sizes_alone(self):
        face = (0.4, 0.4, 0.2, 0.2)
        boxes = [face, face, None, face]
        self.assertEqual(safezones._stabilize(boxes), boxes)

    def test_stabilize_needs_minimum_detections_before_judging_outliers(self):
        # Only 2 detections -- not enough signal to call either an outlier.
        a, b = (0.4, 0.4, 0.2, 0.2), (0.4, 0.4, 0.01, 0.01)
        self.assertEqual(safezones._stabilize([a, b]), [a, b])

    def test_sustained_hits_requires_consecutive_same_zone(self):
        zones = [None, "z1", None, "z1", "z1", None, "z2", "z2", "z2"]
        runs = safezones._sustained_hits(zones)
        self.assertEqual(runs, [(3, 4, "z1"), (6, 8, "z2")])

    def test_sustained_hits_different_adjacent_zones_dont_merge(self):
        zones = ["z1", "z2", "z2"]
        runs = safezones._sustained_hits(zones)
        self.assertEqual(runs, [(1, 2, "z2")])  # the lone leading z1 hit is dropped


if __name__ == "__main__":
    unittest.main(verbosity=2)
