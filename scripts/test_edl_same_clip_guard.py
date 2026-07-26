#!/usr/bin/env python3
"""Regression test for Workstream F -- the manual-edit chronology guardrail
(spec point 2 gap): PUT /api/projects/{pid}/edl and the frontend timeline
drag can reorder segments WITHIN one original clip; that must be rejected.
Only whole-clip block moves (the clip's segments relocated elsewhere in the
EDL, still internally chronological) are legal -- the SACRED INVARIANT is
that within one original clip, segment order is chronological and immutable.

Covers the durable fix in magic_video_editor/api/edl.py's _validate_segments:
after the existing per-segment range/duration checks, segments are grouped
by clip_id (preserving list order) and each group's `start` values must be
non-decreasing in appearance order, else HTTPException(400).

  (a) A valid chronological PUT (segments in their natural order) passes.
  (b) A whole contiguous block of one clip's segments moved elsewhere in the
      EDL (still internally increasing) passes -- this is the legal case the
      guard must NOT reject.
  (c) Two segments from the SAME clip_id appearing out of chronological
      order (interleaved with another clip's segments) is rejected with 400
      and a clear message about whole-clip-only reordering.
  (d) edl_split (unaffected by this change) still works: splitting a segment
      never produces same-clip segments out of order.

No pytest in this project's dependency set -- stdlib unittest + FastAPI's
TestClient, same spirit as scripts/test_edl_resilience.py. MVE_DATA must be
set (to a fresh tmp dir) BEFORE importing anything from magic_video_editor,
since magic_video_editor.config reads it at import time.

Usage:
    uv run python scripts/test_edl_same_clip_guard.py
    uv run python scripts/test_edl_same_clip_guard.py -v
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

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_edl_same_clip_guard_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _seg(clip_id: str, start: float, end: float, text: str = "") -> dict:
    return {
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "text": text,
        "transition": {"type": "none", "duration": 0.5},
        "paragraph_break": False,
    }


def _make_project(n_clips: int = 2, duration: float = 100.0) -> dict:
    project = store.new_project("edl-same-clip-guard-repro")
    project["clips"] = [
        {
            "id": f"clip{i}",
            "path": f"/fake/clip{i}.mp4",
            "source_path": f"/fake/clip{i}.mp4",
            "filename": f"clip{i}.mp4",
            "role": "camera",
            "camera_group": "main",
            "is_main": i == 0,
            "info": {"duration": duration, "has_audio": True, "has_video": True},
            "wav": None,
            "transcript": None,
            "language": None,
        }
        for i in range(n_clips)
    ]
    project["sentences"] = []
    store.save(project)
    return project


class EdlSameClipChronologyGuard(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_a_valid_chronological_put_passes(self):
        project = _make_project()
        segments = [
            _seg("clip0", 0.0, 1.0),
            _seg("clip0", 1.0, 2.0),
            _seg("clip1", 0.0, 1.0),
            _seg("clip1", 1.0, 2.0),
        ]
        resp = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": segments}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["segments"]), 4)

    def test_b_whole_block_move_passes(self):
        # clip1's whole (still-internally-increasing) block relocated ahead
        # of clip0's block -- a legal whole-clip move.
        project = _make_project()
        segments = [
            _seg("clip1", 0.0, 1.0),
            _seg("clip1", 1.0, 2.0),
            _seg("clip0", 0.0, 1.0),
            _seg("clip0", 1.0, 2.0),
        ]
        resp = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": segments}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_c_interleaved_out_of_order_same_clip_rejected(self):
        # clip0's two segments are interleaved with clip1's segment, and
        # clip0's own segments are out of chronological order relative to
        # each other (second start < first start) -- must be rejected.
        project = _make_project()
        segments = [
            _seg("clip0", 5.0, 6.0),
            _seg("clip1", 0.0, 1.0),
            _seg("clip0", 1.0, 2.0),  # earlier start, appears after -- violation
        ]
        resp = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": segments}
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("chronological", resp.json()["detail"])
        self.assertIn("clip0", resp.json()["detail"])

    def test_c_reversed_same_clip_pair_rejected_even_when_adjacent(self):
        # Even without interleaving, two segments of the same clip must
        # never be reversed relative to each other.
        project = _make_project()
        segments = [
            _seg("clip0", 5.0, 6.0),
            _seg("clip0", 1.0, 2.0),
        ]
        resp = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": segments}
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_e_two_block_trim_crossing_rejected_clamped_accepted(self):
        # Regression for the state.js trim() same-clip guard gap: clip0
        # appears in TWO separate blocks (a legal whole-block-move layout,
        # per test_b). Trimming the first block's start so it crosses the
        # second block's start (a same-clip sibling) must be rejected by the
        # backend -- this is exactly the payload state.js's old trim(), which
        # only clamped a segment against its OWN end/clip duration, could
        # produce and send with no rollback. state.js's trim() now clamps
        # client-side to the sibling's start instead (see ui/editor/state.js
        # trim()); this test pins the server-side contract that fix relies
        # on: the clamped-at-bound value is accepted, anything past it isn't.
        project = _make_project()
        segments = [
            _seg("clip0", 0.0, 1.0),
            _seg("clip1", 0.0, 1.0),
            _seg("clip0", 2.0, 3.0),
        ]

        # Uncrossed: first block's start trimmed right up to (but not past)
        # the sibling second block's start -- the tightest legal bound
        # state.js's binary-search clamp converges on. Must be accepted.
        clamped = [
            _seg("clip0", 2.0, 2.0 + 0.05),  # clamped to sibling's start (2.0)
            _seg("clip1", 0.0, 1.0),
            _seg("clip0", 2.0, 3.0),
        ]
        resp_clamped = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": clamped}
        )
        self.assertEqual(resp_clamped.status_code, 200, resp_clamped.text)

        # Crossed: first block's start trimmed PAST the sibling's start --
        # exactly what the unguarded client-side trim() could send. Must
        # still be rejected by the backend even if a client forgot to clamp.
        crossed = [
            _seg("clip0", 2.5, 3.0),  # 2.5 > second block's start (2.0)
            _seg("clip1", 0.0, 1.0),
            _seg("clip0", 2.0, 2.5),
        ]
        resp_crossed = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": crossed}
        )
        self.assertEqual(resp_crossed.status_code, 400, resp_crossed.text)
        self.assertIn("chronological", resp_crossed.json()["detail"])

    def test_d_split_never_produces_out_of_order_same_clip_segments(self):
        project = _make_project()
        segments = [_seg("clip0", 0.0, 4.0), _seg("clip1", 0.0, 1.0)]
        project["edl"] = segments
        store.save(project)

        resp = self.client.post(
            f"/api/projects/{project['id']}/edl/split",
            json={"index": 0, "at": 2.0},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        new_segments = resp.json()["segments"]
        clip0_starts = [s["start"] for s in new_segments if s["clip_id"] == "clip0"]
        self.assertEqual(clip0_starts, sorted(clip0_starts))

        # And a follow-up PUT with that exact (still-valid) split result
        # must still be accepted.
        resp2 = self.client.put(
            f"/api/projects/{project['id']}/edl", json={"segments": new_segments}
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
