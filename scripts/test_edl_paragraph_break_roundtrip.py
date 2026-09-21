#!/usr/bin/env python3
"""Regression: EDL PUT must round-trip paragraph_break on each segment.

Without the flag in the PUT body, Pydantic defaults paragraph_break to False
and autosave wipes paragraph-break hints from the timeline.

Usage:
    uv run python scripts/test_edl_paragraph_break_roundtrip.py
    uv run python scripts/test_edl_paragraph_break_roundtrip.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_edl_paragraph_break_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _seg(
    clip_id: str,
    start: float,
    end: float,
    text: str = "",
    paragraph_break: bool = False,
) -> dict:
    return {
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "text": text,
        "transition": {"type": "none", "duration": 0.5},
        "paragraph_break": paragraph_break,
    }


def _make_project(duration: float = 100.0) -> dict:
    project = store.new_project("edl-paragraph-break-roundtrip")
    project["clips"] = [
        {
            "id": "clip0",
            "path": "/fake/clip0.mp4",
            "source_path": "/fake/clip0.mp4",
            "filename": "clip0.mp4",
            "role": "camera",
            "camera_group": "main",
            "is_main": True,
            "info": {"duration": duration, "has_audio": True, "has_video": True},
            "wav": None,
            "transcript": None,
            "language": None,
        }
    ]
    project["sentences"] = []
    store.save(project)
    return project


class EdlParagraphBreakRoundtrip(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_put_true_round_trips(self):
        project = _make_project()
        segments = [_seg("clip0", 0.0, 2.0, paragraph_break=True)]
        resp = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": segments}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["segments"][0]["paragraph_break"])

        get_resp = self.client.get(f"/api/projects/{project['id']}/edl")
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        self.assertTrue(get_resp.json()["segments"][0]["paragraph_break"])

    def test_put_omitted_field_defaults_false(self):
        project = _make_project()
        segment = _seg("clip0", 0.0, 2.0, paragraph_break=True)
        del segment["paragraph_break"]
        resp = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": [segment]}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["segments"][0]["paragraph_break"])

        get_resp = self.client.get(f"/api/projects/{project['id']}/edl")
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        self.assertFalse(get_resp.json()["segments"][0]["paragraph_break"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
