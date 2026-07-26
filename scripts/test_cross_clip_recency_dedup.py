#!/usr/bin/env python3
"""Unit tests for the cross-clip RECENCY hint in `_cross_clip_dedup`
(pipeline/takes.py, spec point 5's cross-file clause, 2026-07-26):
"re-recording shortly after stopping a clip often means the later file is a
redo." For a cross-clip candidate pair (A in clip_a, B in clip_b) where BOTH
clips have `recorded_at` set (optional, added at ingest -- never fabricated
when missing), if clip_b started within [0, config.CROSS_CLIP_RECENCY_WINDOW_S]
seconds of clip_a finishing (recorded_at + its info duration), one extra line
is appended to dedup_judge's per-pair USER message -- never the system prompt,
never the DedupJudge schema.

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_take_selection.py / scripts/test_judge_stage.py. MVE_DATA is set
to a scratch tmp dir BEFORE importing anything from magic_video_editor.
`get_agent` is fully MOCKED so this script makes no network call and spawns
no ollama process.

Usage:
    uv run python scripts/test_cross_clip_recency_dedup.py
    uv run python scripts/test_cross_clip_recency_dedup.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_cross_clip_recency_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config  # noqa: E402
from magic_video_editor import store  # noqa: E402
from magic_video_editor.pipeline import takes  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _no_log(msg: str) -> None:
    pass


class _FakeAgent:
    """Records every prompt it was asked to run and always returns a
    same_content=False verdict (nothing to auto-cut/suggest -- these tests
    only care about what's in the prompt, not the resolution logic)."""

    def __init__(self):
        self.calls: list[str] = []

    def run_sync(self, prompt: str):
        self.calls.append(prompt)
        return types.SimpleNamespace(
            output=types.SimpleNamespace(
                same_content=False, confidence=0, keep="a", reason="unrelated"
            )
        )


def _mk_clip(clip_id: str, recorded_at: float | None, duration: float = 60.0) -> dict:
    return {
        "id": clip_id,
        "filename": f"{clip_id}.mp4",
        "role": "camera",
        "is_main": clip_id == "clipA",
        "info": {"duration": duration},
        "recorded_at": recorded_at,
        "recorded_at_source": "metadata" if recorded_at is not None else "unknown",
    }


def _mk_sentence(sid: str, clip_id: str, text: str) -> dict:
    return {"id": sid, "clip_id": clip_id, "text": text, "kept": True, "start": 0.0, "end": 3.0}


def _mk_project(clips: list[dict]) -> dict:
    project = store.new_project("cross-clip-recency-test")
    project["clips"] = clips
    return project


# Two sentences similar enough (share a rare keyword, high token_set_ratio)
# to be picked up as a cross-clip candidate pair by the fuzzy pre-filter.
_TEXT_A = "We need to finish the quarterly budget report soon"
_TEXT_B = "We need to finish the quarterly budget report today"


def _run_pair(recorded_a: float | None, recorded_b: float | None, duration_a: float = 60.0):
    clip_a = _mk_clip("clipA", recorded_a, duration=duration_a)
    clip_b = _mk_clip("clipB", recorded_b)
    project = _mk_project([clip_a, clip_b])
    sentences = [
        _mk_sentence("sA-1", "clipA", _TEXT_A),
        _mk_sentence("sB-1", "clipB", _TEXT_B),
    ]
    agent = _FakeAgent()
    with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent):
        takes._cross_clip_dedup(_no_log, sentences, "budgeting", project)
    return agent


class RecencyHintPresentTests(unittest.TestCase):
    def test_close_recorded_at_pair_gets_hint_in_user_message(self):
        recorded_a = 1_000.0
        recorded_a_end = recorded_a + 60.0  # clip_a's info duration
        recorded_b = recorded_a_end + 100.0  # well within the 900s window
        agent = _run_pair(recorded_a, recorded_b)

        self.assertEqual(len(agent.calls), 1)
        prompt = agent.calls[0]
        self.assertIn("started recording ~100s after clip A stopped", prompt)
        self.assertIn("redo fixing a mistake in A", prompt)
        self.assertIn("judge by content first", prompt)

    def test_hint_exactly_at_window_boundary_present(self):
        recorded_a = 2_000.0
        recorded_a_end = recorded_a + 60.0
        recorded_b = recorded_a_end + config.CROSS_CLIP_RECENCY_WINDOW_S
        agent = _run_pair(recorded_a, recorded_b)

        prompt = agent.calls[0]
        self.assertIn("redo fixing a mistake in A", prompt)

    def test_hint_at_zero_gap_present(self):
        recorded_a = 3_000.0
        recorded_a_end = recorded_a + 60.0
        agent = _run_pair(recorded_a, recorded_a_end)

        prompt = agent.calls[0]
        self.assertIn("~0s after clip A stopped", prompt)


class RecencyHintAbsentTests(unittest.TestCase):
    def test_far_apart_pair_no_hint(self):
        recorded_a = 1_000.0
        recorded_a_end = recorded_a + 60.0
        recorded_b = recorded_a_end + config.CROSS_CLIP_RECENCY_WINDOW_S + 1.0
        agent = _run_pair(recorded_a, recorded_b)

        prompt = agent.calls[0]
        self.assertNotIn("redo fixing a mistake in A", prompt)

    def test_clip_a_recorded_at_none_no_hint(self):
        agent = _run_pair(None, 5_000.0)

        prompt = agent.calls[0]
        self.assertNotIn("redo fixing a mistake in A", prompt)

    def test_clip_b_recorded_at_none_no_hint(self):
        agent = _run_pair(1_000.0, None)

        prompt = agent.calls[0]
        self.assertNotIn("redo fixing a mistake in A", prompt)

    def test_both_recorded_at_none_no_hint(self):
        agent = _run_pair(None, None)

        prompt = agent.calls[0]
        self.assertNotIn("redo fixing a mistake in A", prompt)

    def test_negative_gap_b_before_a_finishes_no_hint(self):
        # clip_b started recording BEFORE clip_a even stopped -- not a
        # "redo shortly after" scenario, never fabricate a hint here.
        recorded_a = 10_000.0
        recorded_a_end = recorded_a + 60.0
        recorded_b = recorded_a_end - 30.0
        agent = _run_pair(recorded_a, recorded_b)

        prompt = agent.calls[0]
        self.assertNotIn("redo fixing a mistake in A", prompt)


if __name__ == "__main__":
    unittest.main()
