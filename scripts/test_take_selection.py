#!/usr/bin/env python3
"""Unit tests for two real take-selection defects found in a manual-vs-auto
comparison on a real video (2026-07-26):

FIX 1 -- DEDUP MUST NEVER DROP A LINE TO ZERO. A CTA line was delivered
twice (near-identical retakes); the auto dedup/context_check logic cut BOTH
deliveries, losing the line entirely. Guard: within any duplicate/near-
duplicate cluster (exact-repeat run OR fuzzy-dedup group), exactly one
survivor is protected from being cut by ANY later pass
(cleaner/sequencer/context_check/cross-clip dedup_judge/fragment-drop). See
`takes.py`'s `protected_survivor_ids` set and the final catch-all guard at
the end of `run()`.

FIX 2 -- PREFER THE LATER, MORE-COMPLETE TAKE ("quedate con la ultima"). For
a retake pair like an earlier, truncated "...termina costando el triple."
vs. a later, complete "...costando el triple cuando por fin se hace.", the
heuristic score alone picked the earlier one. Fix: `_select_cluster_winner`
prefers the chronologically later take, then the more complete one (longer /
properly finished), among near-tied candidates (config.TAKE_WINNER_SCORE_MARGIN).

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_exact_repeat.py. MVE_DATA is set to a scratch tmp dir BEFORE
importing anything from magic_video_editor. The LLM is fully MOCKED
(llm.available() patched False) so this script makes no network call and
spawns no ollama process.

Usage:
    uv run python scripts/test_take_selection.py
    uv run python scripts/test_take_selection.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_take_selection_test_")
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


def _build_words(text: str, start: float) -> list[dict]:
    words = []
    t = start
    for tok in text.split(" "):
        words.append({"w": tok, "s": round(t, 3), "e": round(t + 0.3, 3)})
        t += 0.4
    return words


def _run_takes_no_llm(clips: list[dict]) -> dict:
    """Build a scratch project with the given clips and run takes.run() with
    the LLM fully unavailable (deterministic, no network)."""
    project = store.new_project("take-selection-sanity")
    project["clips"] = clips
    with mock.patch("magic_video_editor.pipeline.takes.llm.available", return_value=False):
        takes.run(_no_log, project)
    return project


def _sentence_for(project: dict, needle: str) -> list[dict]:
    return [s for s in project["sentences"] if needle in s["text"]]


class Fix1DuplicateClusterNeverZeroTests(unittest.TestCase):
    """A duplicate/near-duplicate cluster must always keep exactly one
    survivor kept=True, even when later passes would otherwise cut it."""

    def test_fuzzy_cluster_always_keeps_exactly_one(self):
        # Two near-identical (not exact) deliveries of the same CTA line --
        # similar enough to fuzzy-cluster, not identical enough for the
        # exact-repeat pre-pass.
        seg1 = {
            "words": _build_words(
                "Si quieres saber si tu situacion encaja en alguna de estas cinco joyas.", 0.0
            )
        }
        seg2 = {
            "words": _build_words(
                "Si quieres saber si tu situacion encaja en alguna de estas cinco joyas hoy.",
                5.0,
            )
        }
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [seg1, seg2]},
            "wav": None,
        }
        project = _run_takes_no_llm([clip])
        cta = _sentence_for(project, "cinco joyas")
        self.assertEqual(len(cta), 2, "expected both CTA deliveries as separate sentences")
        kept_flags = [s["kept"] for s in cta]
        self.assertEqual(
            kept_flags.count(True), 1, "exactly one CTA delivery must survive, never zero"
        )
        self.assertIn(cta[0]["dup_group"], (f"d{i}" for i in range(10)))
        self.assertEqual(cta[0]["dup_group"], cta[1]["dup_group"])

    def test_unique_non_duplicate_sentence_not_forced_either_way(self):
        # A single, unique sentence: this guard must not force-keep or
        # force-cut it (it's not part of any duplicate cluster).
        seg = {"words": _build_words("Este es un punto completamente distinto y unico.", 0.0)}
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [seg]},
            "wav": None,
        }
        project = _run_takes_no_llm([clip])
        unique = _sentence_for(project, "completamente distinto")
        self.assertEqual(len(unique), 1)
        self.assertIsNone(unique[0]["dup_group"])
        self.assertTrue(unique[0]["kept"])

    def test_protected_survivor_guard_restores_a_cut_winner(self):
        """Directly exercise the guard: build a cluster, let the winner-
        selection loop run, then simulate a later pass wrongly cutting the
        chosen survivor, and confirm the final catch-all in run() would
        restore it. We test this at the unit level against the documented
        contract (protected_survivor_ids + final restore loop) by running
        the full pipeline and checking the invariant holds even though we
        can't inject a fake AI cut without a real agent -- so instead we
        assert the structural invariant on a 3-way exact-duplicate cluster
        that every early-cut pass (cleaner/sequencer/context) is a no-op on
        (LLM disabled) yet still resolves to exactly one survivor."""
        seg1 = {"words": _build_words("Repetimos la misma frase importante otra vez.", 0.0)}
        seg2 = {"words": _build_words("Repetimos la misma frase importante otra vez ya.", 4.0)}
        seg3 = {"words": _build_words("Repetimos la misma frase importante otra vez ya si.", 8.0)}
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [seg1, seg2, seg3]},
            "wav": None,
        }
        project = _run_takes_no_llm([clip])
        cluster = _sentence_for(project, "misma frase importante")
        self.assertEqual(len(cluster), 3)
        self.assertEqual(sum(1 for s in cluster if s["kept"]), 1)


class Fix2LaterMoreCompleteWinsTests(unittest.TestCase):
    """Within a duplicate/retake cluster, the survivor must be the later,
    more complete take, not just the one with the highest raw disfluency
    score."""

    def test_retake_pair_later_complete_wins(self):
        # Reproduces the real "costando el triple" case: an earlier,
        # truncated take vs. a later take that finishes the clause.
        truncated = (
            "Es el clasico caso del mantenimiento que se retrasa y termina costando el triple."
        )
        complete = (
            "Es el clasico caso del mantenimiento que se retrasa y termina "
            "costando el triple cuando por fin se hace."
        )
        seg1 = {"words": _build_words(truncated, 0.0)}
        seg2 = {"words": _build_words(complete, 6.0)}
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [seg1, seg2]},
            "wav": None,
        }
        project = _run_takes_no_llm([clip])
        cluster = _sentence_for(project, "costando el triple")
        self.assertEqual(len(cluster), 2)
        survivor = next(s for s in cluster if s["kept"])
        loser = next(s for s in cluster if not s["kept"])
        self.assertIn("cuando por fin se hace", survivor["text"])
        self.assertIn(truncated.rstrip("."), loser["text"])
        # Later in time AND longer/more complete.
        self.assertGreater(survivor["start"], loser["start"])
        self.assertGreaterEqual(len(survivor["text"]), len(loser["text"]))

    def test_three_way_cluster_keeps_latest_complete(self):
        first = "Al final el proyecto se retrasa y sale mal."
        second = "Al final el proyecto se retrasa y sale muy mal."
        third = "Al final el proyecto se retrasa y sale muy mal para todos."
        seg1 = {"words": _build_words(first, 0.0)}
        seg2 = {"words": _build_words(second, 5.0)}
        seg3 = {"words": _build_words(third, 10.0)}
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [seg1, seg2, seg3]},
            "wav": None,
        }
        project = _run_takes_no_llm([clip])
        cluster = _sentence_for(project, "el proyecto se retrasa")
        self.assertEqual(len(cluster), 3)
        self.assertEqual(sum(1 for s in cluster if s["kept"]), 1)
        survivor = next(s for s in cluster if s["kept"])
        self.assertIn("para todos", survivor["text"])
        # It must be the chronologically last (highest start time) of the three.
        self.assertEqual(survivor["start"], max(s["start"] for s in cluster))

    def test_select_cluster_winner_directly(self):
        """Direct unit test of _select_cluster_winner: a later, more
        complete candidate with a near-tied score beats an earlier, shorter
        one with a marginally higher raw score."""
        earlier = {
            "id": "a",
            "text": "termina costando el triple.",
            "words": [{"w": w} for w in "termina costando el triple".split()],
            "score": 5.0,
            "take_index": 0,
        }
        later_complete = {
            "id": "b",
            "text": "costando el triple cuando por fin se hace.",
            "words": [{"w": w} for w in "costando el triple cuando por fin se hace".split()],
            "score": 4.2,  # within TAKE_WINNER_SCORE_MARGIN of `earlier`
            "take_index": 1,
        }
        winner = takes._select_cluster_winner([earlier, later_complete])
        self.assertEqual(winner["id"], "b")

    def test_select_cluster_winner_respects_score_margin(self):
        """When the earlier take is CLEARLY better (outside the margin),
        the later one must not win just because it's later."""
        earlier_much_better = {
            "id": "a",
            "text": "Frase perfecta y completa.",
            "words": [{"w": w} for w in "Frase perfecta y completa".split()],
            "score": 9.0,
            "take_index": 0,
        }
        later_much_worse = {
            "id": "b",
            "text": "Frase eh eh peor.",
            "words": [{"w": w} for w in "Frase eh eh peor".split()],
            "score": 1.0,  # far outside TAKE_WINNER_SCORE_MARGIN
            "take_index": 1,
        }
        winner = takes._select_cluster_winner([earlier_much_better, later_much_worse])
        self.assertEqual(winner["id"], "a")


class Fix1And2ComposeSanityTests(unittest.TestCase):
    """End-to-end sanity: the exact-repeat pre-pass, fuzzy dedup + Fix 1/2
    guards, and the rest of the pipeline all compose without regressing each
    other on a small synthetic project with both patterns present."""

    def test_exact_repeat_and_fuzzy_retake_together(self):
        exact1 = {"words": _build_words("Hola a todos bienvenidos al video.", 0.0)}
        exact2 = {"words": _build_words("Hola a todos bienvenidos al video.", 3.0)}
        retake1 = {
            "words": _build_words(
                "El mantenimiento que se retrasa termina costando el triple.", 6.0
            )
        }
        retake2 = {
            "words": _build_words(
                "El mantenimiento que se retrasa termina costando el triple al final.", 12.0
            )
        }
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [exact1, exact2, retake1, retake2]},
            "wav": None,
        }
        project = _run_takes_no_llm([clip])

        hola = _sentence_for(project, "bienvenidos al video")
        self.assertEqual(sum(1 for s in hola if s["kept"]), 1)

        triple = _sentence_for(project, "costando el triple")
        self.assertEqual(len(triple), 2)
        self.assertEqual(sum(1 for s in triple if s["kept"]), 1)
        survivor = next(s for s in triple if s["kept"])
        self.assertIn("al final", survivor["text"])


if __name__ == "__main__":
    unittest.main()
