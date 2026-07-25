#!/usr/bin/env python3
"""Tests for the reel dedup analyst (vNext "distinct reels, not always
REEL_SUGGESTIONS" -- owner-reported: "el creador de reels no debería hacer 20
siempre. 20 es el MÁXIMO ... Por intentar llenar los 20 crea reels
prácticamente idénticos"). See docs/PLATFORM-SPEC.md's matching vNext
section and pipeline/reels.py's module docstring for the full design.

The 'reel_dedup' LLM agent is ALWAYS mocked here -- these are unit/wiring
tests, not a live-model eval -- and everything runs against a SCRATCH
MVE_DATA project dir (never the real data dir). MVE_DATA must be set BEFORE
importing anything from magic_video_editor, since magic_video_editor.config
reads it at import time (same convention as scripts/test_reel_previews.py).

Covers:
  1. Structural pre-filter (_reel_dedup_candidate_pairs) flags a pair when
     reels share an OVERLAPPING source window on the same clip_id AND/OR
     near-identical transcript text (rapidfuzz) -- never on topic/keyword
     overlap alone.
  2. A high-confidence "same content" verdict from the (mocked) reel_dedup
     agent collapses a flagged pair to the better candidate.
  3. A pair that only shares a TOPIC (different clip, dissimilar wording) is
     never even sent to the agent -- proven by wiring in an agent that would
     fail the test if it were ever called for that pair -- and both survive.
  4. A borderline-confidence verdict keeps BOTH candidates and only
     annotates the weaker one (dedup_flag), never silently drops it.
  5. same_content=False (however confident) never collapses or flags.
  6. End-to-end via pipeline.reels.suggest(): the persisted project["reels"]
     count is the number of DISTINCT reels after collapsing duplicates,
     never padded up to config.REEL_SUGGESTIONS just because more scored
     candidates existed.

Usage:
    uv run python scripts/test_reel_dedup.py
    uv run python scripts/test_reel_dedup.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_reel_dedup_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from rapidfuzz import fuzz  # noqa: E402

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.pipeline import reels  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

# ---------- fixture text (near-duplicate pairs vs. a merely-similar-topic pair) ----------
# Same underlying moment, reworded -- rapidfuzz token_set_ratio ~79, clears
# REEL_DEDUP_TEXT_SIM_CANDIDATE (70).
TEXT_A1 = (
    "The best way to start a morning routine is drinking a full glass of "
    "water right after waking up."
)
TEXT_A2 = (
    "If you want a good morning routine, start by drinking a full glass of "
    "water as soon as you wake up."
)
# Same theme (morning routines) but a DIFFERENT specific tip -- must NOT be
# treated as a duplicate of A1/A2 (sim ~47, well below the candidate floor).
TEXT_D_TOPIC = (
    "Morning routines work best when you avoid checking your phone for the "
    "first 30 minutes."
)
# Another true duplicate pair (sim ~76), used for the borderline-confidence case.
TEXT_E1 = (
    "Batch meal prepping on Sundays is the single habit that saved me the "
    "most time during the week."
)
TEXT_E2 = (
    "The one habit that saved me the most time each week is batch prepping "
    "my meals every Sunday."
)
# A third true duplicate pair (sim ~80), used only in the end-to-end test.
TEXT_C1 = (
    "Journaling for five minutes before bed is what finally fixed my "
    "anxious late-night thoughts."
)
TEXT_C2 = (
    "What actually fixed my anxious late-night thoughts was journaling for "
    "just five minutes before bed."
)
# A genuinely unrelated candidate -- must survive dedup untouched.
TEXT_G = (
    "Cold plunges every morning changed how alert I feel for the rest of "
    "the entire day."
)


def _mk_candidate(clip_id: str, start: float, end: float, text: str, score: float, title: str = "") -> dict:
    """Shape of one entry in the scored-candidate list `suggest()` builds --
    trimmed to exactly what `_reel_dedup_candidate_pairs`/`_run_reel_dedup_pass`
    read (clip_id/start/end/text/duration/score/title; hook/self_contained/
    payoff aren't touched by dedup so any fixed value is fine here)."""
    return {
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "duration": round(end - start, 1),
        "text": text,
        "score": score,
        "hook": 7.0,
        "self_contained": 7.0,
        "payoff": 7.0,
        "title": title,
    }


class _FakeAgent:
    """Stand-in for a pydantic_ai Agent: `run_sync(prompt).output` returns
    whatever `fn(prompt)` computes. Mirrors the shape reels.py actually calls
    (`agent.run_sync(prompt).output`) without touching Ollama at all."""

    def __init__(self, fn):
        self._fn = fn

    def run_sync(self, prompt: str):
        return types.SimpleNamespace(output=self._fn(prompt))


def _verdict(same_content: bool, keep: str, confidence: int, reason: str = ""):
    return types.SimpleNamespace(
        same_content=same_content, keep=keep, confidence=confidence, reason=reason
    )


def _noop_log(*_args, **_kwargs) -> None:
    pass


class _Log:
    """Minimal stand-in for jobs.JobLog (same shape as test_reel_previews.py's
    own _Log): collects messages, no-op progress -- exactly what a direct
    call to suggest() needs without a real queue/job machinery."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    def progress(self, _frac: float) -> None:
        pass


class ReelDedupPreFilterAndPassTests(unittest.TestCase):
    """Direct unit tests of the two dedup-pipeline building blocks, isolated
    from suggest()'s window generation / scoring / composer passes."""

    def test_a_same_clip_overlap_plus_text_sim_flagged_then_collapsed(self):
        # SAME clip_id, overlapping source windows (0-20 vs 12-32 -> ratio
        # 8/32 = 0.25, right at the floor) AND near-identical text.
        a = _mk_candidate("clipA", 0.0, 20.0, TEXT_A1, score=8.0, title="Morning water A")
        b = _mk_candidate("clipA", 12.0, 32.0, TEXT_A2, score=6.0, title="Morning water B")

        # Sanity on the fixture itself (not the production code): both
        # structural signals are genuinely present.
        self.assertGreaterEqual(reels._overlap(a, b), config.REEL_DEDUP_MIN_WINDOW_OVERLAP)
        self.assertGreaterEqual(
            fuzz.token_set_ratio(a["text"], b["text"]), config.REEL_DEDUP_TEXT_SIM_CANDIDATE
        )

        pairs = reels._reel_dedup_candidate_pairs([a, b])
        self.assertEqual(len(pairs), 1, "overlapping near-duplicate pair must be flagged")

        fake_agent = _FakeAgent(
            lambda _p: _verdict(True, "a", 5, "same moment, reworded opening line")
        )
        with patch("magic_video_editor.pipeline.reels.get_agent", return_value=fake_agent):
            pool = reels._run_reel_dedup_pass(_noop_log, [a, b])

        self.assertEqual(len(pool), 1, "high-confidence duplicate must collapse to one reel")
        self.assertIs(pool[0], a, "agent said keep 'a' -- the higher-scoring reel survives")
        self.assertIsNone(a.get("_dedup_flag"))

    def test_b_shared_topic_different_window_never_flagged_or_touched(self):
        # DIFFERENT clip_id (so _overlap is always 0 regardless of time
        # values) and text that shares a THEME but is not near-identical.
        a = _mk_candidate("clipA", 0.0, 20.0, TEXT_A1, score=8.0)
        d = _mk_candidate("clipD", 100.0, 120.0, TEXT_D_TOPIC, score=7.0)

        self.assertEqual(reels._overlap(a, d), 0.0)
        self.assertLess(
            fuzz.token_set_ratio(a["text"], d["text"]), config.REEL_DEDUP_TEXT_SIM_CANDIDATE
        )

        pairs = reels._reel_dedup_candidate_pairs([a, d])
        self.assertEqual(pairs, [], "same-topic/different-content pair must NOT be flagged")

        # Even if the pair somehow reached the LLM step, wire in an agent
        # that fails the test the instant it's called -- proves the
        # structural pre-filter, not agent good judgement, is what protects
        # a topic-only pair.
        def _must_not_be_called(_prompt):
            raise AssertionError("reel_dedup agent must never run for a topic-only pair")

        fake_agent = _FakeAgent(_must_not_be_called)
        with patch("magic_video_editor.pipeline.reels.get_agent", return_value=fake_agent):
            pool = reels._run_reel_dedup_pass(_noop_log, [a, d])

        self.assertEqual(len(pool), 2)
        self.assertIn(a, pool)
        self.assertIn(d, pool)
        self.assertIsNone(a.get("_dedup_flag"))
        self.assertIsNone(d.get("_dedup_flag"))

    def test_borderline_confidence_flags_but_keeps_both(self):
        e1 = _mk_candidate("clipE1", 0.0, 20.0, TEXT_E1, score=8.0, title="Meal prep A")
        e2 = _mk_candidate("clipE2", 0.0, 20.0, TEXT_E2, score=6.0, title="Meal prep B")
        pairs = reels._reel_dedup_candidate_pairs([e1, e2])
        self.assertEqual(len(pairs), 1)

        fake_agent = _FakeAgent(
            lambda _p: _verdict(
                True, "a", config.REEL_DEDUP_FLAG_CONFIDENCE, "plausible repeat, not fully sure"
            )
        )
        with patch("magic_video_editor.pipeline.reels.get_agent", return_value=fake_agent):
            pool = reels._run_reel_dedup_pass(_noop_log, [e1, e2])

        self.assertEqual(len(pool), 2, "borderline confidence must keep BOTH candidates")
        self.assertIsNone(e1.get("_dedup_flag"), "the kept/winning side is never flagged")
        self.assertEqual(e2.get("_dedup_flag"), "plausible repeat, not fully sure")

    def test_same_content_false_never_collapses_or_flags(self):
        e1 = _mk_candidate("clipE1", 0.0, 20.0, TEXT_E1, score=8.0)
        e2 = _mk_candidate("clipE2", 0.0, 20.0, TEXT_E2, score=6.0)
        fake_agent = _FakeAgent(lambda _p: _verdict(False, "a", 5, "just a similar topic"))
        with patch("magic_video_editor.pipeline.reels.get_agent", return_value=fake_agent):
            pool = reels._run_reel_dedup_pass(_noop_log, [e1, e2])
        self.assertEqual(len(pool), 2)
        self.assertIsNone(e1.get("_dedup_flag"))
        self.assertIsNone(e2.get("_dedup_flag"))

    def test_agent_error_on_one_pair_is_fail_open(self):
        """One pair's agent call blowing up must not lose either candidate
        (fail-open, same convention as every other agent pass in reels.py/
        takes.py)."""
        a = _mk_candidate("clipA", 0.0, 20.0, TEXT_A1, score=8.0)
        b = _mk_candidate("clipA", 12.0, 32.0, TEXT_A2, score=6.0)

        def _boom(_prompt):
            raise RuntimeError("ollama exploded")

        fake_agent = _FakeAgent(_boom)
        with patch("magic_video_editor.pipeline.reels.get_agent", return_value=fake_agent):
            pool = reels._run_reel_dedup_pass(_noop_log, [a, b])
        self.assertEqual(len(pool), 2)


class ReelSuggestDedupEndToEndTests(unittest.TestCase):
    """Exercises pipeline.reels.suggest() itself: REEL_SUGGESTIONS is a
    ceiling, never a padding target -- the persisted reel count must equal
    the number of DISTINCT candidates surviving dedup, capped at the
    ceiling, even when strictly more scored candidates were available."""

    @classmethod
    def setUpClass(cls):
        cls.project = store.new_project("reel-dedup-e2e")
        # suggest() only checks this is truthy before proceeding -- the real
        # sentence data is irrelevant since _candidate_windows is patched out
        # below in favor of a fixed candidate list we fully control.
        cls.project["clips"] = [{"id": "clipG", "role": "camera"}]
        cls.project["sentences"] = [
            {
                "id": "s0",
                "clip_id": "clipG",
                "start": 0.0,
                "end": 1.0,
                "text": "placeholder",
                "kept": True,
            }
        ]
        store.save(cls.project)

        # Seven candidates: three near-duplicate PAIRS (A, E/meal-prep, C) --
        # each pair on a DIFFERENT clip_id per member, so the pool-gather's
        # own 0.45 mutual-overlap filter never touches them (overlap is
        # always 0 across different clips) -- plus one genuinely distinct
        # candidate G. Scores differ within each pair so the mocked
        # reel_dedup verdict has an unambiguous "keep the higher scorer" to
        # report.
        cls.candidates_by_text = {
            TEXT_A1: _mk_candidate("clipA1", 0.0, 20.0, TEXT_A1, score=0.0, title="A1"),
            TEXT_A2: _mk_candidate("clipA2", 0.0, 20.0, TEXT_A2, score=0.0, title="A2"),
            TEXT_E1: _mk_candidate("clipE1", 0.0, 20.0, TEXT_E1, score=0.0, title="E1"),
            TEXT_E2: _mk_candidate("clipE2", 0.0, 20.0, TEXT_E2, score=0.0, title="E2"),
            TEXT_C1: _mk_candidate("clipC1", 0.0, 20.0, TEXT_C1, score=0.0, title="C1"),
            TEXT_C2: _mk_candidate("clipC2", 0.0, 20.0, TEXT_C2, score=0.0, title="C2"),
            TEXT_G: _mk_candidate("clipG2", 0.0, 20.0, TEXT_G, score=0.0, title="G"),
        }
        cls.fixed_candidates = [
            {"clip_id": c["clip_id"], "start": c["start"], "end": c["end"], "text": c["text"], "duration": c["duration"]}
            for c in cls.candidates_by_text.values()
        ]
        # Deterministic per-text scores (reel_scorer mock reads these).
        cls.scores = {
            TEXT_A1: 9.0, TEXT_A2: 7.0,
            TEXT_E1: 8.5, TEXT_E2: 6.5,
            TEXT_C1: 9.5, TEXT_C2: 5.5,
            TEXT_G: 8.0,
        }
        cls.dup_pairs = {frozenset([TEXT_A1, TEXT_A2]), frozenset([TEXT_E1, TEXT_E2]), frozenset([TEXT_C1, TEXT_C2])}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def _fake_reel_scorer(self, prompt: str):
        for text, score in self.scores.items():
            if text[:60] in prompt:
                return types.SimpleNamespace(
                    hook=score, self_contained=score, payoff=score, title=f"Title for {text[:20]}"
                )
        raise AssertionError(f"unrecognized reel_scorer prompt: {prompt!r}")

    def _fake_reel_composer(self, _prompt: str):
        # Never combine -- composer pairing is out of scope for this test,
        # keep the single-window candidates exactly as scored.
        return types.SimpleNamespace(combine=False, why="", order="ab")

    def _fake_reel_dedup(self, prompt: str):
        matched_texts = [t for t in self.scores if t[:40] in prompt]
        if frozenset(matched_texts) not in self.dup_pairs or len(matched_texts) != 2:
            return _verdict(False, "a", 1, "different content")
        part_a, _, part_b = prompt.partition("Reel B")
        text_in_a = next(t for t in matched_texts if t[:40] in part_a)
        text_in_b = next(t for t in matched_texts if t != text_in_a)
        winner_text = text_in_a if self.scores[text_in_a] >= self.scores[text_in_b] else text_in_b
        keep = "a" if winner_text == text_in_a else "b"
        return _verdict(True, keep, 5, "same underlying moment, reworded")

    def _fake_get_agent(self, task: str):
        if task == "reel_scorer":
            return _FakeAgent(self._fake_reel_scorer)
        if task == "reel_composer":
            return _FakeAgent(self._fake_reel_composer)
        if task == "reel_dedup":
            return _FakeAgent(self._fake_reel_dedup)
        raise AssertionError(f"unexpected agent task requested: {task!r}")

    def test_c_distinct_count_not_padded_to_ceiling(self):
        project = store.load(self.project["id"])
        with (
            patch("magic_video_editor.pipeline.reels.llm.available", return_value=True),
            patch("magic_video_editor.pipeline.reels._candidate_windows", return_value=list(self.fixed_candidates)),
            patch("magic_video_editor.pipeline.reels.get_agent", side_effect=self._fake_get_agent),
            patch("magic_video_editor.pipeline.reels._copy_for_reel_safe", return_value=None),
            patch.object(config, "REEL_SUGGESTIONS", 5),
        ):
            reels.suggest(_Log(), project)

        result_titles = {r["title"] for r in project["reels"]}
        # 7 candidates, 3 true-duplicate pairs collapse to 1 each -> 4
        # distinct reels remain -- STRICTLY FEWER than the ceiling (5), i.e.
        # never padded just because more scored candidates existed.
        self.assertLess(
            len(project["reels"]), config.REEL_SUGGESTIONS,
            "distinct-reel count must not be padded up to the REEL_SUGGESTIONS ceiling",
        )
        self.assertEqual(len(project["reels"]), 4, result_titles)

        # Exactly one survivor per duplicate pair -- the higher scorer.
        surviving_texts = {r["text"] for r in project["reels"]}
        for pair in self.dup_pairs:
            survivors_from_pair = surviving_texts & pair
            self.assertEqual(len(survivors_from_pair), 1, f"pair {pair} did not collapse to one")
            (surviving_text,) = survivors_from_pair
            other = next(t for t in pair if t != surviving_text)
            self.assertGreaterEqual(self.scores[surviving_text], self.scores[other])

        # The genuinely distinct candidate must survive untouched.
        self.assertIn(TEXT_G, surviving_texts)

    def test_ceiling_still_caps_when_dedup_collapses_nothing(self):
        """Sanity check the other direction: with the ceiling set BELOW the
        distinct count, suggest() still truncates to the ceiling exactly as
        before -- this feature changes WHEN the cap applies (after dedup),
        not whether it applies at all."""
        project = store.load(self.project["id"])
        with (
            patch("magic_video_editor.pipeline.reels.llm.available", return_value=True),
            patch("magic_video_editor.pipeline.reels._candidate_windows", return_value=list(self.fixed_candidates)),
            patch("magic_video_editor.pipeline.reels.get_agent", side_effect=self._fake_get_agent),
            patch("magic_video_editor.pipeline.reels._copy_for_reel_safe", return_value=None),
            patch.object(config, "REEL_SUGGESTIONS", 2),
        ):
            reels.suggest(_Log(), project)
        self.assertEqual(len(project["reels"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
