#!/usr/bin/env python3
"""Regression/contract test for the owner-confirmed ordering invariant
(2026-07-25): WITHIN a single input clip, cut order is CAUSAL/CHRONOLOGICAL
and is NEVER reordered. The take/cleanup agents (transcript_cleaner,
take_sequencer, context_check, dedup_judge, ...) only ever flip a sentence's
`kept` flag -- they never rewrite `start`/`end` or shuffle sentence order --
and pipeline/ordering.py's build_edl re-sorts each clip's kept sentences by
`start` before grouping them into segments (see the `sorted(..., key=lambda
s: s["start"])` in build_edl). Reordering is allowed ONLY at the
between-clips level via `clip_order` (the clip_order LLM stage / manual
drag-reorder in the UI); the in-clip timeline itself must never move.

This test builds project dicts entirely in memory (no ffmpeg, no ollama, no
LLM calls -- build_edl is pure) against a scratch MVE_DATA dir, and would
FAIL if a future change ever caused build_edl (or anything upstream of it)
to emit a clip's segments out of source-time order.

Covers, over a synthetic 3-clip project with mixed kept/cut sentences:
  (a) build_edl's output segments are strictly increasing in `start` WITHIN
      each clip_id, even when project["sentences"] is fed in shuffled order
      (proves build_edl sorts -- it does not trust input order).
  (b) segment timestamps are never invented: every segment boundary is
      traceable to an original sentence's start/end, only shifted by
      config.SEGMENT_PAD (and clamped at 0 / clip duration).
  (c) clip_order CAN reorder whole clips (segments from a later-recorded
      clip can appear before an earlier one) while each clip's OWN segments
      stay chronological -- "reorder between files, causal within a file".
  (d) a hypothetical agent that flipped `kept` flags in a chaotic order
      (unrelated to timestamps) still yields chronological per-clip
      segments -- the invariant survives adversarial kept-flag ordering,
      not just adversarial list ordering.

Usage:
    uv run python scripts/test_intra_clip_order.py
    uv run python scripts/test_intra_clip_order.py -v
"""

from __future__ import annotations

import copy
import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_intra_clip_order_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.api import edl as edl_api  # noqa: E402
from magic_video_editor.pipeline import judge, ordering  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _camera_clip(cid: str, filename: str, duration: float = 200.0) -> dict:
    return {
        "id": cid,
        "path": f"/fake/{filename}",
        "source_path": f"/fake/{filename}",
        "filename": filename,
        "role": "camera",
        "camera_group": "main",
        "is_main": False,
        "info": {"duration": duration, "has_audio": True, "has_video": True},
        "wav": None,
        "transcript": None,
        "language": None,
    }


def _sentence(sid: str, clip_id: str, start: float, end: float, kept: bool) -> dict:
    return {
        "id": sid,
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "text": f"{sid} @ {start}-{end}",
        "kept": kept,
        "reason": "" if kept else "test cut",
    }


# Three clips, each with sentences deliberately spaced so several fall within
# config.MERGE_GAP of each other (forcing a merge into one segment) and
# several fall far apart (forcing separate segments) -- this exercises the
# per-clip sort across MULTIPLE segments, not just a single one.
CLIP_A, CLIP_B, CLIP_C = "clip-A", "clip-B", "clip-C"

_RAW_SENTENCES = [
    # clip A: two isolated sentences + a cut one in between that must never surface
    _sentence("a1", CLIP_A, 1.0, 2.0, True),
    _sentence("a-cut", CLIP_A, 5.0, 6.0, False),
    _sentence("a2", CLIP_A, 10.0, 11.0, True),
    _sentence("a3", CLIP_A, 30.0, 31.0, True),
    # clip B: two sentences close enough to merge (gap 1.0s <= MERGE_GAP) + one far away
    _sentence("b1", CLIP_B, 0.5, 1.0, True),
    _sentence("b2", CLIP_B, 2.0, 2.5, True),
    _sentence("b-cut", CLIP_B, 15.0, 16.0, False),
    _sentence("b3", CLIP_B, 40.0, 41.0, True),
    # clip C: three isolated sentences, one near t=0 (exercises the clamp-to-0 pad)
    _sentence("c1", CLIP_C, 0.05, 0.4, True),
    _sentence("c2", CLIP_C, 20.0, 21.0, True),
    _sentence("c-cut", CLIP_C, 25.0, 26.0, False),
    _sentence("c3", CLIP_C, 60.0, 61.0, True),
]


def _build_project(sentence_order: list[dict], clip_order: list[str]) -> dict:
    project = store.new_project("intra-clip-order-test")
    project["clips"] = [
        _camera_clip(CLIP_A, "a.mp4"),
        _camera_clip(CLIP_B, "b.mp4"),
        _camera_clip(CLIP_C, "c.mp4"),
    ]
    project["sentences"] = sentence_order
    project["clip_order"] = clip_order
    # Live-verification fix (2026-07-26): judge.run()'s concurrency-safe save
    # (pipeline/judge.py's _save_judge_deltas) reloads the project FROM DISK
    # at the end and reapplies only its own deltas onto that fresh copy (see
    # that module's docstring, "Concurrency") -- by design, so a concurrent
    # HTTP write during judge's long run never gets silently clobbered. That
    # means any test that calls judge.run() against a project built here MUST
    # have this mutated state actually persisted first, or judge reloads the
    # stale, pre-mutation new_project() skeleton (empty clips/sentences/
    # clip_order) and overwrites the in-memory project with THAT -- exactly
    # what caused JudgeAutocutOnlyFlipsKeptTests to see an emptied
    # project["sentences"] after judge.run() until this fix.
    store.save(project)
    return project


def _segments_by_clip(segments: list[dict]) -> dict[str, list[dict]]:
    by_clip: dict[str, list[dict]] = {}
    for seg in segments:
        by_clip.setdefault(seg["clip_id"], []).append(seg)
    return by_clip


def _first_appearance_order(segments: list[dict]) -> list[str]:
    seen: list[str] = []
    for seg in segments:
        if seg["clip_id"] not in seen:
            seen.append(seg["clip_id"])
    return seen


class IntraClipChronologyIsNeverReordered(unittest.TestCase):
    def test_a_segments_strictly_increasing_within_each_clip_despite_shuffled_input(self):
        shuffled = list(_RAW_SENTENCES)
        rng = random.Random(1234)
        rng.shuffle(shuffled)
        # Sanity: prove the shuffle actually changed the order (otherwise
        # the test wouldn't be exercising the "don't trust input order"
        # claim at all).
        self.assertNotEqual([s["id"] for s in shuffled], [s["id"] for s in _RAW_SENTENCES])

        project = _build_project(shuffled, [CLIP_A, CLIP_B, CLIP_C])
        segments = ordering.build_edl(project)

        self.assertTrue(segments, "build_edl produced no segments")
        by_clip = _segments_by_clip(segments)
        self.assertEqual(set(by_clip), {CLIP_A, CLIP_B, CLIP_C})
        for clip_id, segs in by_clip.items():
            starts = [s["start"] for s in segs]
            self.assertEqual(
                starts,
                sorted(starts),
                f"{clip_id}: segments not chronological: {starts}",
            )
            self.assertEqual(
                len(starts),
                len(set(starts)),
                f"{clip_id}: duplicate segment start {starts}",
            )
            # Strictly increasing (no ties, no regressions) between
            # consecutive segments of the SAME clip.
            for prev, nxt in zip(starts, starts[1:], strict=False):
                self.assertLess(prev, nxt, f"{clip_id}: segment starts not strictly increasing")

    def test_b_segment_timestamps_are_never_invented(self):
        """Every segment boundary must be traceable back to an original kept
        sentence's start/end (shifted by SEGMENT_PAD, clamped at 0 / clip
        duration) -- never a value build_edl made up."""
        shuffled = list(_RAW_SENTENCES)
        random.Random(99).shuffle(shuffled)
        project = _build_project(shuffled, [CLIP_A, CLIP_B, CLIP_C])
        segments = ordering.build_edl(project)

        kept_starts = {s["start"] for s in _RAW_SENTENCES if s["kept"]}
        kept_ends = {s["end"] for s in _RAW_SENTENCES if s["kept"]}
        cut_ids = {s["id"] for s in _RAW_SENTENCES if not s["kept"]}
        cut_windows = [
            (s["start"], s["end"]) for s in _RAW_SENTENCES if s["id"] in cut_ids
        ]

        clip_by_id = {c["id"]: c for c in project["clips"]}
        for seg in segments:
            duration = clip_by_id[seg["clip_id"]]["info"]["duration"]
            unpadded_start = seg["start"] + config.SEGMENT_PAD
            unpadded_end = seg["end"] - config.SEGMENT_PAD
            start_ok = seg["start"] == 0.0 or any(
                abs(unpadded_start - s) < 1e-6 for s in kept_starts
            )
            end_ok = seg["end"] == duration or any(
                abs(unpadded_end - e) < 1e-6 for e in kept_ends
            )
            self.assertTrue(
                start_ok, f"segment start {seg['start']} not traceable to a kept sentence"
            )
            self.assertTrue(
                end_ok, f"segment end {seg['end']} not traceable to a kept sentence"
            )
            # A cut sentence's window must never be (even partially) the
            # sole source of a segment's span -- i.e. no segment exactly
            # reproduces a cut sentence's own start/end.
            for cut_start, cut_end in cut_windows:
                self.assertFalse(
                    abs(unpadded_start - cut_start) < 1e-6 and abs(unpadded_end - cut_end) < 1e-6,
                    "segment reproduces a CUT sentence's window",
                )

    def test_c_clip_order_reorders_between_clips_but_not_within(self):
        """clip_order = [B, A, C] must move clip B's segments before clip
        A's entirely, while each clip's own segments remain chronological."""
        project = _build_project(list(_RAW_SENTENCES), [CLIP_B, CLIP_A, CLIP_C])
        segments = ordering.build_edl(project)

        self.assertEqual(_first_appearance_order(segments), [CLIP_B, CLIP_A, CLIP_C])

        by_clip = _segments_by_clip(segments)
        for clip_id, segs in by_clip.items():
            starts = [s["start"] for s in segs]
            self.assertEqual(starts, sorted(starts), f"{clip_id} not chronological after reorder")

        # Cross-clip proof: clip B's LAST segment start is far later in
        # source time than clip A's FIRST segment start, yet B's block
        # still comes first in the output -- between-clip reordering is
        # independent of each clip's internal chronology.
        b_starts = [s["start"] for s in by_clip[CLIP_B]]
        a_starts = [s["start"] for s in by_clip[CLIP_A]]
        self.assertGreater(max(b_starts), min(a_starts))
        b_index = next(i for i, s in enumerate(segments) if s["clip_id"] == CLIP_B)
        a_index = next(i for i, s in enumerate(segments) if s["clip_id"] == CLIP_A)
        self.assertLess(b_index, a_index)

    def test_d_chaotic_kept_flag_order_still_yields_chronological_segments(self):
        """Simulate a hypothetical buggy/adversarial agent pass that flips
        `kept` flags in an order unrelated to timestamps (here: it revisits
        sentences in reverse-id order and re-writes `kept=True` on ones
        already kept). This must never perturb build_edl's per-clip
        chronology, because build_edl re-sorts by `start` regardless of any
        upstream flag-mutation order."""
        chaotic = list(_RAW_SENTENCES)
        for s in sorted(chaotic, key=lambda s: s["id"], reverse=True):
            if s["kept"]:
                s["kept"] = True  # no-op rewrite, exercises "touched out of order"
        random.Random(7).shuffle(chaotic)

        project = _build_project(chaotic, [CLIP_A, CLIP_B, CLIP_C])
        segments = ordering.build_edl(project)

        by_clip = _segments_by_clip(segments)
        for clip_id, segs in by_clip.items():
            starts = [s["start"] for s in segs]
            self.assertEqual(
                starts, sorted(starts), f"{clip_id}: chaotic kept-order broke chronology"
            )

    def test_e_cut_sentences_never_appear(self):
        project = _build_project(list(_RAW_SENTENCES), [CLIP_A, CLIP_B, CLIP_C])
        segments = ordering.build_edl(project)
        all_text = " ".join(seg["text"] for seg in segments)
        for cut_id in ("a-cut", "b-cut", "c-cut"):
            self.assertNotIn(cut_id, all_text, f"cut sentence {cut_id} leaked into the EDL")


class EdlSameClipGuardTests(unittest.TestCase):
    """api/edl.py's `_validate_segments` chronology guardrail: a manual PUT
    /edl may move a whole clip's block of segments elsewhere in the
    timeline, but the segments belonging to any ONE clip_id must never come
    out of chronological (start-ascending) order relative to each other."""

    def _project(self) -> dict:
        return _build_project(list(_RAW_SENTENCES), [CLIP_A, CLIP_B, CLIP_C])

    def _seg(self, clip_id: str, start: float, end: float) -> edl_api.EdlSegment:
        return edl_api.EdlSegment(clip_id=clip_id, start=start, end=end, text="")

    def test_whole_clip_block_reorder_is_allowed(self):
        """Moving clip B's entire (still-chronological) segment block ahead
        of clip A's is a legitimate between-clips reorder -- must not raise."""
        project = self._project()
        segments = [
            self._seg(CLIP_B, 0.5, 1.0),
            self._seg(CLIP_B, 2.0, 2.5),
            self._seg(CLIP_A, 1.0, 2.0),
            self._seg(CLIP_A, 10.0, 11.0),
        ]
        edl_api._validate_segments(project, segments)  # must not raise

    def test_within_clip_reorder_is_rejected(self):
        """Two segments of the SAME clip_id submitted out of start order
        (the sacred invariant) must be rejected with a 400, even though each
        segment individually is a valid, in-bounds range."""
        project = self._project()
        segments = [
            self._seg(CLIP_A, 10.0, 11.0),  # a2 (start=10) BEFORE a1 (start=1) -- illegal
            self._seg(CLIP_A, 1.0, 2.0),
        ]
        with self.assertRaises(HTTPException) as ctx:
            edl_api._validate_segments(project, segments)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("out of chronological order", ctx.exception.detail)

    def test_within_clip_reorder_rejected_even_amid_other_valid_clips(self):
        """The guard is scoped per clip_id -- clip C being perfectly
        chronological must not mask clip A's own segments being flipped."""
        project = self._project()
        segments = [
            self._seg(CLIP_C, 0.05, 0.4),
            self._seg(CLIP_C, 20.0, 21.0),
            self._seg(CLIP_A, 30.0, 31.0),  # a3 (start=30) BEFORE a2 (start=10) -- illegal
            self._seg(CLIP_A, 10.0, 11.0),
        ]
        with self.assertRaises(HTTPException) as ctx:
            edl_api._validate_segments(project, segments)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(CLIP_A, ctx.exception.detail)


class JudgeAutocutOnlyFlipsKeptTests(unittest.TestCase):
    """pipeline/judge.py's one auto-apply exception (a majority, every-run
    high-severity kept_blooper finding) must ONLY ever flip a sentence's
    `kept` flag -- start/end/clip_id and the sentences list's own order must
    survive byte-for-byte identical."""

    def _make_fake_agent(self, target_number: int):
        """First JUDGE_RUNS calls (pass 1) all agree on one high-severity
        kept_blooper finding pointing at `target_number` (majority + every
        contributing run >= JUDGE_AUTOCUT_SEVERITY -> eligible_autocut);
        every subsequent call (pass 2+) reports nothing, so the judge
        converges and stops."""
        fake_agent = mock.Mock()
        calls = {"n": 0}

        def _run_sync(prompt: str):
            calls["n"] += 1
            if calls["n"] <= config.JUDGE_RUNS:
                finding = SimpleNamespace(
                    kind="kept_blooper",
                    sentence_ids=[target_number],
                    message="camera check blooper, cut it",
                    severity=config.JUDGE_AUTOCUT_SEVERITY,
                )
                return SimpleNamespace(output=SimpleNamespace(findings=[finding]))
            return SimpleNamespace(output=SimpleNamespace(findings=[]))

        fake_agent.run_sync.side_effect = _run_sync
        return fake_agent

    def test_autocut_flips_only_kept_never_start_end_clip_id_or_order(self):
        project = _build_project(list(_RAW_SENTENCES), [CLIP_A, CLIP_B, CLIP_C])
        # "a2" (clip A, start=10.0) is the 3rd sentence in the shared
        # clip_order numbering built by judge._numbered_transcripts (see
        # module docstring's per-clip start-sorted numbering): a1=1,
        # a-cut=2, a2=3, ...
        target_number = 3
        _, _, id_map = judge._numbered_transcripts(project)
        self.assertEqual(id_map[target_number], "a2")

        before = copy.deepcopy(project["sentences"])
        before_order = [s["id"] for s in project["sentences"]]

        fake_agent = self._make_fake_agent(target_number)
        with (
            mock.patch("magic_video_editor.llm.available", return_value=True),
            mock.patch(
                "magic_video_editor.agents.agents.get_agent", return_value=fake_agent
            ),
        ):
            judge.run(_no_log, project)

        after = project["sentences"]
        after_order = [s["id"] for s in after]

        # List identity/order untouched -- no reorder, no insert/delete.
        self.assertEqual(after_order, before_order)

        by_id_before = {s["id"]: s for s in before}
        for s in after:
            b = by_id_before[s["id"]]
            self.assertEqual(s["start"], b["start"], f"{s['id']}: start must never change")
            self.assertEqual(s["end"], b["end"], f"{s['id']}: end must never change")
            self.assertEqual(
                s["clip_id"], b["clip_id"], f"{s['id']}: clip_id must never change"
            )
            if s["id"] == "a2":
                self.assertFalse(s["kept"], "targeted kept_blooper sentence must be auto-cut")
            else:
                self.assertEqual(
                    s["kept"],
                    b["kept"],
                    f"{s['id']}: kept must be untouched by an unrelated finding",
                )

        # The auto-cut invalidates the cached EDL (same convention as a
        # manual reorder) so the next read recomputes it from the new
        # kept-flags -- but build_edl itself must still honor chronology.
        self.assertIsNone(project.get("edl"))
        segments = ordering.build_edl(project)
        by_clip = _segments_by_clip(segments)
        clip_a_starts = [s["start"] for s in by_clip[CLIP_A]]
        self.assertEqual(clip_a_starts, sorted(clip_a_starts))
        # a2 (start=10.0) must no longer surface as its own segment.
        self.assertNotIn("a2", " ".join(seg["text"] for seg in segments))


def _no_log(msg: str) -> None:
    pass


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
