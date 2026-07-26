#!/usr/bin/env python3
"""Unit tests for original-video timestamp capture (spec point 5 foundation,
workstream A).

Covers `_new_clip_record()`'s `recorded_at`/`recorded_at_source` resolution
order (magic_video_editor/pipeline/ingest.py):
  1. `info["creation_time_raw"]` parsed as ISO8601 (typically UTC with a
     trailing 'Z') -- source="metadata".
  2. else `os.path.getmtime()` of the SOURCE path (not the imported
     hardlink/copy) -- source="mtime".
  3. else None/"unknown" (e.g. the source file is missing).

`ffmpeg_utils.clip_info()`/`probe()` are mocked throughout -- no real ffprobe
call, no dependency on an actual media file's content. `_import_into_project`
is also mocked so no real hardlink/copy happens; the "imported" path is a
plain in-tmp-dir stand-in.

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_take_selection.py.

Usage:
    uv run python scripts/test_recorded_at_metadata.py
    uv run python scripts/test_recorded_at_metadata.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_recorded_at_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config, ffmpeg_utils  # noqa: E402
from magic_video_editor.pipeline import ingest  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _base_info(creation_time_raw: str | None) -> dict:
    """A clip_info()-shaped dict with every existing key present (as the real
    function would return), plus the new creation_time_raw field."""
    return {
        "duration": 12.0,
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "has_video": True,
        "has_audio": True,
        "size_bytes": 123456,
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
        "creation_time_raw": creation_time_raw,
    }


class ClipInfoCreationTimeTests(unittest.TestCase):
    """ffmpeg_utils.clip_info() surfaces creation_time_raw additively."""

    def test_creation_time_raw_present_in_tags(self) -> None:
        fake_probe_data = {
            "format": {
                "duration": "12.0",
                "size": "123456",
                "tags": {"creation_time": "2026-07-20T14:32:10.000000Z"},
            },
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264",
                 "pix_fmt": "yuv420p", "avg_frame_rate": "30/1"},
            ],
        }
        with mock.patch.object(ffmpeg_utils, "probe", return_value=fake_probe_data):
            info = ffmpeg_utils.clip_info("dummy.mp4")
        self.assertEqual(info["creation_time_raw"], "2026-07-20T14:32:10.000000Z")
        # existing keys untouched
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["codec_name"], "h264")

    def test_creation_time_raw_none_when_no_tags(self) -> None:
        fake_probe_data = {
            "format": {"duration": "12.0", "size": "123456"},
            "streams": [],
        }
        with mock.patch.object(ffmpeg_utils, "probe", return_value=fake_probe_data):
            info = ffmpeg_utils.clip_info("dummy.mp4")
        self.assertIsNone(info["creation_time_raw"])


class RecordedAtResolutionTests(unittest.TestCase):
    """_new_clip_record()'s recorded_at / recorded_at_source resolution."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mve_recorded_at_case_")
        self.source = Path(self.tmpdir) / "source.mp4"
        self.source.write_bytes(b"fake video bytes")
        self.imported = Path(self.tmpdir) / "imported.mp4"
        self.imported.write_bytes(b"fake video bytes")

    def _make_record(self, info: dict | None) -> dict:
        with mock.patch.object(ingest, "_probe_info_for_import", return_value=info):
            return ingest._new_clip_record(self.imported, self.source, "main")

    def test_metadata_tag_wins_when_present(self) -> None:
        info = _base_info("2026-07-20T14:32:10.000000Z")
        record = self._make_record(info)
        self.assertEqual(record["recorded_at_source"], "metadata")
        expected = datetime(2026, 7, 20, 14, 32, 10, tzinfo=UTC).timestamp()
        self.assertAlmostEqual(record["recorded_at"], expected, places=3)

    def test_falls_back_to_mtime_when_no_tag(self) -> None:
        info = _base_info(None)
        expected_mtime = os.path.getmtime(self.source)
        record = self._make_record(info)
        self.assertEqual(record["recorded_at_source"], "mtime")
        self.assertAlmostEqual(record["recorded_at"], expected_mtime, places=3)

    def test_falls_back_to_mtime_when_info_is_none(self) -> None:
        """info can be None entirely (e.g. _probe_info_for_import swallowed an
        unreadable/corrupt file) -- must still fall through to mtime, not raise."""
        expected_mtime = os.path.getmtime(self.source)
        record = self._make_record(None)
        self.assertEqual(record["recorded_at_source"], "mtime")
        self.assertAlmostEqual(record["recorded_at"], expected_mtime, places=3)

    def test_missing_source_file_yields_unknown(self) -> None:
        missing_source = Path(self.tmpdir) / "does_not_exist.mp4"
        info = _base_info(None)
        with mock.patch.object(ingest, "_probe_info_for_import", return_value=info):
            record = ingest._new_clip_record(self.imported, missing_source, "main")
        self.assertIsNone(record["recorded_at"])
        self.assertEqual(record["recorded_at_source"], "unknown")

    def test_unparseable_creation_time_falls_back_to_mtime(self) -> None:
        info = _base_info("not-a-real-timestamp")
        expected_mtime = os.path.getmtime(self.source)
        record = self._make_record(info)
        self.assertEqual(record["recorded_at_source"], "mtime")
        self.assertAlmostEqual(record["recorded_at"], expected_mtime, places=3)

    def test_existing_clip_record_fields_still_present(self) -> None:
        """Additive/backward-compatible: every pre-existing key is still there
        with its usual shape."""
        info = _base_info("2026-07-20T14:32:10.000000Z")
        record = self._make_record(info)
        for key in (
            "id", "path", "source_path", "filename", "role", "camera_group",
            "is_main", "info", "wav", "transcript", "language",
        ):
            self.assertIn(key, record)
        self.assertEqual(record["source_path"], str(self.source))
        self.assertEqual(record["path"], str(self.imported))


class ParseCreationTimeTests(unittest.TestCase):
    """_parse_creation_time() ISO8601-with-Z handling directly."""

    def test_parses_z_suffix(self) -> None:
        ts = ingest._parse_creation_time("2026-07-20T14:32:10.000000Z")
        expected = datetime(2026, 7, 20, 14, 32, 10, tzinfo=UTC).timestamp()
        self.assertAlmostEqual(ts, expected, places=3)

    def test_returns_none_on_garbage(self) -> None:
        self.assertIsNone(ingest._parse_creation_time("garbage"))

    def test_returns_none_on_none_input(self) -> None:
        self.assertIsNone(ingest._parse_creation_time(None))


if __name__ == "__main__":
    unittest.main()
