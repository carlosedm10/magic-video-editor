#!/usr/bin/env python3
"""Unit tests for pipeline/judge.py's cross-run consensus machinery (spec
point 6): findings from config.JUDGE_RUNS independent edit_judge calls are
clustered by (kind, overlapping sentence_ids) and only kept when they recur
in at least config.JUDGE_MAJORITY runs -- a lone run's finding is discarded
outright, never even surfaced as a suggestion.

Covers:
  - a finding that only shows up in 1 of JUDGE_RUNS runs is discarded
  - a finding that recurs in 2 of 3 runs, with different severities in each,
    is kept with the MIN severity across the contributing runs
  - "kept_blooper" is only eligible for auto-cut when EVERY contributing
    run's severity clears config.JUDGE_AUTOCUT_SEVERITY -- one low-severity
    contributing run is enough to disqualify it
  - auto-cut only ever touches the INTERSECTION of sentence_ids across
    contributing runs, never the union: an extra id one run lumps in
    alongside the true blooper is never cut just because it overlapped
    enough to join the cluster; an empty intersection (no id every
    contributing run agreed on) downgrades the whole finding to a
    suggestion, never an auto-cut
  - config.MAX_JUDGE_ITERATIONS bounds the run()-level pass loop even
    against an agent that keeps returning fresh, always-eligible
    kept_blooper findings on a new sentence every single pass

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_take_selection.py. MVE_DATA is set to a scratch tmp dir BEFORE
importing anything from magic_video_editor. `get_agent` is fully MOCKED so
this script makes no network call and spawns no ollama process.

Usage:
    uv run python scripts/test_judge_consensus.py
    uv run python scripts/test_judge_consensus.py -v
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

_SCRATCH = tempfile.mkdtemp(prefix="mve_judge_consensus_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config  # noqa: E402
from magic_video_editor import store  # noqa: E402
from magic_video_editor.pipeline import judge  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _no_log(msg: str) -> None:
    pass


def _finding(kind: str, sentence_ids: list, severity: int, message: str = "m") -> dict:
    return {"kind": kind, "sentence_ids": sentence_ids, "message": message, "severity": severity}


class SingletonDiscardedTests(unittest.TestCase):
    def test_finding_in_only_one_of_three_runs_is_discarded(self):
        runs = [
            [_finding("kept_blooper", ["a"], severity=5)],
            [],
            [],
        ]
        self.assertLess(1, config.JUDGE_MAJORITY, "test assumes JUDGE_MAJORITY > 1")
        merged = judge._aggregate(runs)
        self.assertEqual(merged, [])


class MajorityMinSeverityTests(unittest.TestCase):
    def test_two_of_three_overlapping_runs_kept_with_min_severity(self):
        runs = [
            [_finding("lost_content", ["a", "b"], severity=5)],
            [_finding("lost_content", ["b", "c"], severity=2)],
            [],
        ]
        merged = judge._aggregate(runs)
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m["kind"], "lost_content")
        self.assertEqual(m["sentence_ids"], ["a", "b", "c"])
        self.assertEqual(m["min_severity"], 2, "must take the MIN severity across runs")
        self.assertFalse(m["eligible_autocut"], "only kept_blooper is ever autocut-eligible")


class KeptBlooperEveryRunGateTests(unittest.TestCase):
    def test_every_contributing_run_must_clear_the_autocut_bar(self):
        below = config.JUDGE_AUTOCUT_SEVERITY - 1
        at_bar = config.JUDGE_AUTOCUT_SEVERITY
        runs = [
            [_finding("kept_blooper", ["a"], severity=at_bar)],
            [_finding("kept_blooper", ["a"], severity=below)],
            [],
        ]
        merged = judge._aggregate(runs)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["min_severity"], below)
        self.assertFalse(
            merged[0]["eligible_autocut"],
            "one low-severity contributing run must disqualify the auto-cut",
        )

    def test_all_contributing_runs_at_or_above_bar_is_eligible(self):
        at_bar = config.JUDGE_AUTOCUT_SEVERITY
        runs = [
            [_finding("kept_blooper", ["a"], severity=at_bar)],
            [_finding("kept_blooper", ["a"], severity=at_bar)],
            [],
        ]
        merged = judge._aggregate(runs)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["eligible_autocut"])


class AutocutIntersectionTests(unittest.TestCase):
    """The over-cut bug fix: auto-cut must only ever touch sentence_ids every
    CONTRIBUTING run agreed on (the intersection), never the union used for
    the human-facing suggestion payload."""

    def test_extra_id_in_one_run_is_not_in_the_autocut_set(self):
        at_bar = config.JUDGE_AUTOCUT_SEVERITY
        # Run 0 lumps an extra sentence "b" in alongside the true blooper
        # "a"; run 1 only flags "a". They cluster (overlap on "a"), majority
        # 2 is met, but the majority never actually agreed on "b".
        runs = [
            [_finding("kept_blooper", ["a", "b"], severity=at_bar)],
            [_finding("kept_blooper", ["a"], severity=at_bar)],
            [],
        ]
        merged = judge._aggregate(runs)
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m["sentence_ids"], ["a", "b"], "union still carries 'b' for context")
        self.assertEqual(
            m["autocut_sentence_ids"], ["a"], "'b' was only ever mentioned by one run"
        )
        self.assertTrue(m["eligible_autocut"], "the agreed-on 'a' still clears the bar")

    def test_empty_intersection_downgrades_to_suggestion_only(self):
        at_bar = config.JUDGE_AUTOCUT_SEVERITY
        # Three runs, each severity above the bar, transitively clustered
        # (0-1 overlap on "b", 1-2 overlap on "c") but with NO sentence_id
        # common to all three contributing runs.
        runs = [
            [_finding("kept_blooper", ["a", "b"], severity=at_bar)],
            [_finding("kept_blooper", ["b", "c"], severity=at_bar)],
            [_finding("kept_blooper", ["c", "d"], severity=at_bar)],
        ]
        merged = judge._aggregate(runs)
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m["sentence_ids"], ["a", "b", "c", "d"])
        self.assertEqual(m["autocut_sentence_ids"], [], "no id survives across all 3 runs")
        self.assertFalse(
            m["eligible_autocut"],
            "an empty intersection must downgrade to a suggestion, never auto-cut",
        )


class _FreshFindingEveryPassAgent:
    """Always returns a fresh, autocut-eligible kept_blooper finding
    targeting sentence number `pass_index + 1` (1-indexed, stable across
    passes since sentences are only ever flipped kept=False, never
    removed/reordered) -- i.e. an agent that NEVER runs out of things to
    flag, so the only thing that can stop the loop is
    config.MAX_JUDGE_ITERATIONS itself."""

    def __init__(self, runs_per_pass: int):
        self.runs_per_pass = runs_per_pass
        self.calls: list[str] = []

    def run_sync(self, prompt: str):
        pass_index = len(self.calls) // self.runs_per_pass
        self.calls.append(prompt)
        finding = types.SimpleNamespace(
            kind="kept_blooper",
            sentence_ids=[pass_index + 1],
            message="fresh blooper",
            severity=config.JUDGE_AUTOCUT_SEVERITY,
        )
        return types.SimpleNamespace(output=types.SimpleNamespace(findings=[finding]))


def _mk_sentence(idx: int, clip_id: str = "clip1") -> dict:
    return {
        "id": f"s{clip_id}-{idx}",
        "clip_id": clip_id,
        "start": float(idx * 2),
        "end": float(idx * 2 + 1.5),
        "text": f"Sentence number {idx}.",
        "kept": True,
    }


class MaxIterationsBoundsTheLoopTests(unittest.TestCase):
    def test_loop_stops_at_max_iterations_despite_always_fresh_findings(self):
        # One more sentence than MAX_JUDGE_ITERATIONS so a sentence is left
        # over, proving the loop stopped because of the cap and not because
        # the agent ran out of things to flag.
        n_sentences = config.MAX_JUDGE_ITERATIONS + 1
        sentences = [_mk_sentence(i) for i in range(n_sentences)]
        project = store.new_project("judge-max-iterations-test")
        project["clips"] = [{"id": "clip1", "filename": "clip1.mp4", "role": "camera", "is_main": True}]
        project["sentences"] = sentences
        # judge.run()'s final save reloads fresh from store (reload-merge
        # concurrency fix) rather than the in-memory snapshot -- persist
        # what this test set up so the disk copy matches it.
        store.save(project)

        agent = _FreshFindingEveryPassAgent(config.JUDGE_RUNS)
        with (
            mock.patch("magic_video_editor.pipeline.judge.llm.available", return_value=True),
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
        ):
            judge.run(_no_log, project)

        self.assertEqual(
            len(agent.calls),
            config.MAX_JUDGE_ITERATIONS * config.JUDGE_RUNS,
            "must run exactly MAX_JUDGE_ITERATIONS passes, no more, no less",
        )
        cut_count = sum(1 for s in project["sentences"] if not s["kept"])
        self.assertEqual(cut_count, config.MAX_JUDGE_ITERATIONS)
        # The extra sentence beyond the cap must survive untouched -- proof
        # the agent still had fresh work left when the loop was cut off.
        self.assertTrue(project["sentences"][-1]["kept"])


if __name__ == "__main__":
    unittest.main()
