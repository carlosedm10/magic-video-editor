#!/usr/bin/env python3
"""Regression tests for POST /api/open-folder — platform-specific folder openers
with home-directory safety.

MVE_DATA must be set (to a fresh tmp dir) BEFORE importing anything from
magic_video_editor, since magic_video_editor.config reads it at import time.

Usage:
    /tmp/mve-venv/bin/python scripts/test_open_folder.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_open_folder_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


class OpenFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.home = Path.home().resolve()
        self.inside = self.home / "mve_open_folder_test_inside"
        self.outside = Path("/tmp/mve_open_folder_outside_non_home")

    def tearDown(self) -> None:
        if self.inside.is_dir():
            self.inside.rmdir()

    @patch("magic_video_editor.server.subprocess.run")
    def test_inside_home_darwin_uses_open(self, mock_run) -> None:
        with patch("magic_video_editor.server.sys.platform", "darwin"):
            r = self.client.post("/api/open-folder", json={"path": str(self.inside)})
        self.assertEqual(r.status_code, 200)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][0], "open")

    @patch("magic_video_editor.server.subprocess.run")
    def test_inside_home_linux_uses_xdg_open(self, mock_run) -> None:
        with patch("magic_video_editor.server.sys.platform", "linux"):
            r = self.client.post("/api/open-folder", json={"path": str(self.inside)})
        self.assertEqual(r.status_code, 200)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][0], "xdg-open")

    @patch("magic_video_editor.server.subprocess.run")
    def test_outside_home_returns_400_no_subprocess(self, mock_run) -> None:
        with patch("magic_video_editor.server.sys.platform", "linux"):
            r = self.client.post("/api/open-folder", json={"path": str(self.outside)})
        self.assertEqual(r.status_code, 400)
        mock_run.assert_not_called()

    @patch(
        "magic_video_editor.server.subprocess.run",
        side_effect=FileNotFoundError("xdg-open"),
    )
    def test_missing_opener_returns_400(self, _mock_run) -> None:
        with patch("magic_video_editor.server.sys.platform", "linux"):
            r = self.client.post("/api/open-folder", json={"path": str(self.inside)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("folder opener", r.json()["detail"].lower())

    @patch(
        "magic_video_editor.server.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["xdg-open"]),
    )
    def test_opener_failure_returns_400(self, _mock_run) -> None:
        with patch("magic_video_editor.server.sys.platform", "linux"):
            r = self.client.post("/api/open-folder", json={"path": str(self.inside)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("could not open folder", r.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
