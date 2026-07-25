#!/usr/bin/env python3
"""Unit tests for the v7.11 "Reel framing v2" transform mapping
(magic_video_editor/pipeline/faces.py's transform_crop_rect/
transform_needs_blur/transform_vertical_crop_filter) and the reels.py/
api/reels.py migration + normalization built on top of it.

No pytest in this project's dependency set (see pyproject.toml) -- stdlib
unittest, same spirit as scripts/test_updater.py.

Covers:
  - zoom=1.0 reproduces faces.vertical_crop_filter's legacy full-height crop
    rect exactly (same numbers, not just "close").
  - zoom=0.8 grows the crop window symmetrically (centered, wider than the
    zoom=1.0 baseline) and is correctly flagged as needing the blurred
    cover background (crop aspect wider than the 9:16 target).
  - zoom>1.0 punches in (smaller crop, same 9:16 aspect, never needs blur).
  - offset_x/offset_y pan and clamp to the available room.
  - pipeline/reels.py's legacy-field migration ({crop_x, fit_mode,
    fit_scale} -> transform) and api/reels.py's PATCH-time equivalent.

Usage:
    uv run python scripts/test_reel_transform.py
    uv run python scripts/test_reel_transform.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from magic_video_editor.api import reels as reels_api  # noqa: E402
from magic_video_editor.pipeline import faces, reels  # noqa: E402

# A typical 16:9 camera source, big enough that rounding artifacts don't
# dominate the assertions.
SRC_W, SRC_H = 1920, 1080
OUT_W, OUT_H = 1080, 1920


class TransformCropRectTests(unittest.TestCase):
    def test_zoom_one_matches_legacy_vertical_crop_filter(self):
        """zoom=1.0, offset 0/0 must reproduce faces.vertical_crop_filter's
        crop rect EXACTLY -- this is the "no behavior change for the
        untouched case" guarantee the spec asks for."""
        center = (0.5, 0.45)
        legacy = faces.vertical_crop_filter(SRC_W, SRC_H, center, OUT_W, OUT_H)
        x, y, w, h = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.0, 0.0, 0.0, OUT_W, OUT_H
        )
        self.assertEqual(legacy, f"crop={w}:{h}:{x}:{y}")
        # And, independently, the legacy crop is full-height and very close
        # to 9:16 (int-pixel rounding of a 1080x1920 target keeps it within
        # a fraction of a percent, same tolerance the pre-v7.11 code lived
        # with -- see transform_needs_blur's docstring for why the "needs
        # blur" decision is based on the zoom value, not this ratio).
        self.assertEqual(h, SRC_H)
        self.assertAlmostEqual(w / h, OUT_W / OUT_H, delta=0.001)

    def test_zoom_one_offcenter_face_matches_legacy(self):
        """Same guarantee, but with an off-center detected face -- pins down
        that the shared x/y clamping logic (not just the trivial centered
        case) is bit-for-bit identical to the old function."""
        for center in [(0.15, 0.45), (0.85, 0.45), (0.5, 0.1), (0.5, 0.9)]:
            legacy = faces.vertical_crop_filter(SRC_W, SRC_H, center, OUT_W, OUT_H)
            x, y, w, h = faces.transform_crop_rect(
                SRC_W, SRC_H, center, 1.0, 0.0, 0.0, OUT_W, OUT_H
            )
            self.assertEqual(legacy, f"crop={w}:{h}:{x}:{y}", f"mismatch for center={center}")

    def test_zoom_punch_in_shrinks_same_aspect_never_needs_blur(self):
        center = (0.5, 0.45)
        _x1, _y1, w1, h1 = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.0, 0.0, 0.0, OUT_W, OUT_H
        )
        for zoom in (1.5, 2.0, 3.0):
            x, y, w, h = faces.transform_crop_rect(
                SRC_W, SRC_H, center, zoom, 0.0, 0.0, OUT_W, OUT_H
            )
            self.assertLess(w, w1)
            self.assertLess(h, h1)
            self.assertAlmostEqual(w / h, OUT_W / OUT_H, places=2)
            self.assertFalse(faces.transform_needs_blur(zoom))
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)

    def test_zoom_out_grows_symmetrically_and_needs_blur(self):
        """zoom=0.8: crop window widens (height is already maxed at src_h,
        so it stays put while width grows) -- centered (symmetric) since
        offset is 0 -- and the resulting crop, being wider than 9:16, must
        be flagged as needing the blurred cover background."""
        center = (0.5, 0.45)
        _x1, _y1, w1, h1 = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.0, 0.0, 0.0, OUT_W, OUT_H
        )
        x, y, w, h = faces.transform_crop_rect(SRC_W, SRC_H, center, 0.8, 0.0, 0.0, OUT_W, OUT_H)
        self.assertGreater(w, w1)
        self.assertEqual(h, SRC_H)  # can't grow past the actual source height
        self.assertEqual(h, h1)  # unchanged from the zoom=1.0 baseline
        # symmetric growth around the same (0.5) center -> x is centered
        self.assertAlmostEqual(x, (SRC_W - w) / 2.0, delta=1)
        self.assertTrue(faces.transform_needs_blur(0.8))

        # And the cover threshold really is exactly zoom==1.0 for this source
        # shape (base crop height already == src_h, spec's "the classic
        # full-height 9:16 crop exactly covers the frame").
        self.assertEqual(faces.TRANSFORM_COVER_THRESHOLD_ZOOM, 1.0)

    def test_offsets_pan_and_clamp(self):
        center = (0.5, 0.45)
        x0, y0, w, h = faces.transform_crop_rect(SRC_W, SRC_H, center, 1.5, 0.0, 0.0, OUT_W, OUT_H)
        x_pos, _y, _w2, _h2 = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.5, 1.0, 0.0, OUT_W, OUT_H
        )
        x_neg, _y, _w3, _h3 = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.5, -1.0, 0.0, OUT_W, OUT_H
        )
        self.assertGreater(x_pos, x0)
        self.assertLess(x_neg, x0)
        # extreme offsets are clamped inside the source bounds, not beyond
        self.assertGreaterEqual(x_neg, 0)
        self.assertLessEqual(x_pos, SRC_W - w)
        # and offsets clamp to the -1..1 range regardless of what's passed in
        x_over, *_ = faces.transform_crop_rect(SRC_W, SRC_H, center, 1.5, 5.0, 0.0, OUT_W, OUT_H)
        self.assertEqual(x_over, x_pos)

    def test_offset_y_room_is_zero_at_zoom_one(self):
        """At zoom=1.0 the crop height already equals src_h (no vertical
        room at all) -- offset_y must be a no-op there, matching the
        pre-v7.11 behavior where the vertical crop position never moved."""
        center = (0.5, 0.45)
        _x, y0, _w, _h = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.0, 0.0, 0.0, OUT_W, OUT_H
        )
        _x, y1, _w, _h = faces.transform_crop_rect(
            SRC_W, SRC_H, center, 1.0, 0.0, 1.0, OUT_W, OUT_H
        )
        self.assertEqual(y0, y1)
        self.assertEqual(y0, 0)

    def test_zoom_clamped_to_spec_range(self):
        center = (0.5, 0.45)
        lo_w = faces.transform_crop_rect(SRC_W, SRC_H, center, 0.1, 0.0, 0.0, OUT_W, OUT_H)
        clamp_lo = faces.transform_crop_rect(
            SRC_W, SRC_H, center, faces.TRANSFORM_ZOOM_MIN, 0.0, 0.0, OUT_W, OUT_H
        )
        self.assertEqual(lo_w, clamp_lo)
        hi = faces.transform_crop_rect(SRC_W, SRC_H, center, 99.0, 0.0, 0.0, OUT_W, OUT_H)
        clamp_hi = faces.transform_crop_rect(
            SRC_W, SRC_H, center, faces.TRANSFORM_ZOOM_MAX, 0.0, 0.0, OUT_W, OUT_H
        )
        self.assertEqual(hi, clamp_hi)


class TransformFilterStringTests(unittest.TestCase):
    def test_fill_zoom_is_plain_crop(self):
        vf = faces.transform_vertical_crop_filter(
            SRC_W, SRC_H, (0.5, 0.45), 1.2, 0.0, 0.0, OUT_W, OUT_H
        )
        self.assertTrue(vf.startswith("crop="))
        self.assertNotIn("overlay", vf)

    def test_blur_zoom_out_has_split_and_overlay(self):
        vf = faces.transform_vertical_crop_filter(
            SRC_W, SRC_H, (0.5, 0.45), 0.7, 0.0, 0.0, OUT_W, OUT_H
        )
        self.assertIn("split=2", vf)
        self.assertIn("boxblur", vf)
        self.assertIn("overlay", vf)
        self.assertTrue(vf.startswith("crop="))


class ReelMigrationTests(unittest.TestCase):
    """pipeline/reels.py's read-time migration off the legacy
    {crop_x, fit_mode, fit_scale} trio (spec v7.11 "migration on read")."""

    def _base_reel(self, **extra):
        reel = {
            "id": "r1",
            "rank": 1,
            "clip_id": "c1",
            "start": 0.0,
            "end": 10.0,
            "in_override": None,
            "out_override": None,
            "cue_overrides": {},
            "subtitle_style": {},
            "segments": [{"clip_id": "c1", "start": 0.0, "end": 10.0,
                          "in_override": None, "out_override": None}],
            "transitions": [],
            "composed": False,
        }
        reel.update(extra)
        return reel

    def test_plain_fill_migrates_to_zoom_one(self):
        reel = self._base_reel(fit_mode="fill", fit_scale=0.82)
        reels._normalize_transform(reel)
        self.assertEqual(reel["transform"], {"zoom": 1.0, "offset_x": 0.0, "offset_y": 0.0})

    def test_no_legacy_fields_defaults_to_zoom_one(self):
        reel = self._base_reel()
        reels._normalize_transform(reel)
        self.assertEqual(reel["transform"], {"zoom": 1.0, "offset_x": 0.0, "offset_y": 0.0})

    def test_fit_blur_migrates_zoom_to_fit_scale(self):
        reel = self._base_reel(fit_mode="fit_blur", fit_scale=0.7)
        reels._normalize_transform(reel)
        self.assertEqual(reel["transform"]["zoom"], 0.7)

    def test_crop_x_migrates_to_offset_x(self):
        reel = self._base_reel(crop_x=0.75)
        reels._normalize_transform(reel)
        self.assertAlmostEqual(reel["transform"]["offset_x"], 0.5, places=6)
        self.assertEqual(reel["transform"]["zoom"], 1.0)

    def test_crop_x_and_fit_blur_combine(self):
        reel = self._base_reel(crop_x=0.0, fit_mode="fit_blur", fit_scale=0.65)
        reels._normalize_transform(reel)
        self.assertAlmostEqual(reel["transform"]["offset_x"], -1.0, places=6)
        self.assertEqual(reel["transform"]["zoom"], 0.65)

    def test_migration_is_idempotent_once_transform_exists(self):
        """Once transform exists, a stale crop_x/fit_scale sitting in the
        same dict must NOT be re-applied on a later normalize call (a user
        who has since panned/zoomed shouldn't get clobbered)."""
        reel = self._base_reel(
            crop_x=0.9, transform={"zoom": 2.0, "offset_x": -0.3, "offset_y": 0.1}
        )
        reels._normalize_transform(reel)
        self.assertEqual(reel["transform"], {"zoom": 2.0, "offset_x": -0.3, "offset_y": 0.1})

    def test_transform_values_clamped_to_spec_range(self):
        reel = self._base_reel(transform={"zoom": 99, "offset_x": 5, "offset_y": -9})
        reels._normalize_transform(reel)
        self.assertEqual(reel["transform"]["zoom"], faces.TRANSFORM_ZOOM_MAX)
        self.assertEqual(reel["transform"]["offset_x"], 1.0)
        self.assertEqual(reel["transform"]["offset_y"], -1.0)


class ApiLegacyPatchMappingTests(unittest.TestCase):
    """api/reels.py's _legacy_fields_to_transform -- the PATCH-time
    equivalent of the read-time migration above, exercised directly (no HTTP
    layer needed for this pure function)."""

    def test_crop_x_only(self):
        out = reels_api._legacy_fields_to_transform({"crop_x": 0.25})
        self.assertAlmostEqual(out["offset_x"], -0.5, places=6)
        self.assertNotIn("zoom", out)

    def test_fit_blur_with_scale(self):
        out = reels_api._legacy_fields_to_transform({"fit_mode": "fit_blur", "fit_scale": 0.75})
        self.assertEqual(out["zoom"], 0.75)

    def test_fit_blur_without_scale_falls_back_to_legacy_default(self):
        out = reels_api._legacy_fields_to_transform({"fit_mode": "fit_blur"})
        self.assertEqual(out["zoom"], reels.LEGACY_DEFAULT_FIT_SCALE)

    def test_fit_scale_alone_implies_fit_blur(self):
        out = reels_api._legacy_fields_to_transform({"fit_scale": 0.9})
        self.assertEqual(out["zoom"], 0.9)

    def test_fill_resets_zoom_to_one_and_ignores_fit_scale(self):
        out = reels_api._legacy_fields_to_transform({"fit_mode": "fill", "fit_scale": 0.6})
        self.assertEqual(out["zoom"], 1.0)

    def test_no_legacy_fields_is_empty(self):
        self.assertEqual(reels_api._legacy_fields_to_transform({}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
