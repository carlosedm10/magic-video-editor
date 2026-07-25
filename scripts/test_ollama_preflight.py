#!/usr/bin/env python3
"""Unit tests for the hardware-aware default model + LLM preflight guard
(root-cause fix, 2026-07-25 -- see docs/PLATFORM-SPEC.md vNext section).

Two user reports turned out to share one root cause: a static
DEFAULT_MODEL = "qwen2.5:14b" (~9GB, needs ~10-12GB RAM) was seeded into
settings.json on EVERY first run regardless of hardware, and no code ever
checked the resolved model was installed or fit RAM before a pipeline stage
ran it. On a clean 8GB M2 this meant either a raw ollama error (model never
installed) or, worse, ollama loading a model that can't fit -> the Mac swaps
to death and the whole app hangs.

Covers:
  - magic_video_editor.settings.load(): first-run default_model is seeded
    from the hardware recommendation (api.ollama.recommended_default_model,
    mocked here) instead of the static qwen2.5:14b; an EXISTING settings.json
    is never overwritten (the user's saved choice always wins).
  - magic_video_editor.api.ollama.recommended_default_model(): 8GB machine
    picks llama3.2:3b (the RAM tier table's bottom tier), 48GB picks the big
    qwen2.5:32b.
  - magic_video_editor.api.ollama.preflight_check_models(): raises a single
    clear RuntimeError for a too-big installed model and for a not-installed
    model; passes silently for an installed model that fits RAM.

No pytest in this project's dependency set (see pyproject.toml) -- stdlib
unittest, same spirit as scripts/test_ollama_manager.py / test_updater.py.
Every test overrides config.DATA_DIR to a tempdir (never the real
~/Library/Application Support/Magic Video Editor) and mocks psutil/httpx --
no real ollama is spawned or contacted.

Usage:
    uv run python scripts/test_ollama_preflight.py
    uv run python scripts/test_ollama_preflight.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fake_vmem(total_bytes: float):
    """Minimal stand-in for psutil.virtual_memory()'s return value -- only
    `.total` is read by the code under test."""
    return mock.Mock(total=total_bytes)


def _fake_tags_response(models: list[tuple[str, float]]):
    """Build a mocked httpx.Response-like object for GET /api/tags, where
    `models` is a list of (name, size_gb)."""
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json.return_value = {
        "models": [
            {"name": name, "size": int(size_gb * (1024**3))} for name, size_gb in models
        ]
    }
    return resp


class HardwareAwareFirstRunDefaultTests(unittest.TestCase):
    """settings.py: first-run default_model comes from the hardware
    recommendation, not the static "qwen2.5:14b"; existing settings.json is
    left untouched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mve-preflight-test-")
        self.addCleanup(self.tmp.cleanup)

        from magic_video_editor import config, settings

        self.config = config
        self.settings = settings
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        self.config.DATA_DIR = self._orig_data_dir

    def test_first_run_8gb_machine_defaults_to_small_model(self):
        with mock.patch("psutil.virtual_memory", return_value=_fake_vmem(8 * 1024**3)):
            data = self.settings.load()
        self.assertEqual(data["default_model"], "llama3.2:3b")
        # And it was actually persisted, not just returned in-memory.
        saved = json.loads((self.config.DATA_DIR / "settings.json").read_text())
        self.assertEqual(saved["default_model"], "llama3.2:3b")

    def test_first_run_48gb_machine_defaults_to_big_model(self):
        with mock.patch("psutil.virtual_memory", return_value=_fake_vmem(48 * 1024**3)):
            data = self.settings.load()
        self.assertEqual(data["default_model"], "qwen2.5:32b")

    def test_existing_settings_json_is_not_overwritten(self):
        (self.config.DATA_DIR).mkdir(parents=True, exist_ok=True)
        (self.config.DATA_DIR / "settings.json").write_text(
            json.dumps({"default_model": "mistral:7b"})
        )
        with mock.patch("psutil.virtual_memory", return_value=_fake_vmem(8 * 1024**3)):
            data = self.settings.load()
        self.assertEqual(data["default_model"], "mistral:7b")

    def test_recommendation_helper_failure_falls_back_to_static_default(self):
        with mock.patch("psutil.virtual_memory", side_effect=RuntimeError("boom")):
            data = self.settings.load()
        self.assertEqual(data["default_model"], self.settings.DEFAULT_MODEL)


class RecommendedDefaultModelTests(unittest.TestCase):
    """api/ollama.py: recommended_default_model() directly, same RAM tiers
    /api/ollama/recommendation uses."""

    def test_8gb_picks_llama_3b(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", return_value=_fake_vmem(8 * 1024**3)
        ):
            self.assertEqual(ollama.recommended_default_model(), "llama3.2:3b")

    def test_48gb_picks_qwen32b(self):
        from magic_video_editor.api import ollama

        with mock.patch.object(
            ollama.psutil, "virtual_memory", return_value=_fake_vmem(48 * 1024**3)
        ):
            self.assertEqual(ollama.recommended_default_model(), "qwen2.5:32b")


class PreflightCheckModelsTests(unittest.TestCase):
    """api/ollama.py: preflight_check_models() -- the runtime guard called
    from api/pipeline.py's _preflight_stage before any LLM stage runs."""

    def setUp(self):
        from magic_video_editor.api import ollama

        self.ollama = ollama

    def test_passes_for_installed_model_that_fits_ram(self):
        with (
            mock.patch.object(
                self.ollama.psutil, "virtual_memory", return_value=_fake_vmem(16 * 1024**3)
            ),
            mock.patch.object(
                self.ollama.httpx,
                "get",
                return_value=_fake_tags_response([("llama3.2:3b", 2.0)]),
            ),
        ):
            self.ollama.preflight_check_models(["llama3.2:3b"])  # must not raise

    def test_raises_for_too_big_installed_model(self):
        with (
            mock.patch.object(
                self.ollama.psutil, "virtual_memory", return_value=_fake_vmem(8 * 1024**3)
            ),
            mock.patch.object(
                self.ollama.httpx,
                "get",
                return_value=_fake_tags_response([("qwen2.5:14b", 9.0)]),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.ollama.preflight_check_models(["qwen2.5:14b"])
        msg = str(ctx.exception)
        self.assertIn("qwen2.5:14b", msg)
        self.assertIn("RAM", msg)
        # The clear, actionable error names a fitting alternative.
        self.assertIn("Ajustes", msg)

    def test_raises_for_model_not_installed(self):
        with (
            mock.patch.object(
                self.ollama.psutil, "virtual_memory", return_value=_fake_vmem(16 * 1024**3)
            ),
            mock.patch.object(
                self.ollama.httpx, "get", return_value=_fake_tags_response([])
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.ollama.preflight_check_models(["ghost-model:1b"])
        msg = str(ctx.exception)
        self.assertIn("ghost-model:1b", msg)
        self.assertIn("no está instalado", msg)

    def test_raises_clear_error_when_ollama_unreachable(self):
        with (
            mock.patch.object(
                self.ollama.psutil, "virtual_memory", return_value=_fake_vmem(16 * 1024**3)
            ),
            mock.patch.object(
                self.ollama.httpx, "get", side_effect=ConnectionError("dead port")
            ),
        ):
            with self.assertRaises(RuntimeError):
                self.ollama.preflight_check_models(["llama3.2:3b"])

    def test_noop_for_empty_model_list(self):
        # Must not even try to reach ollama when there's nothing to check
        # (e.g. a non-LLM stage like ingest/sync/render/transcribe).
        with mock.patch.object(self.ollama.httpx, "get") as get:
            self.ollama.preflight_check_models([])
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
