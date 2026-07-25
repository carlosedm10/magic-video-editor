#!/usr/bin/env python3
"""End-to-end test for "poder duplicar un proyecto" (project duplication).

Runs entirely against a SCRATCH MVE_DATA dir (never the real
~/Library/Application Support/Magic Video Editor). Builds a source project
by hand (no ffmpeg, no queue worker thread -- just store.py + a dummy media
file) with a clip whose absolute path is baked into project.json, an edl-
relevant sentence, and a pending queue item, then exercises:

  1. store.duplicate_project(pid): new id/dir, copied project.json's baked
     clip path rewritten from the OLD project dir to the NEW one, the media
     file physically present under the NEW dir, edl/sentences/clips content
     preserved, queue cleared, name becomes "<name> (copy)".
  2. A second duplicate_project(pid) on the same source yields "(copy 2)".
  3. The ORIGINAL project is left completely untouched (paths, queue, name).
  4. POST /api/projects/{pid}/duplicate via TestClient: 200 with the new
     project, 404 for a bogus id.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_reel_previews.py. MVE_DATA must be set BEFORE importing
anything from magic_video_editor, since magic_video_editor.config reads it
at import time.

Usage:
    uv run python scripts/test_duplicate_project.py
    uv run python scripts/test_duplicate_project.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_duplicate_project_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _build_source_project(name: str) -> dict:
    """A project with a clip whose absolute path lives under its OWN
    project dir (exactly what add_clips/register_uploaded_clips produce in
    the real app), a sentence referencing that clip (edl-relevant content),
    and a pending queue item (in-flight state that must NOT survive a
    duplicate)."""
    project = store.new_project(name)
    pdir = store.project_dir(project["id"])
    media_dir = pdir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    clip_path = media_dir / "clip1.mp4"
    clip_path.write_bytes(b"not-really-a-video-just-bytes")

    clip_id = uuid.uuid4().hex[:12]
    project["clips"] = [
        {
            "id": clip_id,
            "path": str(clip_path),
            "camera_group": "main",
            "is_main": True,
            "role": "camera",
            "info": {"duration": 3.0, "has_video": True},
        }
    ]
    project["sentences"] = [
        {
            "id": "s1",
            "clip_id": clip_id,
            "text": "hello world",
            "start": 0.0,
            "end": 1.0,
            "kept": True,
        }
    ]
    project["clip_order"] = [clip_id]
    project["queue"] = [
        {
            "id": "q1",
            "kind": "stage:ingest",
            "payload": {},
            "status": "pending",
            "created_at": "2026-07-25T00:00:00",
            "progress": 0.0,
            "job_id": "fake-job-1",
            "error": None,
        }
    ]
    store.save(project, preserve_queue=False)
    return project


class DuplicateProjectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.source = _build_source_project("Vlog Edit")
        cls.pid = cls.source["id"]
        cls.src_dir = store.project_dir(cls.pid)
        cls.src_clip_path = cls.src_dir / "media" / "clip1.mp4"

    def test_duplicate_creates_new_dir_and_rewrites_paths(self):
        dup = store.duplicate_project(self.pid)

        self.assertNotEqual(dup["id"], self.pid)
        new_dir = store.project_dir(dup["id"])
        self.assertTrue(new_dir.exists(), "new project dir was not created")
        self.assertTrue((new_dir / "project.json").exists())

        # (a) new id/dir exists -- checked above.
        # (b) baked paths now point at the NEW dir, not the old one.
        new_clip_path = dup["clips"][0]["path"]
        self.assertNotIn(str(self.src_dir), new_clip_path)
        self.assertTrue(
            new_clip_path.startswith(str(new_dir)),
            f"{new_clip_path} does not live under {new_dir}",
        )

        # (c) the media file physically exists under the new dir.
        self.assertTrue(Path(new_clip_path).exists())
        self.assertEqual(Path(new_clip_path).read_bytes(), b"not-really-a-video-just-bytes")

        # Same guarantee re-reading straight off disk (not just the
        # in-memory dict duplicate_project returned).
        on_disk = json.loads((new_dir / "project.json").read_text())
        self.assertNotIn(str(self.src_dir), json.dumps(on_disk))

        # (d) edl/sentences/clips content preserved.
        self.assertEqual(len(dup["clips"]), 1)
        self.assertEqual(dup["clips"][0]["camera_group"], "main")
        self.assertEqual(dup["clips"][0]["role"], "camera")
        self.assertEqual(dup["sentences"], self.source["sentences"])
        self.assertEqual(dup["clip_order"], self.source["clip_order"])

        # (e) queue is cleared.
        self.assertEqual(dup["queue"], [])

        # (f) name is "<name> (copy)".
        self.assertEqual(dup["name"], "Vlog Edit (copy)")

        # A second duplicate of the SAME source bumps to "(copy 2)".
        dup2 = store.duplicate_project(self.pid)
        self.assertEqual(dup2["name"], "Vlog Edit (copy 2)")
        self.assertNotEqual(dup2["id"], dup["id"])

        # (g) the ORIGINAL project is untouched.
        original = store.load(self.pid)
        self.assertEqual(original["name"], "Vlog Edit")
        self.assertEqual(original["clips"][0]["path"], str(self.src_clip_path))
        self.assertTrue(self.src_clip_path.exists())
        self.assertEqual(len(original["queue"]), 1)
        self.assertEqual(original["queue"][0]["id"], "q1")

    def test_endpoint_returns_new_project(self):
        resp = self.client.post(f"/api/projects/{self.pid}/duplicate")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertNotEqual(body["id"], self.pid)
        self.assertTrue(body["name"].startswith("Vlog Edit (copy"))

    def test_endpoint_404_for_missing_project(self):
        resp = self.client.post("/api/projects/does-not-exist/duplicate")
        self.assertEqual(resp.status_code, 404, resp.text)


if __name__ == "__main__":
    unittest.main()
