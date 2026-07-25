#!/usr/bin/env python3
"""Regression test for the "no puedo visualizar nada después de que el
pipeline acaba" bug, diagnosed live on project a42ba5d48ba7: the project had
6 real camera clips (85 kept sentences total) but project["clip_order"] was
a single PHANTOM id ("62e6cae7") matching none of them -- a stale leftover
from an earlier project shape. ordering.build_edl iterated that bogus order,
found no matching sentences for it, and returned [] even though 85 kept
sentences existed -- "No segments yet" in the editor after a fully "done"
pipeline.

Covers the durable fix in magic_video_editor/pipeline/ordering.py
(reconcile_clip_order / build_edl) and the clip-set-change invalidation hook
(invalidate_after_clipset_change, wired into pipeline/ingest.py's
add_clips/register_uploaded_clips and api/projects.py's clip_remove):

  (a) build_edl with a stale/phantom clip_order + kept sentences across N
      real clips returns segments for ALL clips-with-kept, not [] -- this is
      the exact reported bug, reproduced with a synthetic project mirroring
      a42ba5d48ba7's shape (6 clips, clip_order=["phantom"], 85 kept
      sentences across them).
  (b) build_edl appends a clip that has kept sentences but is missing from
      clip_order (e.g. a clip added after clip_order was last computed).
  (c) Adding a clip (via ingest.add_clips) and removing a clip (via
      DELETE /api/projects/{pid}/clips/{cid}) both clear the cached edl,
      drop clip_order ids that no longer exist, and un-done the
      order/render/reels stage badges.

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor) and never touches
ollama/ffmpeg: ingest.add_clips/register_uploaded_clips never invoke ffmpeg
themselves (only the "ingest" pipeline STAGE, ingest.run(), does that, and
this test never calls it), and clip-set changes here are exercised with
tiny placeholder files, not real media.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_reel_previews.py. MVE_DATA must be set (to a fresh tmp dir)
BEFORE importing anything from magic_video_editor, since magic_video_editor.config
reads it at import time.

Usage:
    uv run python scripts/test_edl_resilience.py
    uv run python scripts/test_edl_resilience.py -v
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

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_edl_resilience_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.pipeline import ingest, ordering  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _make_camera_clip(cid: str, filename: str, duration: float = 30.0) -> dict:
    """A minimal camera clip record -- shape matches ingest._new_clip_record
    plus a fake "info" (as if ingest.run()'s probe stage had already run,
    which build_edl needs for its clamp-to-duration step)."""
    return {
        "id": cid,
        "path": f"/fake/{filename}",
        "source_path": f"/fake/{filename}",
        "filename": filename,
        "role": "camera",
        "camera_group": "main",
        "is_main": cid == "84d8c23c",
        "info": {"duration": duration, "has_audio": True, "has_video": True},
        "wav": None,
        "transcript": None,
        "language": None,
    }


def _make_sentences(clip_id: str, n_kept: int, start_at: float = 1.0) -> list[dict]:
    """`n_kept` kept sentences for `clip_id`, spaced far enough apart (well
    beyond config.MERGE_GAP) that build_edl won't merge them into one
    segment -- makes the "segments for ALL clips" assertion unambiguous."""
    out = []
    t = start_at
    for i in range(n_kept):
        out.append(
            {
                "id": f"{clip_id}-s{i}",
                "clip_id": clip_id,
                "start": t,
                "end": t + 1.0,
                "text": f"sentence {i} of {clip_id}",
                "kept": True,
                "reason": "",
            }
        )
        t += 5.0
    return out


# The exact live shape: 6 real camera clips, kept-sentence counts
# 2/1/33/40/8/1 = 85 kept total, clip_order = a single phantom id.
_REAL_CLIP_IDS = ["84d8c23c", "d6fbaad5", "84056dcf", "d1d029f4", "54b99e3a", "b8a37310"]
_KEPT_COUNTS = [2, 1, 33, 40, 8, 1]
assert sum(_KEPT_COUNTS) == 85


def _build_live_shaped_project() -> dict:
    project = store.new_project("a42ba5d48ba7-repro")
    project["clips"] = [
        _make_camera_clip(cid, f"{cid}.mp4") for cid in _REAL_CLIP_IDS
    ]
    sentences = []
    for cid, n in zip(_REAL_CLIP_IDS, _KEPT_COUNTS, strict=True):
        sentences.extend(_make_sentences(cid, n))
    project["sentences"] = sentences
    project["clip_order"] = ["62e6cae7"]  # the phantom id -- matches nothing
    project["order_notes"] = "single clip — chronological"
    store.save(project)
    return project


class BuildEdlResilience(unittest.TestCase):
    def test_a_stale_phantom_clip_order_does_not_empty_the_edl(self):
        project = _build_live_shaped_project()

        segments = ordering.build_edl(project)

        self.assertNotEqual(segments, [], "build_edl returned [] despite 85 kept sentences")
        clips_covered = {seg["clip_id"] for seg in segments}
        self.assertEqual(
            clips_covered,
            set(_REAL_CLIP_IDS),
            "build_edl must produce segments for every clip with kept sentences, "
            f"got only {clips_covered}",
        )
        # Sanity: the phantom id must never appear in the reconciled order.
        self.assertNotIn("62e6cae7", {seg["clip_id"] for seg in segments})

    def test_a_reconcile_clip_order_drops_phantom_and_keeps_valid(self):
        project = _build_live_shaped_project()

        order = ordering.reconcile_clip_order(project)

        self.assertNotIn("62e6cae7", order)
        self.assertEqual(set(order), set(_REAL_CLIP_IDS))

    def test_b_build_edl_appends_clip_missing_from_clip_order(self):
        project = _build_live_shaped_project()
        # clip_order only mentions the first two real clips -- as if it was
        # computed before the other four were (re-)imported.
        project["clip_order"] = _REAL_CLIP_IDS[:2]
        store.save(project)

        segments = ordering.build_edl(project)

        clips_covered = {seg["clip_id"] for seg in segments}
        self.assertEqual(
            clips_covered,
            set(_REAL_CLIP_IDS),
            "build_edl must append clips-with-kept that are missing from clip_order",
        )
        # The explicitly-ordered clips must still come first, in their given
        # order, ahead of the appended ones.
        order_of_first_appearance = []
        for seg in segments:
            if seg["clip_id"] not in order_of_first_appearance:
                order_of_first_appearance.append(seg["clip_id"])
        self.assertEqual(order_of_first_appearance[:2], _REAL_CLIP_IDS[:2])

    def test_b_build_edl_empty_clip_order_falls_back_to_all_kept(self):
        project = _build_live_shaped_project()
        project["clip_order"] = []
        store.save(project)

        segments = ordering.build_edl(project)

        self.assertEqual({seg["clip_id"] for seg in segments}, set(_REAL_CLIP_IDS))


class ClipSetChangeInvalidation(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _project_with_done_stages(self) -> dict:
        project = _build_live_shaped_project()
        project["edl"] = ordering.build_edl(project)  # simulate a cached edl
        project["stages"] = {
            "order": {"status": "done", "at": "00:00:00", "detail": ""},
            "render": {"status": "done", "at": "00:00:00", "detail": ""},
            "reels": {"status": "done", "at": "00:00:00", "detail": ""},
            # transcribe/takes must survive -- they're keyed per-clip.
            "transcribe": {"status": "done", "at": "00:00:00", "detail": ""},
            "takes": {"status": "done", "at": "00:00:00", "detail": ""},
        }
        store.save(project)
        return project

    def test_c_adding_a_clip_invalidates_stale_state(self):
        project = self._project_with_done_stages()
        # A placeholder file with a recognized media extension -- add_clips
        # never invokes ffmpeg itself (only the "ingest" pipeline stage
        # does), so this never spawns real ffmpeg.
        src_dir = Path(tempfile.mkdtemp(prefix="mve_edl_resilience_src_"))
        try:
            new_file = src_dir / "new_clip.mp4"
            new_file.write_bytes(b"not a real video")

            ingest.add_clips(project, [str(new_file)])

            reloaded = store.load(project["id"])
            self.assertIsNone(reloaded.get("edl"), "edl must be cleared after a clip is added")
            self.assertNotIn(
                "order", reloaded.get("stages", {}),
                "order stage badge must be un-done after the clip set changes",
            )
            self.assertNotIn(
                "render", reloaded.get("stages", {}),
                "render stage badge must be un-done after the clip set changes",
            )
            self.assertNotIn(
                "reels", reloaded.get("stages", {}),
                "reels stage badge must be un-done after the clip set changes",
            )
            # transcribe/takes are keyed per-clip and must NOT be reset.
            self.assertEqual(reloaded["stages"]["transcribe"]["status"], "done")
            self.assertEqual(reloaded["stages"]["takes"]["status"], "done")
        finally:
            shutil.rmtree(src_dir, ignore_errors=True)

    def test_c_removing_a_clip_drops_it_from_clip_order_and_invalidates(self):
        project = self._project_with_done_stages()
        removed_id = _REAL_CLIP_IDS[2]  # the clip with 33 kept sentences

        resp = self.client.delete(f"/api/projects/{project['id']}/clips/{removed_id}")
        self.assertEqual(resp.status_code, 200, resp.text)

        reloaded = store.load(project["id"])
        self.assertIsNone(reloaded.get("edl"), "edl must be cleared after a clip is removed")
        self.assertNotIn(removed_id, reloaded.get("clip_order") or [])
        self.assertNotIn("order", reloaded.get("stages", {}))
        self.assertNotIn("render", reloaded.get("stages", {}))
        self.assertNotIn("reels", reloaded.get("stages", {}))
        self.assertEqual(reloaded["stages"]["transcribe"]["status"], "done")
        self.assertEqual(reloaded["stages"]["takes"]["status"], "done")
        # Rebuilding the EDL afterwards must still cover the remaining clips
        # (no crash, no phantom reference to the removed clip).
        segments = ordering.build_edl(reloaded)
        self.assertNotIn(removed_id, {seg["clip_id"] for seg in segments})
        self.assertEqual(
            {seg["clip_id"] for seg in segments}, set(_REAL_CLIP_IDS) - {removed_id}
        )


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
