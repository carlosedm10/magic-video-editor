#!/usr/bin/env python3
"""Bounded unit tests for the takes.py perf/quality fixes (this batch):

  1. `_context_check_clip` was one LLM call PER SENTENCE (O(sentences)) --
     now chunked like `_transcript_cleanup`/`_take_sequencer_clip`
     (config.CONTEXT_CHECK_CHUNK_SIZE), so a clip of N sentences makes
     O(ceil(N / CHUNK)) agent calls, not ~N.
  2. context_check verdicts are now confidence-gated ("suggest, don't
     delete"): only >=CONTEXT_CHECK_AUTOCUT_CONFIDENCE auto-cuts; a lower
     (but >=CONTEXT_CHECK_SUGGEST_CONFIDENCE) verdict becomes an entry in
     project["suggestions"] instead of a silent cut.
  3. `_cross_clip_dedup`'s candidate-pair generation was O(kept^2)
     token_set_ratio calls -- the new rare-keyword bucket pre-filter cuts
     the number of fuzzy comparisons on a long synthetic input while still
     finding the same top duplicate pair.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_reel_transform.py / scripts/test_takes.py-style scripts.
MVE_DATA is set to a scratch tmp dir BEFORE importing anything from
magic_video_editor (config.py reads it at import time) -- never the real
data dir. The pydantic_ai agent (`get_agent`) is ALWAYS mocked here: no
real Ollama call is made by this script.

Usage:
    uv run python scripts/test_takes_bounded.py
    uv run python scripts/test_takes_bounded.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_takes_bounded_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config  # noqa: E402
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


class ContextCheckChunkingTests(unittest.TestCase):
    """(1) _context_check_clip must batch, not call the agent per sentence."""

    def test_call_count_is_chunked_not_per_sentence(self):
        n = 200
        sentences = [_mk_sentence(i, f"Sentence number {i} about the topic.") for i in range(n)]

        fake_agent = mock.Mock()
        fake_agent.run_sync.return_value = SimpleNamespace(
            output=SimpleNamespace(out_of_context=[])
        )

        with mock.patch(
            "magic_video_editor.agents.agents.get_agent", return_value=fake_agent
        ) as get_agent_mock:
            autocut, suggestions = takes._context_check_clip(
                _no_log, sentences, "the topic", project={}
            )

        step = config.CONTEXT_CHECK_CHUNK_SIZE - config.CONTEXT_CHECK_CHUNK_OVERLAP
        expected_calls = 0
        i = 0
        while i < n:
            expected_calls += 1
            if i + config.CONTEXT_CHECK_CHUNK_SIZE >= n:
                break
            i += step

        self.assertEqual(fake_agent.run_sync.call_count, expected_calls)
        # O(sentences / chunk), nowhere near the old O(sentences) behavior.
        self.assertLess(expected_calls, n / 5)
        self.assertGreater(get_agent_mock.call_count, 0)
        self.assertEqual(autocut, set())
        self.assertEqual(suggestions, [])

    def test_confidence_gates_autocut_vs_suggestion(self):
        """A high-confidence flag auto-cuts; a low-confidence flag becomes a
        suggestion instead of a silent cut (spec: "suggest, don't delete")."""
        sentences = [
            _mk_sentence(0, "Es este el objetivo del video de hoy."),
            # meant to be flagged low-confidence:
            _mk_sentence(1, "Un segundo que miro el movil, perdon."),
            _mk_sentence(2, "Ahora sigo con el contenido real del video."),
            # meant to be flagged high-confidence:
            _mk_sentence(3, "Y por ultimo, gracias por ver el video."),
        ]

        def fake_run_sync(prompt: str):
            # Both sentences (local numbers 2 and 4) get flagged in the one
            # chunk this small clip produces, with different confidences.
            return SimpleNamespace(
                output=SimpleNamespace(
                    out_of_context=[
                        SimpleNamespace(id=2, confidence=2, reason="camera check, maybe"),
                        SimpleNamespace(id=4, confidence=5, reason="clearly a sign-off aside"),
                    ]
                )
            )

        fake_agent = mock.Mock()
        fake_agent.run_sync.side_effect = fake_run_sync

        with mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent):
            autocut, suggestions = takes._context_check_clip(
                _no_log, sentences, "a general how-to video", project={}
            )

        # High confidence (5 >= CONTEXT_CHECK_AUTOCUT_CONFIDENCE=4) -> auto-cut.
        self.assertIn(sentences[3]["id"], autocut)
        self.assertNotIn(sentences[1]["id"], autocut)

        # Low confidence (2, >= CONTEXT_CHECK_SUGGEST_CONFIDENCE=2 but below
        # autocut) -> a suggestion, NOT a cut.
        self.assertEqual(len(suggestions), 1)
        sugg = suggestions[0]
        self.assertEqual(sugg["sentence_ids"], [sentences[1]["id"]])
        self.assertEqual(sugg["proposed_action"], "cut")
        self.assertEqual(sugg["status"], "open")
        self.assertIn(sugg["kind"], ("off_topic", "redundant", "repeated_idea", "incoherent"))


class CrossClipDedupPrefilterTests(unittest.TestCase):
    """(3) The rare-keyword pre-filter must cut the number of fuzzy
    comparisons on a long synthetic input while still finding the real
    duplicate pair (same top pair as an unfiltered brute-force scan)."""

    def _synthetic_sentences(self, n_clips: int, n_per_clip: int) -> list[dict]:
        sentences = []
        for c in range(n_clips):
            clip_id = f"clip{c}"
            for i in range(n_per_clip):
                # Filler content, unique per (clip, i) -- no shared rare
                # keywords with anything else, so these should NOT become
                # candidate pairs.
                text = f"Filler sentence number {c} dash {i} about nothing special today"
                sentences.append(_mk_sentence(i, text, clip_id=clip_id))
        # Plant ONE genuine cross-clip duplicate pair sharing a rare,
        # distinctive keyword ("thermodynamics") that appears nowhere else.
        dup_a = _mk_sentence(
            9990, "The core idea is thermodynamics drives the whole engine.", "clip0"
        )
        dup_b = _mk_sentence(
            9991, "Basically thermodynamics is what drives the entire engine here.", "clip1"
        )
        sentences.append(dup_a)
        sentences.append(dup_b)
        return sentences

    def test_prefilter_reduces_comparisons_and_keeps_top_pair(self):
        n_clips, n_per_clip = 4, 60  # 240 filler + 2 planted = 242 kept sentences
        sentences = self._synthetic_sentences(n_clips, n_per_clip)
        # Cross-clip recency hint (sibling workstream) looks up clips by id
        # via store.get_clip(project, clip_id) -- give it a minimal project
        # with real clip stubs (no recorded_at) so the hint is a no-op
        # instead of a KeyError on a bare {}.
        project = {"clips": [{"id": f"clip{c}"} for c in range(n_clips)]}

        # dedup_judge is mocked too -- keep "a" every time, moderate
        # confidence, doesn't matter for this test (we only check pair
        # SELECTION, not the judge's verdict).
        fake_agent = mock.Mock()
        fake_agent.run_sync.return_value = SimpleNamespace(
            output=SimpleNamespace(same_content=True, keep="a", confidence=3, reason="dup")
        )

        call_counter = {"n": 0}
        real_ratio = takes.fuzz.token_set_ratio

        def counting_ratio(a, b):
            call_counter["n"] += 1
            return real_ratio(a, b)

        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=fake_agent),
            mock.patch.object(takes.fuzz, "token_set_ratio", side_effect=counting_ratio),
        ):
            autocut, suggestions = takes._cross_clip_dedup(_no_log, sentences, "engines", project)

        prefiltered_calls = call_counter["n"]

        # Brute-force baseline: how many cross-clip, length-eligible pairs
        # the OLD O(kept^2) loop would have fuzzy-compared.
        norm = {s["id"]: takes._norm(s["text"]) for s in sentences}
        eligible = [s for s in sentences if len(norm[s["id"]].split()) >= config.DUP_MIN_WORDS]
        brute_force_calls = 0
        for i, a in enumerate(eligible):
            for b in eligible[i + 1 :]:
                if a["clip_id"] != b["clip_id"]:
                    brute_force_calls += 1

        self.assertGreater(brute_force_calls, prefiltered_calls)
        self.assertLess(prefiltered_calls, brute_force_calls * 0.5)

        # The planted duplicate pair must still have been found and acted on
        # (mocked judge always returns same_content=True, confidence=3 ->
        # suggestion, since 3 < CROSS_DEDUP_AUTOCUT_CONFIDENCE=4 and
        # >= CROSS_DEDUP_SUGGEST_CONFIDENCE=2).
        dup_ids = {"sclip0-9990", "sclip1-9991"}
        found = any(dup_ids & set(s["sentence_ids"]) for s in suggestions) or bool(
            dup_ids & autocut
        )
        self.assertTrue(found, "the planted cross-clip duplicate pair was not detected")


if __name__ == "__main__":
    unittest.main()
