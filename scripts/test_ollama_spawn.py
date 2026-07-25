#!/usr/bin/env python3
"""Unit tests for the system-installed-ollama auto-spawn feature
(magic_video_editor/ollama_manager.py::_system_binary_path /
ensure_ollama()'s new "spawn what's already installed" step).

OWNER ASK (verbatim, translated): "to run this I don't want to have to have
the Ollama app turned on; it should run behind the scenes as a subprocess."
Before this, ensure_ollama() never tried the user's own already-installed
`ollama` binary -- it went straight from "reachable?" to "bundled binary" to
"download from GitHub", so a machine with Ollama.app installed-but-closed
either downloaded a needless duplicate or did nothing useful. This adds a
step in between: shutil.which("ollama") (plus a couple of common install
paths) -> spawn `ollama serve` from it via the SAME tracked-Popen mechanism
the bundled/downloaded path already uses, but WITHOUT forcing OLLAMA_MODELS
to our private app-data dir, so the user's existing pulled models (e.g.
qwen2.5:14b) stay available.

SAFETY: no pytest in this project (see scripts/test_ollama_manager.py for
precedent) -- stdlib unittest. Everything here is mocked: _reachable(),
shutil.which(), and _spawn_binary()/ffmpeg_utils._spawn() are all patched so
NO real `ollama` process is ever launched by this file, per the hard rule
against spawning real ollama in tests/verification. Uses a scratch
config.DATA_DIR (MVE_DATA-style temp dir) like the rest of the suite.

Covers:
  (a) reachable -> mode "system", nothing spawned (no regression).
  (b) not reachable, but which("ollama") finds a system binary -> spawned via
      the tracked mechanism, mode "system-spawned", OLLAMA_MODELS NOT forced.
  (c) not reachable, no system ollama found -> falls through to
      bundled/download exactly as before.
  (d) terminate() tears down a system-spawned child via the shared
      ffmpeg_utils registry.

Usage:
    uv run python scripts/test_ollama_spawn.py
    uv run python scripts/test_ollama_spawn.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SystemBinarySpawnTests(unittest.TestCase):
    """ensure_ollama()'s new preference order, fully mocked -- no real
    process, no real network, no real filesystem probing of the actual
    machine's ollama install."""

    def setUp(self):
        from magic_video_editor import config, ollama_manager

        self.om = ollama_manager
        self.config = config

        self.tmp = tempfile.TemporaryDirectory(prefix="mve-ollama-spawn-test-")
        self.addCleanup(self.tmp.cleanup)
        self._orig_data_dir = config.DATA_DIR
        self._orig_url = config.OLLAMA_URL
        config.DATA_DIR = Path(self.tmp.name)
        config.OLLAMA_URL = "http://127.0.0.1:8849"  # dead port, never dialed for real

        self.om.terminate()  # reset module state between tests

    def tearDown(self):
        self.om.terminate()
        self.config.DATA_DIR = self._orig_data_dir
        self.config.OLLAMA_URL = self._orig_url

    # -- (a) reachable wins, nothing spawned -------------------------------

    def test_reachable_uses_system_mode_and_spawns_nothing(self):
        with (
            mock.patch.object(self.om, "_reachable", return_value=True),
            mock.patch("shutil.which") as which,
            mock.patch.object(self.om, "_spawn_binary") as spawn,
        ):
            mode = self.om.ensure_ollama()

        self.assertEqual(mode, "system")
        which.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(self.om._proc)

    # -- (b) not reachable, system ollama found -> spawned, no OLLAMA_MODELS -

    def test_unreachable_with_system_binary_spawns_it_without_forcing_models_dir(self):
        fake_system_binary = Path("/usr/local/bin/ollama")
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None

        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch("shutil.which", return_value=str(fake_system_binary)) as which,
            mock.patch.object(self.om, "_spawn_binary", return_value=fake_proc) as spawn,
            mock.patch.object(self.om, "bundled_binary_path") as bundled,
            mock.patch.object(self.om, "_download_and_spawn") as dl,
        ):
            mode = self.om.ensure_ollama()

        which.assert_called_once_with("ollama")
        spawn.assert_called_once_with(fake_system_binary, set_models_dir=False)
        bundled.assert_not_called()
        dl.assert_not_called()
        self.assertEqual(mode, "system-spawned")
        self.assertIs(self.om._proc, fake_proc)

    def test_system_binary_lookup_falls_back_to_common_paths_when_which_empty(self):
        # which("ollama") returns nothing (e.g. a packaged .app launched from
        # Finder with a stripped-down PATH), but one of the well-known
        # Homebrew install locations exists on disk.
        homebrew_path = self.config.DATA_DIR / "fake-homebrew-bin" / "ollama"
        homebrew_path.parent.mkdir(parents=True, exist_ok=True)
        homebrew_path.write_text("#!/bin/sh\necho fake\n")

        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None

        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch("shutil.which", return_value=None),
            mock.patch.object(
                self.om, "_EXTRA_SYSTEM_OLLAMA_PATHS", (homebrew_path,)
            ),
            mock.patch.object(self.om, "_spawn_binary", return_value=fake_proc) as spawn,
        ):
            mode = self.om.ensure_ollama()

        spawn.assert_called_once_with(homebrew_path, set_models_dir=False)
        self.assertEqual(mode, "system-spawned")

    def test_system_binary_spawn_failure_falls_back_to_bundled(self):
        fake_system_binary = Path("/opt/homebrew/bin/ollama")
        fake_bundled_binary = Path("/tmp/definitely-not-real/vendor/ollama")
        fake_bundled_proc = mock.Mock()

        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch("shutil.which", return_value=str(fake_system_binary)),
            mock.patch.object(self.om, "bundled_binary_path", return_value=fake_bundled_binary),
            mock.patch.object(
                self.om,
                "_spawn_binary",
                side_effect=[None, fake_bundled_proc],  # system fails, bundled succeeds
            ) as spawn,
        ):
            mode = self.om.ensure_ollama()

        self.assertEqual(spawn.call_count, 2)
        spawn.assert_any_call(fake_system_binary, set_models_dir=False)
        spawn.assert_any_call(fake_bundled_binary)
        self.assertEqual(mode, "bundled")

    # -- (c) no system ollama at all -> unchanged bundled/download path ----

    def test_no_system_binary_falls_through_to_bundled(self):
        # NOTE: mocks _system_binary_path() directly (not just shutil.which)
        # so this is immune to whatever `ollama` binaries happen to actually
        # exist at /usr/local/bin or /opt/homebrew/bin on the machine running
        # the suite -- otherwise a real dev laptop with Homebrew ollama
        # installed would make this test spawn (or try to spawn) the real
        # thing, which is exactly what this file must never do.
        fake_bundled_binary = Path("/tmp/definitely-not-real/vendor/ollama")
        fake_proc = mock.Mock()

        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch.object(self.om, "_system_binary_path", return_value=None),
            mock.patch.object(self.om, "bundled_binary_path", return_value=fake_bundled_binary),
            mock.patch.object(self.om, "_spawn_binary", return_value=fake_proc) as spawn,
        ):
            mode = self.om.ensure_ollama()

        spawn.assert_called_once_with(fake_bundled_binary)
        self.assertEqual(mode, "bundled")

    def test_no_system_binary_and_no_bundled_falls_through_to_download(self):
        fake_proc = mock.Mock()

        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch.object(self.om, "_system_binary_path", return_value=None),
            mock.patch.object(self.om, "bundled_binary_path", return_value=None),
            mock.patch.object(self.om, "_download_and_spawn", return_value=fake_proc) as dl,
        ):
            mode = self.om.ensure_ollama()

        dl.assert_called_once()
        self.assertEqual(mode, "downloaded")

    # -- (d) terminate() cleans up a system-spawned child -------------------

    def test_terminate_cleans_up_system_spawned_child_via_shared_registry(self):
        from magic_video_editor import ffmpeg_utils

        fake_system_binary = Path("/usr/local/bin/ollama")
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 999999  # never a real pid

        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch("shutil.which", return_value=str(fake_system_binary)),
            mock.patch.object(self.om, "_spawn_binary", return_value=fake_proc),
        ):
            mode = self.om.ensure_ollama()
        self.assertEqual(mode, "system-spawned")

        with mock.patch.object(ffmpeg_utils, "_unregister") as unregister:
            self.om.terminate()

        # terminate() unregisters from the shared ffmpeg_utils registry (the
        # same one SIGTERM/atexit/window-close already tear down) and calls
        # terminate()/wait() on the mocked Popen -- never a real spawn.
        unregister.assert_called_once_with(fake_proc)
        fake_proc.terminate.assert_called_once()
        self.assertIsNone(self.om._proc)
        self.assertEqual(self.om.current_mode(), "unreachable")


if __name__ == "__main__":
    unittest.main()
