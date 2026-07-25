#!/usr/bin/env python3
"""Regression test for the "incremental analyze after completed pipeline is
dead" bug (owner-confirmed, pre-existing at HEAD before this fix).

Root cause: pipeline/ingest.py's add_clips() and register_uploaded_clips()
both did, in this order:

    _finalize_main_group(project, added)
    if added:
        ordering.invalidate_after_clipset_change(project)
    store.save(project)
    _enqueue_analyze_for_new_clips(project, added)

ordering.invalidate_after_clipset_change() un-marks the order/render/reels
stage badges (deletes project["stages"][s] for any s in that set whose
status was "done") the instant the clip set changes -- by design, see that
function's docstring. But _enqueue_analyze_for_new_clips used to call
_completed_pipeline(project) AFTER that invalidation, on the SAME in-memory
project dict -- so it always saw a just-invalidated (never "done") project
and could never take the v7.3 "pipeline already completed" branch, even
when the project genuinely had a prior completed render/order pass. Net
effect: every new camera clip added to an already-completed project got
silently downgraded to the lightweight first-batch `make_proxy:<id>` job
instead of the full `analyze_clip:<id>` job (transcribe + placement
suggestion) -- the entire v7.3 "Incremental clip addition" feature
(docs/PLATFORM-SPEC.md v7.3) was dead when triggered through add_clips /
register_uploaded_clips.

The fix: both callers now compute `_completed_pipeline(project)` BEFORE
calling ordering.invalidate_after_clipset_change(), and thread that boolean
straight into _enqueue_analyze_for_new_clips(project, added,
pipeline_was_completed), which uses the passed-in flag instead of
re-deriving it post-invalidation. The invalidate/save call order and the
stage-badge un-marking behavior are untouched.

This test drives the REAL public entrypoints (add_clips, register_uploaded_
clips) end-to-end -- never the private _enqueue_analyze_for_new_clips
function directly (that was the flagged workaround: calling the branch-
selection helper in isolation can't prove the bug is fixed, since the bug
was specifically about what add_clips/register_uploaded_clips pass into it).

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor). Uses real, tiny,
lavfi-synthesized ffmpeg clips. Skips itself (rather than failing) if
ffmpeg/ffprobe aren't on PATH. Every wait is bounded.

Usage:
    uv run python scripts/test_incremental_analyze.py
    uv run python scripts/test_incremental_analyze.py -v
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

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_incremental_analyze_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from magic_video_editor import config, ffmpeg_utils, store  # noqa: E402
from magic_video_editor.pipeline import ingest  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

_HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")

if _HAVE_FFMPEG:
    try:
        ffmpeg_utils.ffmpeg_bin()
        ffmpeg_utils.ffprobe_bin()
    except Exception:
        pass

_MEDIA_DIR = _SCRATCH / "src_media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _make_synthetic_clip(name: str, color: str, duration: float = 1.0) -> Path:
    """A tiny, real, decodable h264/yuv420p/720p mp4 (already browser-safe,
    so it never also queues a make_proxy job and muddies the assertions
    here -- that path is covered by scripts/test_manual_proxy.py)."""
    path = _MEDIA_DIR / name
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=640x360:d={duration}:r=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
    )
    return path


def _new_completed_project(name: str) -> dict:
    """A project with one real ingested clip and its order/render stages
    genuinely marked "done" -- i.e. _completed_pipeline(project) is True,
    the precondition the v7.3 incremental-analyze path requires."""
    clip_path = _make_synthetic_clip(f"{name}_seed.mp4", "blue")
    project = store.new_project(name)
    seed = ingest._new_clip_record(clip_path, clip_path, "main")
    project["clips"].append(seed)
    ingest.set_main_group(project, "main")
    project.setdefault("stages", {})
    project["stages"]["order"] = {"status": "done"}
    project["stages"]["render"] = {"status": "done"}
    store.save(project)
    assert ingest._completed_pipeline(project), (
        "precondition: project must read as pipeline-completed"
    )
    return project


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class IncrementalAnalyzeAfterCompletedPipeline(unittest.TestCase):
    """The core fix: adding a clip to an already-completed project must
    enqueue the full analyze_clip:<id> job (transcribe + placement), not the
    first-batch make_proxy:<id> job -- via the REAL add_clips /
    register_uploaded_clips entrypoints, exercising the actual pre-
    invalidation capture, not the private helper directly."""

    def test_add_clips_enqueues_analyze_clip_after_completion(self):
        project = _new_completed_project("incr-add-clips")
        new_clip_path = _make_synthetic_clip("new_camera_clip.mp4", "green")

        added = ingest.add_clips(project, [str(new_clip_path)])
        self.assertEqual(len(added), 1)
        new_clip = added[0]

        reloaded = store.load(project["id"])
        kinds = {i["kind"] for i in reloaded.get("queue", [])}
        self.assertIn(
            f"analyze_clip:{new_clip['id']}",
            kinds,
            f"expected analyze_clip job for the new clip on an already-completed "
            f"project, got queue kinds={kinds}",
        )
        self.assertNotIn(
            f"make_proxy:{new_clip['id']}",
            kinds,
            "an already-completed project must not fall back to the first-batch "
            "make_proxy path for a browser-safe clip",
        )

    def test_register_uploaded_clips_enqueues_analyze_clip_after_completion(self):
        project = _new_completed_project("incr-register-uploaded")
        media_dir = store.project_dir(project["id"]) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        uploaded_src = _make_synthetic_clip("uploaded_camera_clip.mp4", "yellow")
        dest = media_dir / uploaded_src.name
        shutil.copy2(uploaded_src, dest)

        added = ingest.register_uploaded_clips(project, [(dest, "main")])
        self.assertEqual(len(added), 1)
        new_clip = added[0]

        reloaded = store.load(project["id"])
        kinds = {i["kind"] for i in reloaded.get("queue", [])}
        self.assertIn(
            f"analyze_clip:{new_clip['id']}",
            kinds,
            f"expected analyze_clip job via register_uploaded_clips on an "
            f"already-completed project, got queue kinds={kinds}",
        )
        self.assertNotIn(f"make_proxy:{new_clip['id']}", kinds)

    def test_stage_badges_still_uninvalidated_after_clipset_change(self):
        """The stage-badge un-marking behavior must be completely unchanged
        by this fix -- only the DECISION input to _enqueue_analyze_for_new_
        clips is pre-invalidation; the actual invalidate/save call order is
        untouched."""
        project = _new_completed_project("incr-badges-preserved")
        new_clip_path = _make_synthetic_clip("badge_check_clip.mp4", "purple")

        ingest.add_clips(project, [str(new_clip_path)])

        reloaded = store.load(project["id"])
        stages = reloaded.get("stages", {})
        self.assertNotIn(
            "order", stages, "order stage badge must be un-marked after a clipset change"
        )
        self.assertNotIn(
            "render", stages, "render stage badge must be un-marked after a clipset change"
        )

    def test_not_completed_project_still_gets_make_proxy_for_first_batch(self):
        """Non-regression: a project whose pipeline never completed (no
        order/render stage marked done) must still take the first-batch
        make_proxy path -- not analyze_clip -- exactly as the recent
        manual-editing fix (scripts/test_manual_proxy.py) established."""
        project = store.new_project("incr-not-completed")
        self.assertFalse(ingest._completed_pipeline(project))
        # A clip whose height flags _proxy_needed so a make_proxy job is
        # actually queued (a same-resolution browser-safe clip queues nothing).
        clip_path = _make_synthetic_clip_tall("tall_first_batch.mp4")

        added = ingest.add_clips(project, [str(clip_path)])
        self.assertEqual(len(added), 1)
        clip = added[0]

        reloaded = store.load(project["id"])
        kinds = {i["kind"] for i in reloaded.get("queue", [])}
        self.assertIn(f"make_proxy:{clip['id']}", kinds)
        self.assertNotIn(f"analyze_clip:{clip['id']}", kinds)


def _make_synthetic_clip_tall(name: str, duration: float = 1.0) -> Path:
    path = _MEDIA_DIR / name
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=red:s=640x1088:d={duration}:r=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
    )
    return path


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
