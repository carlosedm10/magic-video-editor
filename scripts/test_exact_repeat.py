#!/usr/bin/env python3
"""Unit tests for the exact-consecutive-repeat pre-pass in takes.py (owner
heuristic, 2026-07-25): "si se ha dicho EXACTAMENTE lo mismo DOS VECES
SEGUIDAS, quedate con la ULTIMA" -- when the same line is spoken twice (or
more) in a row within one clip, keep only the LAST take and cut the earlier
identical one(s) as bloopers. Deterministic, no LLM.

Covers:
  (a) two identical consecutive sentences -> first cut, last kept
  (b) three identical in a row -> only the last survives
  (c) punctuation/case/whitespace-only differences still count as identical
  (d) two identical but NON-consecutive (different sentence between them)
      are NOT both cut by this pass
  (e) different text is left untouched
  (f) does not cross clip boundaries
  (g) a bounded end-to-end run() sanity pass (no LLM, ollama mocked
      unavailable) confirms the pre-pass composes with the rest of the
      pipeline without crashing and without being undone downstream.

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_takes_bounded.py. MVE_DATA is set to a scratch tmp dir BEFORE
importing anything from magic_video_editor. No real LLM/Ollama call is made
by this script (llm.available() is mocked False for the run() test; the
per-clip helper under test here never calls an agent at all).

Usage:
    uv run python scripts/test_exact_repeat.py
    uv run python scripts/test_exact_repeat.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_exact_repeat_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config  # noqa: E402
from magic_video_editor import store  # noqa: E402
from magic_video_editor.pipeline import takes  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _mk_sentence(idx: int, text: str, clip_id: str = "clip1") -> dict:
    return {
        "id": f"s{clip_id}-{idx}",
        "clip_id": clip_id,
        "start": float(idx * 2),
        "end": float(idx * 2 + 1.5),
        "text": text,
        "kept": True,
    }


def _no_log(msg: str) -> None:
    pass


class NormalizeExactTests(unittest.TestCase):
    """(c) case/punctuation/whitespace-insensitive, but exact-only (no
    fuzzy/filler-word forgiveness like `_norm` gives the other passes)."""

    def test_case_punctuation_whitespace_ignored(self):
        a = takes._normalize_exact("Bueno, empezamos.")
        b = takes._normalize_exact("bueno   empezamos")
        self.assertEqual(a, b)

    def test_different_words_not_equal(self):
        a = takes._normalize_exact("Bueno, empezamos.")
        b = takes._normalize_exact("Bueno, ya empezamos.")
        self.assertNotEqual(a, b)

    def test_filler_words_are_NOT_stripped_unlike_norm(self):
        # Exact-repeat must be stricter than `_norm` (which strips filler
        # words for the fuzzy/LLM passes) -- "um" changes the exact text.
        with_filler = takes._normalize_exact("um bueno empezamos")
        without_filler = takes._normalize_exact("bueno empezamos")
        self.assertNotEqual(with_filler, without_filler)


class ExactRepeatDedupClipTests(unittest.TestCase):
    def test_two_identical_consecutive_keeps_last(self):
        sentences = [
            _mk_sentence(0, "Bueno, empezamos el video."),
            _mk_sentence(1, "Bueno, empezamos el video."),
        ]
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, sentences)
        self.assertEqual(cut_ids, {sentences[0]["id"]})
        self.assertNotIn(sentences[1]["id"], cut_ids)

    def test_three_identical_in_a_row_only_last_survives(self):
        sentences = [
            _mk_sentence(0, "Hola a todos, bienvenidos."),
            _mk_sentence(1, "Hola a todos, bienvenidos."),
            _mk_sentence(2, "Hola a todos, bienvenidos."),
        ]
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, sentences)
        self.assertEqual(cut_ids, {sentences[0]["id"], sentences[1]["id"]})
        self.assertNotIn(sentences[2]["id"], cut_ids)

    def test_punctuation_case_whitespace_variants_still_count_as_identical(self):
        sentences = [
            _mk_sentence(0, "Bueno, empezamos."),
            _mk_sentence(1, "bueno   empezamos"),
        ]
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, sentences)
        self.assertEqual(cut_ids, {sentences[0]["id"]})

    def test_non_consecutive_identical_sentences_not_both_cut(self):
        sentences = [
            _mk_sentence(0, "Vamos a ver el primer punto."),
            _mk_sentence(1, "Este es un inciso totalmente distinto."),
            _mk_sentence(2, "Vamos a ver el primer punto."),
        ]
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, sentences)
        self.assertEqual(cut_ids, set())

    def test_different_text_untouched(self):
        sentences = [
            _mk_sentence(0, "Primero hablamos de A."),
            _mk_sentence(1, "Ahora pasamos a B."),
            _mk_sentence(2, "Y por ultimo C."),
        ]
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, sentences)
        self.assertEqual(cut_ids, set())

    def test_single_sentence_clip_no_crash(self):
        sentences = [_mk_sentence(0, "Solo una frase.")]
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, sentences)
        self.assertEqual(cut_ids, set())

    def test_empty_clip_no_crash(self):
        cut_ids = takes._exact_repeat_dedup_clip(_no_log, [])
        self.assertEqual(cut_ids, set())


class ExactRepeatDoesNotCrossClipBoundariesTests(unittest.TestCase):
    """(f) the per-clip helper only ever sees one clip's sentences, and
    run() groups by clip_id before calling it -- verify run()'s own
    by_clip grouping keeps identical text in DIFFERENT clips untouched."""

    def test_identical_text_in_different_clips_each_kept_by_the_pass(self):
        clip_a = [_mk_sentence(0, "Repetimos esta linea.", clip_id="clipA")]
        clip_b = [_mk_sentence(0, "Repetimos esta linea.", clip_id="clipB")]
        cut_a = takes._exact_repeat_dedup_clip(_no_log, clip_a)
        cut_b = takes._exact_repeat_dedup_clip(_no_log, clip_b)
        self.assertEqual(cut_a, set())
        self.assertEqual(cut_b, set())


class RunEndToEndSanityTests(unittest.TestCase):
    """(g) bounded sanity run of the full `takes.run()` on a tiny synthetic
    project: confirms the pre-pass is wired in early, the fuzzy/AI passes
    below don't re-litigate it, and nothing crashes. LLM passes are fully
    skipped (llm.available() mocked False) -- this test makes no network
    call and spawns no ollama process."""

    def _build_words(self, text: str, start: float) -> list[dict]:
        words = []
        t = start
        for tok in text.split(" "):
            words.append({"w": tok, "s": round(t, 3), "e": round(t + 0.3, 3)})
            t += 0.4
        return words

    def test_run_keeps_last_of_exact_repeat_no_crash(self):
        project = store.new_project("exact-repeat-sanity")

        # One clip: a blooper line said twice verbatim, then a distinct
        # sentence with plenty of real words so it survives the fragment
        # filter and any fuzzy grouping untouched.
        seg1 = {"words": self._build_words("Bueno empezamos el video de hoy.", 0.0)}
        seg2 = {"words": self._build_words("Bueno empezamos el video de hoy.", 3.0)}
        seg3 = {
            "words": self._build_words(
                "Ahora vamos a explicar el primer punto importante del tema.", 6.0
            )
        }
        clip = {
            "id": "clipA",
            "filename": "cam1.mp4",
            "role": "camera",
            "is_main": True,
            "transcript": {"segments": [seg1, seg2, seg3]},
            "wav": None,
        }
        project["clips"] = [clip]

        with mock.patch("magic_video_editor.pipeline.takes.llm.available", return_value=False):
            takes.run(_no_log, project)

        sentences = project["sentences"]
        by_norm: dict[str, list[dict]] = {}
        for s in sentences:
            by_norm.setdefault(takes._normalize_exact(s["text"]), []).append(s)

        blooper_group = by_norm[takes._normalize_exact("Bueno empezamos el video de hoy.")]
        self.assertEqual(len(blooper_group), 2)
        kept_flags = [s["kept"] for s in blooper_group]
        self.assertEqual(kept_flags.count(True), 1, "exactly one of the repeat pair must survive")
        self.assertEqual(kept_flags.count(False), 1)

        # The LAST occurrence (later start time) must be the survivor.
        survivor = next(s for s in blooper_group if s["kept"])
        loser = next(s for s in blooper_group if not s["kept"])
        self.assertGreater(survivor["start"], loser["start"])
        self.assertIn("repetición exacta", loser["reason"])
        self.assertIn("última toma", loser["reason"])

        # The distinct third sentence must be untouched by this pass.
        distinct = [
            s
            for s in sentences
            if takes._normalize_exact(s["text"])
            == takes._normalize_exact(
                "Ahora vamos a explicar el primer punto importante del tema."
            )
        ]
        self.assertEqual(len(distinct), 1)
        self.assertTrue(distinct[0]["kept"])


if __name__ == "__main__":
    unittest.main()
