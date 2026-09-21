#!/usr/bin/env python3
"""Regression tests for stale-EDL and suggestion-restore bugs.

- Toggling a sentence's kept flag drops the cached EDL, and GET /edl rebuilds
  from the sentences (an empty cached list is not treated as final).
- Changing pacing or clip order drops the cached EDL.
- Accepting a judge "restore" suggestion marks the sentences kept again.
- build_edl does not crash when a clip has no probe info yet.
- GET /media/file does not authorize a sibling directory via string prefix.

MVE_DATA must be set before importing magic_video_editor.
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

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_stale_edl_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.pipeline import ordering  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH)


def _project() -> dict:
    project = store.new_project("stale-edl")
    project["clips"] = [
        {
            "id": "clipA",
            "path": "/fake/clipA.mp4",
            "source_path": "/fake/clipA.mp4",
            "filename": "clipA.mp4",
            "role": "camera",
            "camera_group": "main",
            "is_main": True,
            "info": {"duration": 40.0, "has_audio": True, "has_video": True},
        }
    ]
    project["clip_order"] = ["clipA"]
    project["sentences"] = [
        {
            "id": "s1",
            "clip_id": "clipA",
            "start": 2.0,
            "end": 8.0,
            "text": "Primera frase con contenido real.",
            "kept": True,
            "reason": "",
        },
        {
            "id": "s2",
            "clip_id": "clipA",
            "start": 12.0,
            "end": 18.0,
            "text": "Segunda frase que sigue en el corte.",
            "kept": True,
            "reason": "",
        },
        {
            "id": "s3",
            "clip_id": "clipA",
            "start": 22.0,
            "end": 28.0,
            "text": "Tercera frase que el juez quitó.",
            "kept": False,
            "reason": "cut by judge",
        },
    ]
    project["edl"] = [
        {
            "clip_id": "clipA",
            "start": 1.4,
            "end": 8.12,
            "text": "stale",
            "transition": {"type": "none", "duration": 0.5},
        }
    ]
    project["suggestions"] = [
        {
            "id": "sug1",
            "kind": "lost_content",
            "sentence_ids": ["s3"],
            "message": "Se perdió contenido.",
            "proposed_action": "restore",
            "status": "open",
        }
    ]
    store.save(project)
    return project


class StaleEdlAndRestore(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_sentence_toggle_drops_cached_edl(self):
        project = _project()
        resp = self.client.post(
            f"/api/projects/{project['id']}/sentences/s2",
            json={"kept": False},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        saved = store.load(project["id"])
        self.assertIsNone(saved["edl"])
        self.assertFalse(next(s for s in saved["sentences"] if s["id"] == "s2")["kept"])

        rebuilt = self.client.get(f"/api/projects/{project['id']}/edl")
        self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
        texts = [s["text"] for s in rebuilt.json()["segments"]]
        self.assertTrue(any("Primera frase" in t for t in texts))
        self.assertFalse(any("Segunda frase" in t for t in texts))

    def test_empty_cached_edl_rebuilds_when_sentences_exist(self):
        project = _project()
        project["edl"] = []
        store.save(project)
        resp = self.client.get(f"/api/projects/{project['id']}/edl")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertGreater(len(resp.json()["segments"]), 0)

    def test_pacing_change_drops_cached_edl(self):
        project = _project()
        resp = self.client.patch(
            f"/api/projects/{project['id']}",
            json={"pacing": "airy"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(store.load(project["id"])["edl"])
        self.assertEqual(store.load(project["id"])["pacing"], "airy")

    def test_same_pacing_keeps_edl(self):
        project = _project()
        project["pacing"] = "natural"
        store.save(project)
        resp = self.client.patch(
            f"/api/projects/{project['id']}",
            json={"pacing": "natural"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(store.load(project["id"])["edl"])

    def test_manual_order_drops_cached_edl(self):
        project = _project()
        resp = self.client.post(
            f"/api/projects/{project['id']}/order",
            json={"clip_order": ["clipA"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(store.load(project["id"])["edl"])

    def test_accept_restore_brings_sentence_back(self):
        project = _project()
        resp = self.client.post(
            f"/api/projects/{project['id']}/suggestions/sug1/accept"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["suggestion"]["status"], "accepted")
        saved = store.load(project["id"])
        s3 = next(s for s in saved["sentences"] if s["id"] == "s3")
        self.assertTrue(s3["kept"])
        self.assertEqual(s3["reason"], "")
        self.assertIsNone(saved["edl"])
        rebuilt = self.client.get(f"/api/projects/{project['id']}/edl")
        texts = " ".join(s["text"] for s in rebuilt.json()["segments"])
        self.assertIn("Tercera frase", texts)

    def test_build_edl_survives_missing_probe_info(self):
        project = _project()
        project["clips"][0]["info"] = None
        project["edl"] = None
        segments = ordering.build_edl(project)
        self.assertGreater(len(segments), 0)
        self.assertGreater(segments[0]["end"], segments[0]["start"])

    def test_media_file_rejects_sibling_prefix(self):
        project = _project()
        pid = project["id"]
        pdir = store.project_dir(pid)
        inside = pdir / "ok.txt"
        inside.write_text("ok")
        sibling = pdir.parent / f"{pid}_evil"
        sibling.mkdir()
        secret = sibling / "secret.txt"
        secret.write_text("nope")

        ok = self.client.get(
            f"/api/projects/{pid}/media/file", params={"path": str(inside)}
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        blocked = self.client.get(
            f"/api/projects/{pid}/media/file", params={"path": str(secret)}
        )
        self.assertEqual(blocked.status_code, 403, blocked.text)

    def test_audio_preview_builds_edl_when_missing(self):
        project = _project()
        project["edl"] = None
        store.save(project)
        resp = self.client.post(
            f"/api/projects/{project['id']}/audio-preview-at",
            json={"start_s": 0.0, "duration_s": 2.0},
        )
        # The clip file is fake, so ffmpeg may fail — but not for lack of an EDL.
        if resp.status_code != 200:
            self.assertNotIn("no EDL", resp.text)
        saved = store.load(project["id"])
        self.assertTrue(saved.get("edl"))


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
