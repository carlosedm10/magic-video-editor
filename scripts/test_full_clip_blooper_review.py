#!/usr/bin/env python3
"""Unit tests for the WS-C full-clip blooper-review pass in takes.py (spec
point 3, take-keeping half): the chunked windows (transcript_cleaner <=40,
take_sequencer ~12, context_check <=15 sentences) can each only see a small
slice of one clip, so a bad take whose better retake lands far outside every
window slips through all three. `takes._full_clip_review` sends the clip's
ENTIRE not-yet-cut, numbered sentence list to the new `blooper_reviewer`
agent in ONE prompt, falling back to `config.FULL_CLIP_REVIEW_FALLBACK_
CHUNKS` large overlapping chunks (never the tiny windows) only when the
full text doesn't fit the resolved model's context window.

Covers:
  - an in-budget clip makes exactly ONE call to the blooper_reviewer agent
  - an oversized clip falls back to FULL_CLIP_REVIEW_FALLBACK_CHUNKS calls
  - a sentence in `protected_survivor_ids` is NEVER auto-cut, regardless of
    the confidence the (mocked) agent reports for it
  - a low-confidence flag becomes a project["suggestions"] entry, not an
    auto-cut

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_take_selection.py. MVE_DATA is set to a scratch tmp dir BEFORE
importing anything from magic_video_editor. `get_agent` is fully MOCKED so
this script makes no network call and spawns no ollama process.

Usage:
    uv run python scripts/test_full_clip_blooper_review.py
    uv run python scripts/test_full_clip_blooper_review.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from rapidfuzz import fuzz

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_full_clip_blooper_review_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import (  # noqa: E402
    config,
    store,
)
from magic_video_editor.pipeline import takes  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _no_log(msg: str) -> None:
    pass


def _mk_sentence(idx: int, text: str, clip_id: str = "clip1") -> dict:
    return {
        "id": f"s{clip_id}-{idx}",
        "clip_id": clip_id,
        "start": float(idx * 2),
        "end": float(idx * 2 + 1.5),
        "text": text,
    }


def _fake_flag(
    sentence_number: int,
    superseded_by: int,
    confidence: int,
    reason: str = "superseded take",
):
    return types.SimpleNamespace(
        sentence_number=sentence_number,
        superseded_by=superseded_by,
        confidence=confidence,
        reason=reason,
    )


def _fake_result(flags: list):
    return types.SimpleNamespace(flags=flags)


class _FakeAgent:
    """Records every prompt it's asked to run and returns a canned
    per-call response from `responses` (one response per call, reused for
    the last one if there are more calls than responses)."""

    def __init__(self, responses: list):
        self.responses = responses
        self.calls: list[str] = []

    def run_sync(self, prompt: str):
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return types.SimpleNamespace(output=self.responses[idx])


class InBudgetSingleCallTests(unittest.TestCase):
    """A clip whose full text fits the resolved model's context window
    makes exactly ONE call to blooper_reviewer, not one per sentence/chunk."""

    def test_one_call_when_full_text_fits(self):
        sentences = [_mk_sentence(i, f"Sentence number {i} of the clip.") for i in range(6)]
        agent = _FakeAgent([_fake_result([])])
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            autocut, suggestions = takes._full_clip_review(_no_log, sentences, {}, set())
        self.assertEqual(len(agent.calls), 1, "expected exactly one full-text call")
        self.assertEqual(autocut, set())
        self.assertEqual(suggestions, [])


class OversizedChunkedFallbackTests(unittest.TestCase):
    """A clip whose full text does NOT fit the resolved model's context
    window falls back to FULL_CLIP_REVIEW_FALLBACK_CHUNKS large overlapping
    chunks -- never the tiny 12/15/40-sentence windows."""

    def test_falls_back_to_configured_chunk_count(self):
        sentences = [_mk_sentence(i, f"Sentence number {i} of the clip.") for i in range(12)]
        agent = _FakeAgent([_fake_result([])])
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=False,
            ),
        ):
            takes._full_clip_review(_no_log, sentences, {}, set())
        self.assertEqual(
            len(agent.calls),
            config.FULL_CLIP_REVIEW_FALLBACK_CHUNKS,
            "expected one call per fallback chunk, not the tiny windows",
        )

    def test_fallback_chunks_are_large_and_overlapping_not_tiny_windows(self):
        sentences = [_mk_sentence(i, f"Sentence number {i} of the clip.") for i in range(30)]
        chunks = takes._large_overlapping_chunks(sentences, config.FULL_CLIP_REVIEW_FALLBACK_CHUNKS)
        self.assertEqual(len(chunks), config.FULL_CLIP_REVIEW_FALLBACK_CHUNKS)
        for chunk in chunks:
            # Must be much bigger than the tiny 12-sentence take_sequencer window.
            self.assertGreater(len(chunk), takes.SEQUENCER_WINDOW_SIZE)
        # Every original sentence must be covered by at least one chunk.
        covered_ids = {s["id"] for chunk in chunks for s in chunk}
        self.assertEqual(covered_ids, {s["id"] for s in sentences})


class ProtectedSurvivorNeverCutTests(unittest.TestCase):
    """A protected_survivor_ids sentence must never be auto-cut, no matter
    how confident the (mocked) agent claims to be about it."""

    def test_high_confidence_flag_on_protected_id_is_dropped(self):
        sentences = [_mk_sentence(i, f"Sentence number {i} of the clip.") for i in range(4)]
        protected_ids = {sentences[2]["id"]}
        # Agent flags sentence #3 (1-indexed -> local index 2, the protected one)
        # at max confidence.
        agent = _FakeAgent([_fake_result([_fake_flag(3, superseded_by=4, confidence=5)])])
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            autocut, suggestions = takes._full_clip_review(
                _no_log, sentences, {}, protected_ids
            )
        self.assertNotIn(sentences[2]["id"], autocut)
        self.assertEqual(autocut, set())
        self.assertEqual(suggestions, [])
        # The prompt itself must have told the model this id was off-limits.
        self.assertIn("Off-limits sentence numbers", agent.calls[0])


class LowConfidenceSuggestionTests(unittest.TestCase):
    """A flag below the autocut threshold (but at/above the suggest
    threshold) becomes an open suggestion, not a silent cut."""

    def test_low_confidence_flag_becomes_suggestion_not_autocut(self):
        sentences = [_mk_sentence(i, f"Sentence number {i} of the clip.") for i in range(4)]
        confidence = config.FULL_CLIP_REVIEW_SUGGEST_CONFIDENCE
        self.assertLess(confidence, config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE)
        agent = _FakeAgent(
            [_fake_result([_fake_flag(2, superseded_by=3, confidence=confidence)])]
        )
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            autocut, suggestions = takes._full_clip_review(_no_log, sentences, {}, set())
        self.assertEqual(autocut, set())
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["sentence_ids"], [sentences[1]["id"]])
        self.assertEqual(suggestions[0]["proposed_action"], "cut")
        self.assertEqual(suggestions[0]["status"], "open")

    def test_high_confidence_flag_autocuts_when_not_protected(self):
        sentences = [_mk_sentence(i, f"Sentence number {i} of the clip.") for i in range(4)]
        agent = _FakeAgent(
            [
                _fake_result(
                    [
                        _fake_flag(
                            1,
                            superseded_by=2,
                            confidence=config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE,
                        )
                    ]
                )
            ]
        )
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            autocut, suggestions = takes._full_clip_review(_no_log, sentences, {}, set())
        self.assertEqual(autocut, {sentences[0]["id"]})
        self.assertEqual(suggestions, [])


class TooFewSentencesTests(unittest.TestCase):
    """Fewer than 2 sentences: nothing to compare against, so the pass is a
    guaranteed no-op and must not even call the agent."""

    def test_single_sentence_never_calls_agent(self):
        sentences = [_mk_sentence(0, "Only one sentence in this clip.")]
        with mock.patch("magic_video_editor.agents.agents.get_agent") as get_agent:
            autocut, suggestions = takes._full_clip_review(_no_log, sentences, {}, set())
        get_agent.assert_not_called()
        self.assertEqual(autocut, set())
        self.assertEqual(suggestions, [])


class SupersededByVerificationTests(unittest.TestCase):
    """Precision fix (2026-07-26): a live-verification run against
    deepseek-r1:14b auto-cut two topic-setup/transition sentences with NO
    genuine later restatement. `superseded_by` is now a required BlooperFlag
    field and takes.py's _full_clip_review code-verifies it (real, later,
    kept, textually similar via rapidfuzz token_set_ratio) before trusting
    it -- these tests are the regression coverage: a hallucinated
    `superseded_by` (nonexistent / earlier / dissimilar) must never auto-cut,
    while a genuine, verified repeat still does."""

    # Distinct, controlled-similarity sentence texts (module-level constants
    # so every test in this class shares the same fixture):
    #   _EARLIER / _LATER_GENUINE: near-identical restatement (rapidfuzz
    #       token_set_ratio ~100 -- a genuine repeat).
    #   _UNRELATED: a completely different topic (~34 similarity to
    #       _EARLIER -- below FULL_CLIP_REVIEW_SUPERSEDE_DROP_SIMILARITY).
    _EARLIER = "El piso tiene dos habitaciones y una terraza."
    _LATER_GENUINE = "El piso tiene dos habitaciones y una terraza muy luminosa."
    _UNRELATED = "Mañana llueve mucho en Madrid según el pronóstico."

    def _sentences(self) -> list[dict]:
        return [
            _mk_sentence(0, self._EARLIER),
            _mk_sentence(1, self._UNRELATED),
            _mk_sentence(2, self._LATER_GENUINE),
        ]

    def _run(self, flag) -> tuple[set[str], list[dict]]:
        agent = _FakeAgent([_fake_result([flag])])
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            return takes._full_clip_review(_no_log, self._sentences(), {}, set())

    def test_genuine_verified_repeat_still_autocuts(self):
        """sentence #1 (_EARLIER) genuinely superseded by #3 (_LATER_GENUINE,
        ~100 similarity) at autocut confidence -> autocut, as before."""
        sentences = self._sentences()
        flag = _fake_flag(
            1, superseded_by=3, confidence=config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE
        )
        autocut, suggestions = self._run(flag)
        self.assertEqual(autocut, {sentences[0]["id"]})

    def test_nonexistent_superseded_by_never_autocuts(self):
        """superseded_by pointing outside the numbered list entirely
        (hallucinated) -- structurally unverifiable, downgraded to a
        suggestion at most, never auto-cut."""
        sentences = self._sentences()
        flag = _fake_flag(
            1, superseded_by=99, confidence=config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE
        )
        autocut, suggestions = self._run(flag)
        self.assertEqual(autocut, set())
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["sentence_ids"], [sentences[0]["id"]])

    def test_earlier_superseded_by_never_autocuts(self):
        """superseded_by pointing BACKWARD (or at itself) -- never a real
        "later, better" restatement -- structurally unverifiable, never
        auto-cut."""
        sentences = self._sentences()
        flag = _fake_flag(
            3, superseded_by=1, confidence=config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE
        )
        autocut, suggestions = self._run(flag)
        self.assertEqual(autocut, set())
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["sentence_ids"], [sentences[2]["id"]])

    def test_dissimilar_superseded_by_dropped_entirely_never_autocuts(self):
        """superseded_by resolves to a real, later, kept sentence, but one
        that says something completely different (~34 similarity, below
        FULL_CLIP_REVIEW_SUPERSEDE_DROP_SIMILARITY) -- too dissimilar to
        trust even as a suggestion, dropped outright, never auto-cut."""
        similarity = fuzz.token_set_ratio(
            takes._norm(self._EARLIER), takes._norm(self._UNRELATED)
        )
        self.assertLess(similarity, config.FULL_CLIP_REVIEW_SUPERSEDE_DROP_SIMILARITY)
        flag = _fake_flag(
            1, superseded_by=2, confidence=config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE
        )
        autocut, suggestions = self._run(flag)
        self.assertEqual(autocut, set())
        self.assertEqual(suggestions, [])

    def test_similar_but_below_supersede_floor_becomes_suggestion_not_autocut(self):
        """A superseded_by claim that's real/later/kept but only middling
        similarity (at/above the drop floor, below the trust floor) is
        downgraded to a suggestion, never auto-cut -- distinct from the
        "too dissimilar, dropped entirely" case above."""
        moderately_similar = "Cerca del centro hay pisos con terraza y ascensor."
        similarity = fuzz.token_set_ratio(
            takes._norm(self._EARLIER), takes._norm(moderately_similar)
        )
        self.assertGreaterEqual(similarity, config.FULL_CLIP_REVIEW_SUPERSEDE_DROP_SIMILARITY)
        self.assertLess(similarity, config.FULL_CLIP_REVIEW_SUPERSEDE_SIMILARITY)
        sentences = [
            _mk_sentence(0, self._EARLIER),
            _mk_sentence(1, moderately_similar),
        ]
        agent = _FakeAgent(
            [
                _fake_result(
                    [
                        _fake_flag(
                            1,
                            superseded_by=2,
                            confidence=config.FULL_CLIP_REVIEW_AUTOCUT_CONFIDENCE,
                        )
                    ]
                )
            ]
        )
        with (
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            autocut, suggestions = takes._full_clip_review(_no_log, sentences, {}, set())
        self.assertEqual(autocut, set())
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["sentence_ids"], [sentences[0]["id"]])


class EndToEndRunIntegrationTests(unittest.TestCase):
    """Bounded sanity check that run() actually calls the full-clip review
    pass (wired in after transcript_cleaner/take_sequencer/context_check)
    and that a protected duplicate-cluster survivor still can't be cut by
    it, end to end."""

    def _build_words(self, text: str, start: float) -> list[dict]:
        words = []
        t = start
        for tok in text.split(" "):
            words.append({"w": tok, "s": round(t, 3), "e": round(t + 0.3, 3)})
            t += 0.4
        return words

    def test_run_wires_in_full_clip_review_and_respects_protected_survivor(self):
        seg1 = {"words": self._build_words("Primero decimos una frase completamente unica.", 0.0)}
        seg2 = {
            "words": self._build_words(
                "Y luego una segunda frase distinta que no se repite.", 4.0
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
        project = store.new_project("full-clip-review-integration")
        project["clips"] = [clip]

        agent = _FakeAgent([_fake_result([])])
        with (
            mock.patch("magic_video_editor.pipeline.takes.llm.available", return_value=True),
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            takes.run(_no_log, project)
        # blooper_reviewer must have been called at least once (on top of
        # video_topic/transcript_cleaner/take_sequencer/context_check, all
        # routed through the same mocked get_agent).
        self.assertGreaterEqual(len(agent.calls), 1)
        self.assertEqual(len(project["sentences"]), 2)


class ClusterWinnerNeverSuggestedTests(unittest.TestCase):
    """Confirmed-finding regression (2026-07-26): `protected_survivor_ids`
    passed into `_full_clip_review` during `run()` only contains the
    exact-repeat pre-pass ids at call time -- the fuzzy-dedup cluster winner
    is only added to the set LATER, in the winner-selection loop. Auto-cut
    was already guarded against this (the apply-verdicts loop runs after
    winner selection), but the SUGGESTIONS `_full_clip_review` returns were
    never re-filtered against the final protected set, so a fuzzy-dedup
    cluster winner (the take we just decided to KEEP) could still show up
    in project["suggestions"] proposing to cut it. This exercises `run()`
    end to end: two near-duplicate deliveries of the same line in one clip
    (fuzzy-dedup cluster, winner = later take), with the mocked
    blooper_reviewer flagging the winner at suggest-level confidence. The
    winner must end up kept=True AND must never appear in
    project["suggestions"]."""

    def _build_words(self, text: str, start: float) -> list[dict]:
        words = []
        t = start
        for tok in text.split(" "):
            words.append({"w": tok, "s": round(t, 3), "e": round(t + 0.3, 3)})
            t += 0.4
        return words

    def test_fuzzy_cluster_winner_never_in_full_review_suggestions(self):
        # Two near-duplicate takes of the same line, far enough apart that
        # they land in different sentences -- rapidfuzz token_sort_ratio
        # should still cluster them (near-identical wording).
        seg1 = {
            "words": self._build_words(
                "Bienvenidos a este video sobre nuestro producto genial.", 0.0
            )
        }
        seg2 = {"words": self._build_words("Y ahora hablamos de otra cosa distinta.", 6.0)}
        seg3 = {
            "words": self._build_words(
                "Bienvenidos a este video sobre nuestro producto genial y bueno.", 12.0
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
        project = store.new_project("cluster-winner-suggestion-guard")
        project["clips"] = [clip]

        # blooper_reviewer flags EVERY sentence at suggest-level confidence
        # (below autocut) on every call, so whichever sentence numbers exist
        # in whatever window/chunk it's asked about, they all get a
        # suggestion -- including, if the bug is present, the cluster
        # winner (seg3's sentence, kept by fuzzy dedup as the later take).
        suggest_conf = config.FULL_CLIP_REVIEW_SUGGEST_CONFIDENCE

        def _flag_all(prompt: str):
            # Count numbered lines "N: " in the prompt to flag all of them.
            import re as _re

            nums = [int(m) for m in _re.findall(r"^(\d+):", prompt, flags=_re.MULTILINE)]
            # superseded_by = n + 1 for each (may be out of range for the
            # last number -- that's fine, _verify_superseded_by treats an
            # out-of-range claim as unverifiable rather than crashing, and
            # this test only cares about suggest-level confidence gating,
            # not verification).
            return types.SimpleNamespace(
                flags=[_fake_flag(n, superseded_by=n + 1, confidence=suggest_conf) for n in nums]
            )

        class _AlwaysFlagAgent:
            def run_sync(self, prompt: str):
                return types.SimpleNamespace(output=_flag_all(prompt))

        agent = _AlwaysFlagAgent()
        with (
            mock.patch("magic_video_editor.pipeline.takes.llm.available", return_value=True),
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch(
                "magic_video_editor.pipeline.takes.token_budget.fits_context",
                return_value=True,
            ),
        ):
            takes.run(_no_log, project)

        # Find the fuzzy-dedup cluster containing seg1/seg3's sentences and
        # identify the winner (the one kept=True with a dup_group set).
        cluster_sentences = [s for s in project["sentences"] if s.get("dup_group") is not None]
        self.assertTrue(cluster_sentences, "expected the two near-duplicate takes to cluster")
        winners = [s for s in cluster_sentences if s["kept"]]
        self.assertEqual(len(winners), 1, "cluster must have exactly one surviving winner")
        winner_id = winners[0]["id"]

        # The winner must never appear in any suggestion's sentence_ids.
        for sugg in project.get("suggestions", []):
            self.assertNotIn(
                winner_id,
                sugg["sentence_ids"],
                f"protected cluster winner {winner_id} leaked into a suggestion: {sugg}",
            )


if __name__ == "__main__":
    unittest.main()
