#!/usr/bin/env python3
"""Contract test for the paragraph-break feature (owner, 2026-07-25):
pipeline/paragraphs.py + ordering.build_edl's `paragraph_break_after`
NON-DESTRUCTIVELY mark a suggested transition spot where the conversation
changes topic/paragraph ("punto y aparte") -- they never remove content,
never reorder anything, and never auto-apply a transition.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_takes_bounded.py / scripts/test_intra_clip_order.py.
MVE_DATA is set to a scratch tmp dir BEFORE importing anything from
magic_video_editor. The paragraph_break agent (`get_agent("paragraph_break")`)
is ALWAYS mocked here: no real Ollama call is made by this script.

Covers:
  (a) a mocked flag between sentence 2 and 3 forces an extra segment
      boundary there (build_edl merges everything into ONE segment without
      it -- the sentences are close enough); no sentence text is dropped
      (the space-joined text across all output segments is byte-identical
      to the unsplit baseline); the new segment is tagged
      paragraph_break=True and the one before it is NOT.
  (b) no flagged break (agent returns an empty list) -> build_edl's output
      is identical to today's (no paragraph_break_after at all) except every
      segment additionally carries paragraph_break=False.
  (c) a flagged break does NOT auto-apply a transition: round-tripping a
      produced segment through api.edl.EdlSegment (the same model
      api/edl.py normalizes PUT bodies through) yields transition.type ==
      "none" regardless of the paragraph_break tag.
  (d) intra-clip chronological order survives a forced split (segments
      strictly increasing in `start` within each clip_id) -- the existing
      invariant scripts/test_intra_clip_order.py locks down must still hold
      when paragraph-break splitting is in play.

Usage:
    uv run python scripts/test_paragraph_cuts.py
    uv run python scripts/test_paragraph_cuts.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="mve_paragraph_cuts_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.api.edl import EdlSegment  # noqa: E402
from magic_video_editor.pipeline import ordering, paragraphs  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

CLIP_ID = "clip-1"


def _camera_clip(cid: str, filename: str, duration: float = 200.0) -> dict:
    return {
        "id": cid,
        "path": f"/fake/{filename}",
        "source_path": f"/fake/{filename}",
        "filename": filename,
        "role": "camera",
        "camera_group": "main",
        "is_main": True,
        "info": {"duration": duration, "has_audio": True, "has_video": True},
        "wav": None,
        "transcript": None,
        "language": None,
    }


def _sentence(sid: str, clip_id: str, start: float, end: float) -> dict:
    return {
        "id": sid,
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "text": f"[{sid}]",
        "kept": True,
        "reason": "",
    }


# Four sentences, each gap (0.3s) comfortably inside config.MERGE_GAP (1.2s) --
# build_edl merges ALL FOUR into ONE segment when no paragraph break is
# recorded. This is the fixture both scenarios (break flagged / not flagged)
# share.
_SENTENCES = [
    _sentence("s1", CLIP_ID, 1.0, 2.0),
    _sentence("s2", CLIP_ID, 2.3, 3.3),
    _sentence("s3", CLIP_ID, 3.6, 4.6),
    _sentence("s4", CLIP_ID, 4.9, 5.9),
]


def _build_project(sentences: list[dict]) -> dict:
    project = store.new_project("paragraph-cuts-test")
    project["clips"] = [_camera_clip(CLIP_ID, "a.mp4")]
    project["sentences"] = [dict(s) for s in sentences]  # copy -- tests mutate independently
    project["clip_order"] = [CLIP_ID]
    return project


def _no_log(msg: str) -> None:
    pass


def _fake_agent(breaks: list[tuple[int, int]]) -> mock.Mock:
    """breaks: list of (after_id, confidence) -> a fake paragraph_break agent
    whose .run_sync(...).output.breaks mirrors schemas.ParagraphBreaks."""
    agent = mock.Mock()
    agent.run_sync.return_value = SimpleNamespace(
        output=SimpleNamespace(
            breaks=[
                SimpleNamespace(after_id=after_id, confidence=conf, reason="test")
                for after_id, conf in breaks
            ]
        )
    )
    return agent


class ParagraphBreakContract(unittest.TestCase):
    def test_a_flagged_break_forces_a_segment_boundary_without_losing_content(self):
        project = _build_project(_SENTENCES)
        baseline = ordering.build_edl(_build_project(_SENTENCES))
        self.assertEqual(len(baseline), 1, "fixture sentences should merge into ONE segment today")
        baseline_text = baseline[0]["text"]

        fake_agent = _fake_agent([(2, config.PARAGRAPH_BREAK_MIN_CONFIDENCE)])  # after s2, before s3
        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent), \
             mock.patch.object(paragraphs.llm, "available", return_value=True):
            paragraphs.run(_no_log, project)

        self.assertEqual(project["paragraph_break_after_ids"], ["s2"])
        self.assertIsNone(project["edl"], "paragraphs.run must invalidate the cached EDL")

        segments = ordering.build_edl(project)
        self.assertEqual(len(segments), 2, "the break must force an extra segment boundary")

        seg1, seg2 = segments
        self.assertEqual(seg1["clip_id"], CLIP_ID)
        self.assertEqual(seg2["clip_id"], CLIP_ID)
        self.assertFalse(seg1.get("paragraph_break"), "no break before the FIRST segment")
        self.assertTrue(seg2.get("paragraph_break"), "the segment after the break must be tagged")

        # No content removed: the space-joined text across the split
        # segments is exactly the unsplit baseline's text (splitting only
        # changes WHERE the join happens, never which sentences are in it).
        self.assertEqual(" ".join(s["text"] for s in segments), baseline_text)
        for sid in ("s1", "s2", "s3", "s4"):
            self.assertIn(f"[{sid}]", " ".join(s["text"] for s in segments))

        # Same order, same underlying timestamps: every segment boundary is
        # still only ever original sentence start/end shifted by
        # SEGMENT_PAD (never invented), and total duration is unchanged up
        # to the at-most 2*SEGMENT_PAD wobble introduced by padding BOTH
        # sides of a newly-split junction instead of one merged span's two
        # outer edges (build_edl pads every segment's outer edges the same
        # way regardless of why it's a separate segment).
        total_before = sum(s["end"] - s["start"] for s in baseline)
        total_after = sum(s["end"] - s["start"] for s in segments)
        self.assertLessEqual(
            abs(total_after - total_before),
            2 * config.SEGMENT_PAD + 1e-9,
            "splitting must not change the program duration beyond the pad wobble",
        )
        self.assertAlmostEqual(seg1["start"], baseline[0]["start"])  # same leading edge
        self.assertAlmostEqual(seg2["end"], baseline[0]["end"])  # same trailing edge

    def test_b_no_flagged_break_leaves_the_edl_unchanged(self):
        project = _build_project(_SENTENCES)
        baseline = ordering.build_edl(_build_project(_SENTENCES))

        fake_agent = _fake_agent([])  # agent finds nothing -- the common case
        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent), \
             mock.patch.object(paragraphs.llm, "available", return_value=True):
            paragraphs.run(_no_log, project)

        self.assertEqual(project["paragraph_break_after_ids"], [])
        segments = ordering.build_edl(project)

        self.assertEqual(len(segments), len(baseline))
        for got, want in zip(segments, baseline, strict=True):
            self.assertEqual(got["clip_id"], want["clip_id"])
            self.assertEqual(got["start"], want["start"])
            self.assertEqual(got["end"], want["end"])
            self.assertEqual(got["text"], want["text"])
            self.assertFalse(got.get("paragraph_break"))

    def test_c_flagged_break_never_auto_applies_a_transition(self):
        project = _build_project(_SENTENCES)
        fake_agent = _fake_agent([(2, config.PARAGRAPH_BREAK_MIN_CONFIDENCE)])
        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent), \
             mock.patch.object(paragraphs.llm, "available", return_value=True):
            paragraphs.run(_no_log, project)

        segments = ordering.build_edl(project)
        self.assertTrue(any(s.get("paragraph_break") for s in segments))
        for raw in segments:
            # Same model api/edl.py normalizes PUT bodies (and defaults GET
            # reads) through -- a fresh build_edl segment carries no
            # "transition" key at all, so this exercises the SAME default
            # path a real GET/PUT would.
            validated = EdlSegment.model_validate(raw)
            self.assertEqual(
                validated.transition.type,
                "none",
                "a paragraph-break suggestion must never auto-apply a transition",
            )
            self.assertEqual(validated.paragraph_break, raw.get("paragraph_break", False))

    def test_d_intra_clip_chronology_survives_a_forced_split(self):
        project = _build_project(_SENTENCES)
        fake_agent = _fake_agent([(2, config.PARAGRAPH_BREAK_MIN_CONFIDENCE)])
        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent), \
             mock.patch.object(paragraphs.llm, "available", return_value=True):
            paragraphs.run(_no_log, project)

        segments = ordering.build_edl(project)
        by_clip: dict[str, list[float]] = {}
        for s in segments:
            by_clip.setdefault(s["clip_id"], []).append(s["start"])
        for clip_id, starts in by_clip.items():
            self.assertEqual(starts, sorted(starts), f"{clip_id}: not chronological")
            for prev, nxt in zip(starts, starts[1:], strict=False):
                self.assertLess(prev, nxt, f"{clip_id}: segment starts not strictly increasing")

    def test_e_low_confidence_break_is_not_applied(self):
        """A break below config.PARAGRAPH_BREAK_MIN_CONFIDENCE must be
        dropped entirely -- confirms the gate in
        pipeline/paragraphs.py:_detect_clip, not just the happy path above."""
        project = _build_project(_SENTENCES)
        low_conf = max(1, config.PARAGRAPH_BREAK_MIN_CONFIDENCE - 1)
        fake_agent = _fake_agent([(2, low_conf)])
        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent), \
             mock.patch.object(paragraphs.llm, "available", return_value=True):
            paragraphs.run(_no_log, project)

        self.assertEqual(project["paragraph_break_after_ids"], [])
        segments = ordering.build_edl(project)
        self.assertEqual(len(segments), 1)
        self.assertFalse(segments[0].get("paragraph_break"))

    def test_f_disabled_toggle_skips_detection_even_with_a_confident_mock(self):
        project = _build_project(_SENTENCES)
        fake_agent = _fake_agent([(2, 5)])
        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent), \
             mock.patch.object(paragraphs.llm, "available", return_value=True), \
             mock.patch.object(config, "PARAGRAPH_BREAK_ENABLED", False):
            paragraphs.run(_no_log, project)

        self.assertEqual(project["paragraph_break_after_ids"], [])
        fake_agent.run_sync.assert_not_called()


if __name__ == "__main__":
    import shutil

    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
