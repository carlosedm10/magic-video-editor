#!/usr/bin/env python3
"""Regression tests for the "black preview for manually-imported clips" bug.

Root cause (owner-confirmed, with screenshot -- "los vídeos de MEDIA nunca se
pueden previsualizar: se escuchan pero la pantalla se queda negra"):

  1. server.py's media_preview used to fall back to the ORIGINAL file
     whenever a clip had no proxy (`clip.get("proxy") or clip["path"]`). For
     an HEVC/10-bit/4K iPhone .MOV, Chromium/WKWebView decode the AUDIO
     track but not the video -- sound plays, the frame stays black, forever.
  2. pipeline/ingest.py's `_enqueue_analyze_for_new_clips` used to SKIP a
     project's very first batch of clips entirely (left for the old
     run-all/manual stage flow). Since manual editing is now first-class
     (no pipeline run required), a manually-added clip's first batch never
     got a proxy queued at all -- so media_preview had nothing but the
     undecodable original to serve, permanently.

This file covers the fix:
  - add_clips's FIRST batch now enqueues a lightweight `make_proxy:<id>`
    queue job (pipeline/ingest.py's `run_make_proxy`) for any clip that
    `_proxy_needed()` flags, with no pipeline run required.
  - server.py's media_preview (`_preview_source`) returns 425 ("preview
    proxy not ready") instead of silently serving an undecodable original,
    and keeps serving the original directly when it's already browser-safe.
  - the proxy job is idempotent with ingest.run()'s own proxy step -- a
    later full pipeline run must not redo it.

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor). Uses real, tiny,
lavfi-synthesized ffmpeg clips -- one already browser-safe (h264/yuv420p/
<=1080p), one _proxy_needed flags (h264/yuv420p but height > 1080; codec
alone isn't relied on since a build may lack libx265, and _proxy_needed's
own height check is exercised the same way HEVC would be). Skips itself
(rather than failing) if ffmpeg/ffprobe aren't on PATH.

Every wait is bounded (short polling loops, well under 30s), never
unbounded. No pytest in this project's dependency set -- stdlib unittest,
same spirit as scripts/test_manual_edit.py / test_queue_resilience.py.

Usage:
    uv run python scripts/test_manual_proxy.py
    uv run python scripts/test_manual_proxy.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_manual_proxy_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, ffmpeg_utils, queue, store  # noqa: E402
from magic_video_editor import server as server_mod  # noqa: E402
from magic_video_editor.pipeline import ingest  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

_HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")

# Warm up ffmpeg_utils' OWN binary resolution (ffmpeg_bin()/ffprobe_bin())
# before any bounded-poll test runs. The first call in a fresh environment
# can lazily download a static ffmpeg build (imageio-ffmpeg/static-ffmpeg),
# which is a one-off, possibly slow-over-network cost wholly unrelated to
# the make_proxy queue job's own bounded runtime -- doing it here keeps that
# download OUT of the bounded polling windows below instead of racing them.
if _HAVE_FFMPEG:
    try:
        ffmpeg_utils.ffmpeg_bin()
        ffmpeg_utils.ffprobe_bin()
    except Exception:
        pass

_BOUND = 45.0  # generic bounded-poll ceiling for the real make_proxy queue job (real ffmpeg encode)

_MEDIA_DIR = _SCRATCH / "src_media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _make_synthetic_clip(name: str, color: str, width: int, height: int, duration: float = 1.0) -> Path:
    """A tiny, real, decodable h264/yuv420p mp4 (color bars + a sine tone)
    via ffmpeg's lavfi virtual inputs -- no external fixture files needed.
    `height` is the knob that drives _proxy_needed(): <=1080 is already
    browser-safe, >1080 needs a proxy (mirrors HEVC/10-bit/4K without
    depending on the local ffmpeg build actually shipping libx265)."""
    path = _MEDIA_DIR / name
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d={duration}:r=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
    )
    return path


def _wait_for_item_status(pid: str, item_id: str, statuses: set, timeout: float = _BOUND) -> dict:
    """Bounded poll for queue item `item_id` in project `pid` to reach one
    of `statuses`. Raises AssertionError on timeout (never loops forever)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        project = store.load(pid)
        item = next((i for i in project.get("queue", []) if i["id"] == item_id), None)
        if item is not None:
            last = item
            if item["status"] in statuses:
                return item
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {pid}/{item_id} to reach {statuses}: {last}")


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class FirstBatchEnqueuesProxyWork(unittest.TestCase):
    """The core fix: a project's FIRST batch of clips must now get proxy
    work queued for anything _proxy_needed flags -- no pipeline run
    required (previously _enqueue_analyze_for_new_clips skipped the first
    batch outright, see ingest.py's updated docstring)."""

    def test_needs_proxy_clip_gets_make_proxy_queued_on_first_batch(self):
        clip_path = _make_synthetic_clip("tall.mp4", "red", 640, 1088)
        project = store.new_project("proxy-first-batch-needed")
        self.assertEqual(project.get("stages", {}), {})  # precondition: no pipeline ever ran

        added = ingest.add_clips(project, [str(clip_path)])
        self.assertEqual(len(added), 1)
        clip = added[0]
        self.assertIsNotNone(clip["info"])
        self.assertTrue(ingest._proxy_needed(clip["info"]), "1088p clip must be flagged as needing a proxy")

        reloaded = store.load(project["id"])
        q = reloaded.get("queue", [])
        make_proxy_items = [i for i in q if i["kind"] == f"make_proxy:{clip['id']}"]
        self.assertEqual(
            len(make_proxy_items), 1,
            f"expected exactly one make_proxy job queued for the first batch, got queue={q}",
        )
        analyze_items = [i for i in q if i["kind"].startswith("analyze_clip:")]
        self.assertEqual(analyze_items, [], "first batch must not get the heavy analyze_clip pass")

        item = _wait_for_item_status(project["id"], make_proxy_items[0]["id"], {"done", "error"})
        self.assertEqual(item["status"], "done", item.get("error"))

        final = store.load(project["id"])
        final_clip = store.get_clip(final, clip["id"])
        self.assertIn("proxy", final_clip)
        self.assertIsNotNone(final_clip["proxy"])
        proxy_path = Path(final_clip["proxy"])
        self.assertTrue(proxy_path.exists(), "make_proxy job must actually write the proxy file")

        proxy_info = server_mod.ffmpeg_utils.clip_info(str(proxy_path))
        self.assertEqual(proxy_info["codec_name"], "h264")
        self.assertEqual(proxy_info["pix_fmt"], "yuv420p")
        self.assertLessEqual(proxy_info["height"], 720)

    def test_already_browser_safe_clip_gets_no_proxy_job(self):
        clip_path = _make_synthetic_clip("safe.mp4", "blue", 320, 180)
        project = store.new_project("proxy-first-batch-safe")

        added = ingest.add_clips(project, [str(clip_path)])
        clip = added[0]
        self.assertFalse(ingest._proxy_needed(clip["info"]))

        reloaded = store.load(project["id"])
        q = reloaded.get("queue", [])
        self.assertEqual(
            [i for i in q if i["kind"].startswith("make_proxy:")], [],
            "an already browser-safe clip must not get a proxy job queued",
        )

    def test_incremental_batch_after_pipeline_still_uses_analyze_clip(self):
        """Regression guard: once the pipeline has completed, a newly added
        clip must keep going through the existing full analyze_clip:<id>
        path (transcribe + placement suggestion) -- the new first-batch
        make_proxy path must NOT swallow that case too.

        Exercised through the REAL add_clips() entrypoint (not the branch-
        selection function directly): add_clips also calls
        ordering.invalidate_after_clipset_change() before enqueuing, which
        un-marks a project's done "render"/"order" stage badges the instant
        a clip is added (see ordering.py's invalidate_after_clipset_change
        docstring: "un-dones the order/render/reels stage badges"). ingest.py
        now captures `_completed_pipeline(project)` BEFORE that invalidation
        call and threads it through to `_enqueue_analyze_for_new_clips`, so
        the analyze_clip branch is still correctly selected even though the
        project's stage badges are no longer "done" by the time this
        function runs (see scripts/test_incremental_analyze.py for the
        dedicated end-to-end coverage of that fix)."""
        clip_path = _make_synthetic_clip("later.mp4", "green", 640, 1088)
        project = store.new_project("proxy-incremental-batch")
        project.setdefault("stages", {})["render"] = {"status": "done"}
        clip_path0 = _make_synthetic_clip("first.mp4", "blue", 640, 1088)
        project["clips"].append(ingest._new_clip_record(clip_path0, clip_path0, "main"))
        store.save(project)

        added = ingest.add_clips(project, [str(clip_path)])
        clip = added[0]

        reloaded = store.load(project["id"])
        kinds = {i["kind"] for i in reloaded.get("queue", [])}
        self.assertIn(f"analyze_clip:{clip['id']}", kinds)
        self.assertNotIn(f"make_proxy:{clip['id']}", kinds)


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class MakeProxyIsIdempotentWithFullPipeline(unittest.TestCase):
    """A later full "Run Pipeline" must not redo the proxy work the
    first-batch make_proxy job already did (ingest.py docstring's explicit
    contract)."""

    def test_ingest_run_does_not_regenerate_an_existing_proxy(self):
        clip_path = _make_synthetic_clip("idempotent.mp4", "red", 640, 1088)
        project = store.new_project("proxy-idempotent")
        added = ingest.add_clips(project, [str(clip_path)])
        clip_id = added[0]["id"]

        reloaded = store.load(project["id"])
        item = next(i for i in reloaded["queue"] if i["kind"] == f"make_proxy:{clip_id}")
        item = _wait_for_item_status(project["id"], item["id"], {"done", "error"})
        self.assertEqual(item["status"], "done", item.get("error"))

        project = store.load(project["id"])
        proxy_path = store.get_clip(project, clip_id)["proxy"]
        mtime_before = Path(proxy_path).stat().st_mtime

        class _FakeLog:
            def __call__(self, msg):
                pass

            def progress(self, frac):
                pass

        ingest.run(_FakeLog(), project)

        proxy_after = store.get_clip(project, clip_id)["proxy"]
        self.assertEqual(proxy_after, proxy_path, "ingest.run() must not touch an already-set proxy path")
        self.assertEqual(
            Path(proxy_after).stat().st_mtime, mtime_before,
            "ingest.run() must not re-encode a proxy that already exists (not idempotent)",
        )


class MediaPreviewNotReadyContract(unittest.TestCase):
    """Unit-level coverage of server.py's _preview_source/media_preview
    contract, independent of real queue timing (deterministic: crafts the
    clip dict directly in each state instead of racing a background job)."""

    def _project_with_clip(self, clip: dict) -> dict:
        project = store.new_project(f"preview-contract-{clip['id']}")
        project["clips"].append(clip)
        store.save(project)
        return project

    def test_needed_but_missing_proxy_returns_none(self):
        clip = {
            "id": "c1", "path": "/nonexistent/original.mov", "filename": "original.mov",
            "role": "camera", "info": {"has_video": True, "codec_name": "hevc",
                                        "pix_fmt": "yuv420p10le", "height": 2160},
        }
        self.assertIsNone(server_mod._preview_source(clip))

    def test_needed_and_proxy_ready_returns_proxy_path(self):
        clip = {
            "id": "c2", "path": "/nonexistent/original.mov", "filename": "original.mov",
            "role": "camera", "proxy": "/some/proxy.mp4",
            "info": {"has_video": True, "codec_name": "hevc", "pix_fmt": "yuv420p10le", "height": 2160},
        }
        self.assertEqual(server_mod._preview_source(clip), "/some/proxy.mp4")

    def test_already_browser_safe_returns_original_even_without_proxy_key(self):
        clip = {
            "id": "c3", "path": "/nonexistent/original.mp4", "filename": "original.mp4",
            "role": "camera",
            "info": {"has_video": True, "codec_name": "h264", "pix_fmt": "yuv420p", "height": 720},
        }
        self.assertEqual(server_mod._preview_source(clip), "/nonexistent/original.mp4")

    def test_audio_only_or_unprobed_returns_original(self):
        clip_audio = {"id": "c4", "path": "/nonexistent/a.wav", "filename": "a.wav",
                      "role": "audio", "info": {"has_video": False}}
        clip_unprobed = {"id": "c5", "path": "/nonexistent/b.mov", "filename": "b.mov",
                         "role": "camera", "info": None}
        self.assertEqual(server_mod._preview_source(clip_audio), "/nonexistent/a.wav")
        self.assertEqual(server_mod._preview_source(clip_unprobed), "/nonexistent/b.mov")

    def test_endpoint_returns_425_when_proxy_not_ready(self):
        clip = {
            "id": "c6", "path": "/nonexistent/original.mov", "filename": "original.mov",
            "role": "camera", "camera_group": "main", "is_main": False, "wav": None,
            "transcript": None, "language": None, "source_path": "/nonexistent/original.mov",
            "info": {"has_video": True, "codec_name": "hevc", "pix_fmt": "yuv420p10le", "height": 2160},
        }
        project = self._project_with_clip(clip)
        client = TestClient(server_mod.app)
        resp = client.get(f"/api/projects/{project['id']}/media/preview/{clip['id']}")
        self.assertEqual(resp.status_code, 425, resp.text)
        self.assertIn("not ready", resp.json().get("detail", ""))

    def test_endpoint_serves_original_directly_when_already_browser_safe(self):
        # Use a real project dir/media so _stream()'s path.exists() succeeds.
        project = store.new_project("preview-real-safe")
        pdir = store.project_dir(project["id"])
        media_dir = pdir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        real_file = media_dir / "safe.mp4"
        if _HAVE_FFMPEG:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=blue:s=320x180:d=0.5:r=25", str(real_file)],
                check=True,
            )
        else:
            real_file.write_bytes(b"stand-in bytes (no ffmpeg on PATH for this test run)")
        clip = {
            "id": "c7", "path": str(real_file), "filename": "safe.mp4", "role": "camera",
            "camera_group": "main", "is_main": False, "wav": None, "transcript": None,
            "language": None, "source_path": str(real_file),
            "info": {"has_video": True, "codec_name": "h264", "pix_fmt": "yuv420p", "height": 180},
        }
        project["clips"].append(clip)
        store.save(project)

        client = TestClient(server_mod.app)
        resp = client.get(f"/api/projects/{project['id']}/media/preview/{clip['id']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.content, real_file.read_bytes())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
