#!/usr/bin/env python3
"""Unit tests for the "thinking"/reasoning-model tier table (workstream D,
2026-07-26): api/ollama.py's _THINKING_MODEL_TIERS + recommended_thinking_model()
mirror _RECOMMENDATION_TIERS/recommended_default_model() (same 48/24/16/0 RAM
boundaries), plus model_installed_and_fits() -- a non-raising twin of
preflight_check_models() for a single model -- and
pipeline/ordering._resolve_ordering_model()'s degrade ladder that uses it:
thinking model ready -> use it, full-text path; not ready -> fall back to the
task's configured model with digests forced. Never hangs or crashes when the
thinking model is absent/unreachable.

No pytest in this project's dependency set -- stdlib unittest, same spirit
and mocking style as scripts/test_ollama_preflight.py. Every test mocks
psutil/httpx -- no real ollama is spawned or contacted.

Usage:
    uv run python scripts/test_thinking_model_tiers.py
    uv run python scripts/test_thinking_model_tiers.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fake_vmem(total_bytes: float):
    return mock.Mock(total=total_bytes)


def _fake_tags_response(models: list[tuple[str, float]]):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json.return_value = {
        "models": [{"name": name, "size": int(size_gb * (1024**3))} for name, size_gb in models]
    }
    return resp


class ThinkingModelTierTableTests(unittest.TestCase):
    """recommended_thinking_model() against each RAM tier boundary, mirroring
    RecommendedDefaultModelTests in test_ollama_preflight.py."""

    def test_48gb_picks_qwen3_32b(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
        ):
            self.assertEqual(ollama.recommended_thinking_model(), "qwen3:32b")

    def test_24gb_picks_qwen3_14b(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", return_value=_fake_vmem(24 * 1024**3)
        ):
            self.assertEqual(ollama.recommended_thinking_model(), "qwen3:14b")

    def test_16gb_picks_qwen3_8b(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", return_value=_fake_vmem(16 * 1024**3)
        ):
            self.assertEqual(ollama.recommended_thinking_model(), "qwen3:8b")

    def test_8gb_picks_qwen3_4b(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", return_value=_fake_vmem(8 * 1024**3)
        ):
            self.assertEqual(ollama.recommended_thinking_model(), "qwen3:4b")

    def test_psutil_failure_falls_back_without_raising(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(ollama.recommended_thinking_model(), "qwen3:4b")

    def test_tier_table_mirrors_recommendation_tiers_ram_boundaries(self):
        from magic_video_editor.api import ollama

        thinking_bounds = sorted(t["min_ram_gb"] for t in ollama._THINKING_MODEL_TIERS)
        default_bounds = sorted(t["min_ram_gb"] for t in ollama._RECOMMENDATION_TIERS)
        self.assertEqual(thinking_bounds, default_bounds)
        self.assertEqual(thinking_bounds, [0, 16, 24, 48])


class ModelInstalledAndFitsTests(unittest.TestCase):
    """model_installed_and_fits() -- non-raising twin of preflight_check_models
    for a single model, used by ordering.py's degrade ladder."""

    def test_true_for_installed_model_that_fits_ram(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
            ),
            mock.patch.object(
                ollama.httpx, "get", return_value=_fake_tags_response([("qwen3:32b", 20.0)])
            ),
        ):
            self.assertTrue(ollama.model_installed_and_fits("qwen3:32b"))

    def test_false_for_not_installed_model(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
            ),
            mock.patch.object(ollama.httpx, "get", return_value=_fake_tags_response([])),
        ):
            self.assertFalse(ollama.model_installed_and_fits("qwen3:32b"))

    def test_false_for_too_big_installed_model(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(8 * 1024**3)
            ),
            mock.patch.object(
                ollama.httpx, "get", return_value=_fake_tags_response([("qwen3:32b", 20.0)])
            ),
        ):
            self.assertFalse(ollama.model_installed_and_fits("qwen3:32b"))

    def test_false_when_ollama_unreachable_never_raises(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(16 * 1024**3)
            ),
            mock.patch.object(
                ollama.httpx, "get", side_effect=ConnectionError("dead port")
            ),
        ):
            self.assertFalse(ollama.model_installed_and_fits("qwen3:8b"))

    def test_false_for_empty_model_name(self):
        from magic_video_editor.api import ollama

        self.assertFalse(ollama.model_installed_and_fits(""))
        self.assertFalse(ollama.model_installed_and_fits(None))


class RecommendedInstalledThinkingModelTests(unittest.TestCase):
    """recommended_installed_thinking_model() (2026-07-26 live-verification
    fix, gate 3c): scans _THINKING_MODEL_TIERS from this machine's own tier
    DOWN to the smallest (best, then optimal, at each tier) for the first
    candidate that's actually installed and fits -- instead of only ever
    trying this machine's own tier's "best" pick. Verified live on a real
    48GB machine where deepseek-r1:14b (installed) is the 24GB tier's
    "optimal" pick but no qwen3:* model is installed -- the OLD ladder gave
    up entirely in that case; this is the regression test for it."""

    def test_top_tier_best_pick_installed_is_used(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
            ),
            mock.patch.object(
                ollama, "model_installed_and_fits", side_effect=lambda n: n == "qwen3:32b"
            ),
        ):
            self.assertEqual(ollama.recommended_installed_thinking_model(), "qwen3:32b")

    def test_tier_pick_absent_but_lower_tier_thinking_model_installed(self):
        """The exact real-machine case this fix targets: 48GB RAM, tier picks
        qwen3:32b/qwen3:14b (48GB tier) and qwen3:8b (16GB tier) all absent,
        but deepseek-r1:14b (the 24GB tier's "optimal" pick) IS installed --
        the scan must walk down past the top tier's two absent picks and
        return deepseek-r1:14b rather than giving up."""
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
            ),
            mock.patch.object(
                ollama,
                "model_installed_and_fits",
                side_effect=lambda n: n == "deepseek-r1:14b",
            ),
        ):
            self.assertEqual(
                ollama.recommended_installed_thinking_model(), "deepseek-r1:14b"
            )

    def test_never_scans_above_this_machines_own_tier(self):
        """A 16GB machine must never be offered the 24GB/48GB tiers' picks,
        even if one happens to be "installed" per the mock -- only tiers at
        or below this machine's own RAM tier are ever scanned."""
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(16 * 1024**3)
            ),
            mock.patch.object(ollama, "model_installed_and_fits", return_value=True),
        ):
            self.assertEqual(ollama.recommended_installed_thinking_model(), "qwen3:8b")

    def test_nothing_in_the_whole_ladder_installed_returns_none(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
            ),
            mock.patch.object(ollama, "model_installed_and_fits", return_value=False),
        ):
            self.assertIsNone(ollama.recommended_installed_thinking_model())

    def test_never_hangs_or_raises_when_ollama_unreachable(self):
        from magic_video_editor.api import ollama

        with (
            mock.patch.object(
                ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
            ),
            mock.patch.object(
                ollama.httpx, "get", side_effect=ConnectionError("dead port")
            ),
        ):
            self.assertIsNone(ollama.recommended_installed_thinking_model())

    def test_psutil_failure_returns_none_without_raising(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(ollama.recommended_installed_thinking_model())


class ResolveOrderingModelDegradeLadderTests(unittest.TestCase):
    """pipeline/ordering._resolve_ordering_model(): thinking-ready -> use it,
    full-text attempted; not ready -> fall back to the task model, digest
    forced. Delegates to api.ollama.recommended_installed_thinking_model()
    (the tier-scanning fix, 2026-07-26), and never raises even if that
    lookup itself blows up."""

    def test_thinking_model_ready_is_used_with_full_text_allowed(self):
        from magic_video_editor.pipeline import ordering

        with mock.patch(
            "magic_video_editor.api.ollama.recommended_installed_thinking_model",
            return_value="deepseek-r1:14b",
        ):
            model_name, force_digest = ordering._resolve_ordering_model("qwen2.5:14b")
        self.assertEqual(model_name, "deepseek-r1:14b")
        self.assertFalse(force_digest)

    def test_thinking_model_not_ready_degrades_to_task_model_with_forced_digest(self):
        from magic_video_editor.pipeline import ordering

        with mock.patch(
            "magic_video_editor.api.ollama.recommended_installed_thinking_model",
            return_value=None,
        ):
            model_name, force_digest = ordering._resolve_ordering_model("qwen2.5:14b")
        self.assertEqual(model_name, "qwen2.5:14b")
        self.assertTrue(force_digest)

    def test_thinking_tier_lookup_exception_degrades_without_raising(self):
        from magic_video_editor.pipeline import ordering

        with mock.patch(
            "magic_video_editor.api.ollama.recommended_installed_thinking_model",
            side_effect=RuntimeError("boom"),
        ):
            model_name, force_digest = ordering._resolve_ordering_model("qwen2.5:14b")
        self.assertEqual(model_name, "qwen2.5:14b")
        self.assertTrue(force_digest)


if __name__ == "__main__":
    unittest.main()
