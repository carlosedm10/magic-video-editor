#!/usr/bin/env python3
"""Regression tests for 8-band program EQ on final export and reel render.

Ensures _apply_program_eq is a no-op when audio_eq is flat/missing, runs a
single ffmpeg remux when gains are non-flat, and that preview-manifest hashing
includes audio_eq.

MVE_DATA must be set before importing magic_video_editor (see
scripts/test_edl_same_clip_guard.py).

Usage:
    /tmp/mve-venv/bin/python scripts/test_program_eq.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_program_eq_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)

from magic_video_editor import config  # noqa: E402
from magic_video_editor.pipeline import render  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH})"
)


class _NullLog:
    def __call__(self, _msg: str) -> None:
        pass


class ProgramEqTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = _NullLog()
        self.in_path = _SCRATCH / "program.mp4"
        self.in_path.write_bytes(b"fake-mp4")
        self.out_path = _SCRATCH / "program_eq_out.mp4"

    @mock.patch("magic_video_editor.ffmpeg_utils.run")
    def test_flat_eq_skips_ffmpeg(self, run_mock: mock.MagicMock) -> None:
        project = {"audio_eq": [0.0] * 8}
        self.assertFalse(render._apply_program_eq(self.log, project, self.in_path, self.out_path))
        run_mock.assert_not_called()

    @mock.patch("magic_video_editor.ffmpeg_utils.run")
    def test_missing_eq_skips_ffmpeg(self, run_mock: mock.MagicMock) -> None:
        project = {}
        self.assertFalse(render._apply_program_eq(self.log, project, self.in_path, self.out_path))
        run_mock.assert_not_called()

    @mock.patch("magic_video_editor.ffmpeg_utils.run")
    def test_non_flat_eq_runs_one_ffmpeg_copy_video(self, run_mock: mock.MagicMock) -> None:
        def _touch_out(cmd: list, **_kwargs) -> None:
            Path(cmd[-1]).write_bytes(b"eq-out")

        run_mock.side_effect = _touch_out
        gains = [0.0] * 8
        gains[3] = 3.0
        project = {"audio_eq": gains}
        self.assertTrue(render._apply_program_eq(self.log, project, self.in_path, self.out_path))
        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        cmd_str = " ".join(cmd)
        self.assertIn("equalizer", cmd_str)
        self.assertIn("-c:v", cmd)
        copy_idx = cmd.index("-c:v")
        self.assertEqual(cmd[copy_idx + 1], "copy")

    def test_preview_manifest_includes_audio_eq(self) -> None:
        base = {
            "edl": [],
            "color": None,
            "subtitles": None,
            "audio_enhance": None,
            "audio_track": None,
        }
        flat = {**base, "audio_eq": [0.0] * 8}
        boosted = {**base, "audio_eq": [0.0, 0.0, 0.0, 4.0, 0.0, 0.0, 0.0, 0.0]}
        self.assertNotEqual(
            render._preview_manifest(flat),
            render._preview_manifest(boosted),
        )


if __name__ == "__main__":
    unittest.main()
