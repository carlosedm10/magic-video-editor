#!/usr/bin/env python3
"""End-to-end test for the reel preview render pipeline (spec v7.14 "Reel
preview render").

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor) with a tiny synthetic
ffmpeg-generated clip. Covers:

  1. GET /api/projects/{pid}/media/reel-preview/{reel_id} 404s before any
     preview has been rendered.
  2. Running the "reel_previews" queue runner (magic_video_editor.pipeline.
     reels.render_all_reel_previews, the exact callable KIND_RUNNERS
     dispatches to) renders <project_dir>/previews/reels/{reel_id}.mp4.
  3. The rendered file is ~480x854 H.264 (ffprobe).
  4. The endpoint now serves it, 206 on a Range request with a correct
     Content-Range, 200 without one.
  5. A second run of the SAME job is a no-op (hash-skip: file mtime
     unchanged) because reel["segments"]/["transform"]/["transitions"]
     haven't changed.
  6. Editing the reel's transform via PATCH /api/projects/{pid}/reels/{rid}
     invalidates the preview (preview_ready flips false, "reel_previews" is
     auto-enqueued) and re-running the job re-renders it (mtime changes).

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_reel_transform.py. MVE_DATA must be set (to a fresh tmp dir)
BEFORE importing anything from magic_video_editor, since magic_video_editor.config reads
it at import time.

Usage:
    uv run python scripts/test_reel_previews.py
    uv run python scripts/test_reel_previews.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_reel_previews_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, ffmpeg_utils, jobs, store  # noqa: E402
from magic_video_editor.pipeline import ingest, reels  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


class _Log:
    """Minimal stand-in for jobs.JobLog: collects messages, no-op progress,
    never cancels -- exactly what a synchronous direct call to a queue
    runner needs, without spinning up the real queue worker thread (bounded,
    deterministic, no polling)."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    def progress(self, frac: float) -> None:
        pass

    def stage(self, *a, **k) -> None:
        pass


def _make_synthetic_clip(dst: Path, duration: float = 3.0) -> None:
    """A tiny H.264 16:9 clip (testsrc + sine tone) -- cheap, decodable,
    plenty for exercising the crop/subtitle/encode path end to end."""
    cmd = [
        ffmpeg_utils.ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=640x360:rate=24:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


class ReelPreviewsE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

        cls.project = store.new_project("reel-previews-e2e")
        cls.pid = cls.project["id"]

        src_dir = Path(tempfile.mkdtemp(prefix="mve_reel_previews_src_"))
        cls.src_clip = src_dir / "clip.mp4"
        _make_synthetic_clip(cls.src_clip)

        added = ingest.add_clips(cls.project, [str(cls.src_clip)])
        assert len(added) == 1, added
        cls.clip_id = added[0]["id"]

        log = _Log()
        ingest.run(log, cls.project)  # probes info, extracts wav -- no proxy needed for h264
        store.save(cls.project)

        clip = store.get_clip(cls.project, cls.clip_id)
        assert clip["info"]["has_video"] and clip["info"]["codec_name"] == "h264", clip["info"]

        # A fake reel suggestion -- shape matches what reels.suggest() emits,
        # trimmed to what render_reel_preview actually reads.
        cls.reel = {
            "id": "fakereel1",
            "rank": 1,
            "clip_id": cls.clip_id,
            "start": 0.2,
            "end": 2.5,
            "duration": 2.3,
            "title": "Test reel",
            "description": "",
            "hashtags": [],
            "score": 5.0,
            "hook": 1.0,
            "self_contained": 1.0,
            "payoff": 1.0,
            "text": "",
            "path": None,
            "status": "suggested",
            "in_override": None,
            "out_override": None,
            "crop_x": None,
            "cue_overrides": {},
            "subtitle_style": {},
            "transform": dict(reels.DEFAULT_TRANSFORM),
            "segments": [
                {
                    "clip_id": cls.clip_id,
                    "start": 0.2,
                    "end": 2.5,
                    "in_override": None,
                    "out_override": None,
                }
            ],
            "transitions": [],
            "composed": False,
            "preview_ready": False,
            "preview_hash": None,
        }
        cls.project["reels"] = [cls.reel]
        store.save(cls.project)

    @classmethod
    def tearDownClass(cls):
        # A PATCH in test_08 auto-enqueues "reel_previews" via magic_video_editor.queue,
        # which spins up its own real (daemon) background worker thread --
        # bounded wait for it to drain before nuking the scratch dir out from
        # under it, so it doesn't log a spurious "project not found" on its
        # way out (harmless -- daemon thread, process is exiting either way
        # -- but noisy). Bounded per the "no unbounded waits" rule.
        import time as _time

        deadline = _time.time() + 10
        while _time.time() < deadline:
            try:
                project = store.load(cls.pid)
            except FileNotFoundError:
                break
            if not any(i["status"] in ("pending", "running") for i in project.get("queue", [])):
                break
            _time.sleep(0.2)
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def _preview_path(self) -> Path:
        return store.project_dir(self.pid) / "previews" / "reels" / f"{self.reel['id']}.mp4"

    def test_01_endpoint_404s_before_any_render(self):
        r = self.client.get(f"/api/projects/{self.pid}/media/reel-preview/{self.reel['id']}")
        self.assertEqual(r.status_code, 404)

    def test_02_run_reel_previews_job_renders_the_file(self):
        project = store.load(self.pid)
        job = jobs.run_sync(
            "test:reel_previews", lambda log: reels.render_all_reel_previews(log, project)
        )
        self.assertEqual(job["status"], "done", job.get("error"))

        preview_path = self._preview_path()
        self.assertTrue(preview_path.exists(), f"missing {preview_path}")

        reloaded = store.load(self.pid)
        reel = reloaded["reels"][0]
        self.assertTrue(reel["preview_ready"])
        self.assertIsNotNone(reel["preview_hash"])
        self.assertEqual(reel["preview_hash"], reels.reel_content_hash(reel))

    def test_03_preview_is_480x854_h264(self):
        info = ffmpeg_utils.clip_info(str(self._preview_path()))
        self.assertEqual(info["width"], reels.PREVIEW_W)
        self.assertEqual(info["height"], reels.PREVIEW_H)
        self.assertEqual(info["codec_name"], "h264")

    def test_04_endpoint_serves_range_206(self):
        r = self.client.get(
            f"/api/projects/{self.pid}/media/reel-preview/{self.reel['id']}",
            headers={"Range": "bytes=0-1023"},
        )
        self.assertEqual(r.status_code, 206)
        self.assertIn("bytes 0-1023/", r.headers.get("content-range", ""))
        self.assertEqual(r.headers.get("accept-ranges"), "bytes")
        self.assertEqual(len(r.content), 1024)

    def test_05_endpoint_serves_200_without_range(self):
        r = self.client.get(f"/api/projects/{self.pid}/media/reel-preview/{self.reel['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.content), 0)

    def test_06_endpoint_404s_for_unknown_reel_id(self):
        r = self.client.get(f"/api/projects/{self.pid}/media/reel-preview/does-not-exist")
        self.assertEqual(r.status_code, 404)

    def test_07_second_run_skips_unchanged_reel_hash_matches(self):
        preview_path = self._preview_path()
        mtime_before = preview_path.stat().st_mtime_ns

        project = store.load(self.pid)
        log = _Log()
        jobs.run_sync(
            "test:reel_previews2", lambda _log: reels.render_all_reel_previews(log, project)
        )

        mtime_after = preview_path.stat().st_mtime_ns
        self.assertEqual(mtime_before, mtime_after, "hash-skip should leave the file untouched")
        self.assertTrue(
            any("already up to date" in line for line in log.lines),
            f"expected a skip log line, got: {log.lines}",
        )

    def test_08_patch_transform_invalidates_and_requeues(self):
        preview_path = self._preview_path()
        mtime_before = preview_path.stat().st_mtime_ns

        r = self.client.patch(
            f"/api/projects/{self.pid}/reels/{self.reel['id']}",
            json={"transform": {"zoom": 1.8, "offset_x": 0.3}},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["preview_ready"], "a composition-changing PATCH must invalidate")

        project = store.load(self.pid)
        queued_kinds = [i["kind"] for i in project.get("queue", [])]
        self.assertIn("reel_previews", queued_kinds, "PATCH must auto-enqueue reel_previews")

        reel = next(r for r in project["reels"] if r["id"] == self.reel["id"])
        self.assertNotEqual(reel["preview_hash"], reels.reel_content_hash(reel))

        job = jobs.run_sync(
            "test:reel_previews3", lambda log: reels.render_all_reel_previews(log, project)
        )
        self.assertEqual(job["status"], "done", job.get("error"))

        mtime_after = preview_path.stat().st_mtime_ns
        self.assertNotEqual(mtime_before, mtime_after, "an invalidated preview must re-render")

        reloaded = store.load(self.pid)
        reel2 = next(r for r in reloaded["reels"] if r["id"] == self.reel["id"])
        self.assertTrue(reel2["preview_ready"])
        self.assertEqual(reel2["preview_hash"], reels.reel_content_hash(reel2))

    def test_09_patch_that_does_not_touch_composition_leaves_preview_alone(self):
        # A title-only edit must NOT invalidate the preview (spec: the hash
        # covers segments/transform/transitions only).
        r = self.client.patch(
            f"/api/projects/{self.pid}/reels/{self.reel['id']}",
            json={"title": "A different title"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["preview_ready"], "a title-only edit must not invalidate the preview")


def _wait_for_queue_kind_settled(pid: str, kind: str, timeout: float = 10.0) -> None:
    """Bounded wait for every `kind` item in `pid`'s queue to leave pending/
    running (picked up by the real background worker thread `queue.enqueue`
    spins up -- see ReelPreviewsE2ETest.tearDownClass for the same pattern).
    Never used with an unbounded loop."""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        project = store.load(pid)
        items = [i for i in project.get("queue", []) if i["kind"] == kind]
        if items and all(i["status"] not in ("pending", "running") for i in items):
            return
        _time.sleep(0.2)


class ReelPreviewBackfillE2ETest(unittest.TestCase):
    """SEAM 2 (spec v7.14 addendum): projects whose reels predate the
    preview-render feature (or whose render never completed) never get a
    preview otherwise -- GET /media/reel-preview/{id} 404s forever and the
    drawer stays exactly as dead as the bug the whole feature exists to fix.
    GET /api/projects/{pid} (api/projects.py's project_get, via
    _backfill_reel_previews_once) is the self-heal hook: enqueue
    "reel_previews" once per project per process when at least one reel
    lacks a fresh preview, never when everything's already current."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        src_dir = Path(tempfile.mkdtemp(prefix="mve_reel_previews_backfill_src_"))
        cls.src_clip = src_dir / "clip.mp4"
        _make_synthetic_clip(cls.src_clip)

    def _new_project_with_clip(self, name: str) -> tuple[dict, str]:
        project = store.new_project(name)
        added = ingest.add_clips(project, [str(self.src_clip)])
        clip_id = added[0]["id"]
        log = _Log()
        ingest.run(log, project)
        store.save(project)
        return project, clip_id

    def _legacy_reel(self, clip_id: str) -> dict:
        """Shape of a reel suggestion from BEFORE spec v7.14 landed: no
        "segments"/"transform"/"transitions"/"preview_ready"/"preview_hash"
        keys at all -- exactly what an old project.json on disk looks like."""
        return {
            "id": "legacyreel1",
            "rank": 1,
            "clip_id": clip_id,
            "start": 0.2,
            "end": 2.5,
            "duration": 2.3,
            "title": "Legacy reel",
            "description": "",
            "hashtags": [],
            "score": 5.0,
            "hook": 1.0,
            "self_contained": 1.0,
            "payoff": 1.0,
            "text": "",
            "path": None,
            "status": "suggested",
            "in_override": None,
            "out_override": None,
            "crop_x": None,
            "cue_overrides": {},
            "subtitle_style": {},
        }

    def test_01_backfill_enqueues_for_legacy_project_missing_preview(self):
        project, clip_id = self._new_project_with_clip("reel-previews-backfill-legacy")
        pid = project["id"]
        project["reels"] = [self._legacy_reel(clip_id)]
        store.save(project)

        r = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 200, r.text)

        reloaded = store.load(pid)
        queued = [i for i in reloaded.get("queue", []) if i["kind"] == "reel_previews"]
        self.assertEqual(len(queued), 1, "a stale/legacy reel must auto-enqueue reel_previews once")

        _wait_for_queue_kind_settled(pid, "reel_previews")
        rendered = store.load(pid)
        reel = rendered["reels"][0]
        self.assertTrue(reel.get("preview_ready"), "backfilled reel must end up with a fresh preview")
        preview_path = store.project_dir(pid) / "previews" / "reels" / f"{reel['id']}.mp4"
        self.assertTrue(preview_path.exists())

        # A second GET, now that the preview is current, must NOT enqueue a
        # second "reel_previews" item (both the per-process guard and the
        # freshness check itself should prevent it).
        r2 = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(r2.status_code, 200, r2.text)
        again = store.load(pid)
        queued_after = [i for i in again.get("queue", []) if i["kind"] == "reel_previews"]
        self.assertEqual(
            len(queued_after), 1, "a fresh preview must not trigger a repeat enqueue on re-open"
        )

    def test_02_backfill_does_not_enqueue_when_already_fresh(self):
        """Independent of the per-process guard: a project whose reel
        already has a rendered, hash-matching preview must not enqueue
        anything on its very FIRST GET either."""
        project, clip_id = self._new_project_with_clip("reel-previews-backfill-fresh")
        pid = project["id"]
        reel = self._legacy_reel(clip_id)
        reel["id"] = "freshreel1"
        project["reels"] = [reel]
        store.save(project)

        # Bring the reel up to the current shape and render its preview
        # synchronously (same call the real "reel_previews" job makes), so
        # it's genuinely fresh BEFORE the first GET ever inspects it.
        reels.ensure_segments(reel)
        log = _Log()
        job = jobs.run_sync(
            "test:backfill_fresh_seed", lambda _log: reels.render_reel_preview(log, project, reel["id"])
        )
        self.assertEqual(job["status"], "done", job.get("error"))
        store.save(project)

        r = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 200, r.text)

        reloaded = store.load(pid)
        queued = [i for i in reloaded.get("queue", []) if i["kind"] == "reel_previews"]
        self.assertEqual(len(queued), 0, "an already-fresh project must never auto-enqueue on open")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
