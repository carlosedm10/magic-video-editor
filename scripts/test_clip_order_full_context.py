#!/usr/bin/env python3
"""Unit tests for pipeline/ordering.py's hierarchical clip listing (workstream
D, "full-context clip ordering", 2026-07-26): the clip_order prompt used to
blindly truncate every clip's kept text to 1200 chars. It now sends each
clip's FULL kept text when the whole listing fits the resolved model's
context window (token_budget.fits_context), and only falls back to a cheap
per-clip digest (the clip_digest agent) -- NEVER a naive substring cut -- when
it doesn't. Each clip line also carries a human-readable RECORDED line when
the clip's recorded_at is known, omitted otherwise.

Covers:
  - A small project's full kept text fits comfortably -> _build_clip_listing
    sends it verbatim and the clip_digest agent is NEVER called.
  - An oversized project (kept text far exceeding the model's context window)
    -> clip_digest IS called for each clip and its digest output feeds the
    listing (not a truncated substring).
  - A clip with recorded_at set gets a "RECORDED: ..." line; a clip with
    recorded_at None omits it entirely.
  - run() end-to-end (LLM mocked) wires all of this together and still
    produces a valid clip_order.

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_take_selection.py / test_ollama_preflight.py. MVE_DATA is set to
a scratch tmp dir BEFORE importing anything from magic_video_editor. No real
ollama is contacted -- every LLM call is mocked.

Usage:
    uv run python scripts/test_clip_order_full_context.py
    uv run python scripts/test_clip_order_full_context.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_clip_order_full_context_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.pipeline import ordering, token_budget  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _no_log(msg: str) -> None:
    pass


def _make_clip(cid: str, filename: str, recorded_at: float | None = None) -> dict:
    return {
        "id": cid,
        "path": f"/tmp/{filename}",
        "source_path": f"/tmp/{filename}",
        "filename": filename,
        "role": "camera",
        "camera_group": "a",
        "is_main": False,
        "info": {"duration": 60.0},
        "wav": None,
        "transcript": None,
        "language": None,
        "recorded_at": recorded_at,
        "recorded_at_source": "metadata" if recorded_at is not None else "unknown",
    }


def _make_sentence(sid: int, cid: str, text: str, start: float, kept: bool = True) -> dict:
    return {
        "id": sid,
        "clip_id": cid,
        "text": text,
        "start": start,
        "end": start + 1.0,
        "kept": kept,
    }


def _build_project(clip_texts: dict[str, str], recorded_ats: dict[str, float | None]) -> dict:
    project = store.new_project("clip-order-full-context")
    project["clips"] = [
        _make_clip(cid, f"{cid}.mp4", recorded_ats.get(cid)) for cid in clip_texts
    ]
    sentences = []
    sid = 0
    for cid, text in clip_texts.items():
        sentences.append(_make_sentence(sid, cid, text, start=float(sid)))
        sid += 1
    project["sentences"] = sentences
    return project


class BuildClipListingTests(unittest.TestCase):
    """_build_clip_listing's hierarchical full-text/digest strategy."""

    def test_small_project_sends_full_text_digest_never_called(self):
        project = _build_project(
            {"c0": "Hola a todos, hoy os cuento el primer paso.", "c1": "Y ahora el segundo paso."},
            {},
        )
        with mock.patch("magic_video_editor.agents.agents.get_agent") as get_agent:
            listing = ordering._build_clip_listing(
                _no_log, project, ["c0", "c1"], "qwen2.5:14b"
            )
        get_agent.assert_not_called()
        self.assertIn("Hola a todos, hoy os cuento el primer paso.", listing)
        self.assertIn("Y ahora el segundo paso.", listing)

    def test_oversized_project_calls_clip_digest_and_uses_its_output(self):
        # Each clip's kept text alone is ~135,000 chars (~34k tokens at the
        # chars/4 heuristic) -- comfortably bigger than any curated context
        # window in token_budget, forcing the digest path.
        big_text = "palabra de contenido real " * 5000
        project = _build_project({"c0": big_text, "c1": big_text}, {})

        fake_agent = mock.Mock()
        fake_agent.run_sync.return_value.output.summary = "DIGEST SUMMARY"
        with mock.patch(
            "magic_video_editor.agents.agents.get_agent", return_value=fake_agent
        ) as get_agent:
            listing = ordering._build_clip_listing(
                _no_log, project, ["c0", "c1"], "qwen2.5:14b"
            )
        get_agent.assert_called_with("clip_digest")
        self.assertEqual(fake_agent.run_sync.call_count, 2)
        self.assertNotIn(big_text, listing)
        self.assertIn("DIGEST SUMMARY", listing)

    def test_force_digest_skips_full_text_even_when_it_would_fit(self):
        project = _build_project({"c0": "Short content.", "c1": "More short content."}, {})
        fake_agent = mock.Mock()
        fake_agent.run_sync.return_value.output.summary = "DIGEST"
        with mock.patch(
            "magic_video_editor.agents.agents.get_agent", return_value=fake_agent
        ):
            listing = ordering._build_clip_listing(
                _no_log, project, ["c0", "c1"], "llama3.2:3b", force_digest=True
            )
        self.assertEqual(fake_agent.run_sync.call_count, 2)
        self.assertIn("DIGEST", listing)
        self.assertNotIn("Short content.", listing)

    def test_digest_failure_is_fail_open_and_falls_back_to_truncated_text(self):
        big_text = "x" * 5000
        project = _build_project({"c0": big_text}, {})
        with mock.patch(
            "magic_video_editor.agents.agents.get_agent",
            side_effect=RuntimeError("ollama down"),
        ):
            # Must not raise -- one bad clip_digest call never breaks ordering.
            listing = ordering._build_clip_listing(
                _no_log, project, ["c0"], "qwen2.5:14b", force_digest=True
            )
        self.assertIn("[...]", listing)  # old truncation fallback kicked in

    def test_recorded_at_line_present_when_known(self):
        project = _build_project({"c0": "Contenido."}, {"c0": 1_753_500_000.0})
        listing = ordering._build_clip_listing(_no_log, project, ["c0"], "qwen2.5:14b")
        self.assertIn("RECORDED:", listing)

    def test_recorded_at_line_omitted_when_none(self):
        project = _build_project({"c0": "Contenido."}, {"c0": None})
        listing = ordering._build_clip_listing(_no_log, project, ["c0"], "qwen2.5:14b")
        self.assertNotIn("RECORDED:", listing)


class FormatRecordedAtTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(ordering._format_recorded_at(None))

    def test_valid_epoch_formats_as_readable_string(self):
        formatted = ordering._format_recorded_at(1_753_500_000.0)
        self.assertIsNotNone(formatted)
        self.assertRegex(formatted, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_garbage_value_never_raises(self):
        self.assertIsNone(ordering._format_recorded_at(float("inf")))


class RunEndToEndTests(unittest.TestCase):
    """run() end-to-end with the LLM mocked -- confirms the hierarchical
    listing, thinking-model degrade, and num_ctx wiring don't break the
    overall clip_order stage contract (a valid permutation clip_order)."""

    def test_run_produces_valid_order_small_project(self):
        project = _build_project(
            {"c0": "Intro del video.", "c1": "Conclusion del video."}, {}
        )

        clip_order_agent = mock.Mock()
        clip_order_agent.run_sync.return_value.output.order = [1, 0]
        clip_order_agent.run_sync.return_value.output.notes = "c1 sets up, c0 concludes"

        with (
            mock.patch("magic_video_editor.pipeline.ordering.llm.available", return_value=True),
            mock.patch(
                "magic_video_editor.pipeline.ordering._resolve_ordering_model",
                return_value=("qwen2.5:14b", False),
            ),
            mock.patch(
                "magic_video_editor.agents.agents.get_agent", return_value=clip_order_agent
            ),
        ):
            ordering.run(_no_log, project)

        self.assertEqual(project["clip_order"], ["c1", "c0"])
        self.assertIn("c1 sets up", project["order_notes"])
        # num_ctx was actually threaded through to the agent call.
        _, kwargs = clip_order_agent.run_sync.call_args
        self.assertIn("options", kwargs["model_settings"]["extra_body"])
        self.assertIn("num_ctx", kwargs["model_settings"]["extra_body"]["options"])

    def test_num_ctx_estimate_matches_token_budget_contract(self):
        listing = "a" * 4000  # ~1000 tokens at the chars/4 heuristic
        expected = token_budget.num_ctx_for(token_budget.estimate_tokens(listing), "qwen2.5:14b")
        self.assertIsInstance(expected, int)
        self.assertGreater(expected, 0)


if __name__ == "__main__":
    unittest.main()
