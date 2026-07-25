#!/usr/bin/env python3
"""Unit + live tests for the Ollama auto-spawn field-bug fix
(magic_video_editor/ollama_manager.py, magic_video_editor/app.py,
magic_video_editor/server.py).

No pytest in this project's dependency set (see pyproject.toml) -- stdlib
unittest, same spirit as scripts/test_updater.py.

Covers:
  - bundled_binary_path() path resolution against a FAKE PyInstaller onedir
    .app bundle layout (Contents/Frameworks via sys._MEIPASS, and the
    Contents/MacOS/<exe> -> Contents/Frameworks fallback computed from
    sys.executable) as well as the real dev checkout layout.
  - ensure_ollama()'s system/bundled/downloaded/unreachable state machine
    (mocked reachability -- no real process spawned in these).
  - A LIVE spawn of the real vendored packaging/vendor/ollama/ollama binary:
    point OLLAMA_URL at a dead port, let ensure_ollama() spawn the bundled
    binary for real, confirm /api/version answers through it, then
    terminate() and confirm no orphaned process is left running. This is a
    normal CLI child process (`ollama serve`), not the packaged .app the
    corporate EDR flags -- safe to run here.
  - The self-provisioning download fallback, with the vendored binary
    temporarily renamed out of the way. The real download function is
    mocked (api.github.com is unreachable from this network right now --
    verified with `curl -m 4 https://api.github.com/...` -> exit failure /
    "000" before writing this test) to instead materialize the already-
    fetched vendored binary at the download destination, so the rest of the
    real code path (jobs.py tracking, mode transition, spawn, health
    reporting) is still exercised for real.

Usage:
    uv run python scripts/test_ollama_manager.py
    uv run python scripts/test_ollama_manager.py -v
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_VENDOR_BINARY = REPO_ROOT / "packaging" / "vendor" / "ollama" / "ollama"


def _make_fake_binary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho fake ollama\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class BundleRootResolutionTests(unittest.TestCase):
    """bundled_binary_path() against fabricated bundle layouts -- no real
    PyInstaller build needed, just the sys.* signals it reads."""

    def setUp(self):
        from magic_video_editor import ollama_manager

        self.om = ollama_manager
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

        # Snapshot/restore the sys.* attributes we poke.
        self._orig_meipass = getattr(sys, "_MEIPASS", None)
        self._orig_frozen = getattr(sys, "frozen", None)
        self._orig_executable = sys.executable
        self.addCleanup(self._restore_sys)

    def _restore_sys(self):
        if self._orig_meipass is None:
            if hasattr(sys, "_MEIPASS"):
                del sys._MEIPASS
        else:
            sys._MEIPASS = self._orig_meipass
        if self._orig_frozen is None:
            if hasattr(sys, "frozen"):
                del sys.frozen
        else:
            sys.frozen = self._orig_frozen
        sys.executable = self._orig_executable

    def test_dev_checkout_layout_finds_real_vendored_binary(self):
        # No _MEIPASS, not frozen -- exactly a bare `uv run mve-server`
        # checkout. The real repo already has packaging/vendor/ollama/ollama
        # fetched (packaging/fetch_ollama.sh has been run in this repo).
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS
        if hasattr(sys, "frozen"):
            del sys.frozen
        msg = "expected a fetched vendor binary for this test"
        self.assertTrue(REAL_VENDOR_BINARY.exists(), msg)
        found = self.om.bundled_binary_path()
        self.assertEqual(found, REAL_VENDOR_BINARY)

    def test_onedir_bundle_layout_via_meipass(self):
        # Mirrors what an actual `pyinstaller packaging/mve.spec` onedir
        # macOS BUNDLE produces: datas land under Contents/Frameworks, and
        # sys._MEIPASS points straight at it (verified empirically against
        # a real minimal PyInstaller onedir build during development of
        # this fix).
        app_root = self.tmp_path / "Magic Video Editor.app"
        frameworks = app_root / "Contents" / "Frameworks"
        _make_fake_binary(frameworks / "packaging" / "vendor" / "ollama" / "ollama")

        sys._MEIPASS = str(frameworks)
        sys.frozen = True
        sys.executable = str(app_root / "Contents" / "MacOS" / "Magic Video Editor")

        found = self.om.bundled_binary_path()
        self.assertEqual(
            found.resolve(), (frameworks / "packaging" / "vendor" / "ollama" / "ollama").resolve()
        )

    def test_onedir_bundle_layout_via_executable_fallback_without_meipass(self):
        # Defensive second path: even if _MEIPASS were ever wrong/absent in
        # a frozen build, computing Contents/Frameworks straight from
        # sys.executable's real location must still find the binary.
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS
        app_root = self.tmp_path / "Magic Video Editor.app"
        frameworks = app_root / "Contents" / "Frameworks"
        _make_fake_binary(frameworks / "packaging" / "vendor" / "ollama" / "ollama")

        sys.frozen = True
        sys.executable = str(app_root / "Contents" / "MacOS" / "Magic Video Editor")

        found = self.om.bundled_binary_path()
        self.assertEqual(
            found.resolve(), (frameworks / "packaging" / "vendor" / "ollama" / "ollama").resolve()
        )

    def test_missing_everywhere_returns_none(self):
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS
        sys.frozen = True
        app_root = self.tmp_path / "Empty.app"
        sys.executable = str(app_root / "Contents" / "MacOS" / "Empty")

        with mock.patch.object(
            self.om, "_candidate_bundle_roots", return_value=[self.tmp_path / "nowhere"]
        ):
            self.assertIsNone(self.om.bundled_binary_path())


class EnsureOllamaModeTests(unittest.TestCase):
    """State machine logic with reachability/spawn mocked -- no real
    process, no real network."""

    def setUp(self):
        from magic_video_editor import ollama_manager

        self.om = ollama_manager
        self.om.terminate()  # reset module state between tests

    def tearDown(self):
        self.om.terminate()

    def test_system_reachable_wins(self):
        with mock.patch.object(self.om, "_reachable", return_value=True):
            mode = self.om.ensure_ollama()
        self.assertEqual(mode, "system")

    def test_falls_back_to_download_when_no_bundled_binary(self):
        # _system_binary_path() is mocked to None here (not just left to run
        # for real) -- otherwise, on a machine that actually has a system
        # `ollama` install, this would fall into the new system-binary-spawn
        # step and try to launch a REAL `ollama serve` child, which tests
        # must never do.
        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch.object(self.om, "_system_binary_path", return_value=None),
            mock.patch.object(self.om, "bundled_binary_path", return_value=None),
            mock.patch.object(self.om, "_download_and_spawn", return_value=None) as dl,
        ):
            mode = self.om.ensure_ollama()
        dl.assert_called_once()
        self.assertEqual(mode, "unreachable")

    def test_bundled_spawn_failure_falls_back_to_download(self):
        fake_binary = Path("/tmp/definitely-not-a-real-path/ollama")
        fake_proc = mock.Mock()
        with (
            mock.patch.object(self.om, "_reachable", return_value=False),
            mock.patch.object(self.om, "_system_binary_path", return_value=None),
            mock.patch.object(self.om, "bundled_binary_path", return_value=fake_binary),
            mock.patch.object(self.om, "_spawn_binary", return_value=None) as spawn,
            mock.patch.object(self.om, "_download_and_spawn", return_value=fake_proc) as dl,
        ):
            mode = self.om.ensure_ollama()
        spawn.assert_called_once_with(fake_binary)  # the bundled attempt
        dl.assert_called_once()  # falls through to self-provisioning
        self.assertEqual(mode, "downloaded")


class LiveBundledSpawnTest(unittest.TestCase):
    """Spawns the REAL vendored ollama binary as a child process (plain
    `ollama serve` -- the same thing `make models` / dev workflows already
    do; not the packaged .app the EDR flags). Uses MVE_DATA scratch dirs
    and a dead-port OLLAMA_URL per the project's safety rules."""

    @classmethod
    def setUpClass(cls):
        if not REAL_VENDOR_BINARY.exists():
            raise unittest.SkipTest("packaging/vendor/ollama/ollama not fetched in this checkout")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mve-ollama-live-test-")
        self.addCleanup(self.tmp.cleanup)

        from magic_video_editor import config, ollama_manager

        self.om = ollama_manager
        self.config = config
        self._orig_data_dir = config.DATA_DIR
        self._orig_url = config.OLLAMA_URL
        config.DATA_DIR = Path(self.tmp.name)
        config.OLLAMA_URL = "http://127.0.0.1:8848"  # dead port, in the 8846-8848 test range
        self.om.terminate()

    def tearDown(self):
        self.om.terminate()
        self.config.DATA_DIR = self._orig_data_dir
        self.config.OLLAMA_URL = self._orig_url

    def test_spawns_real_binary_and_cleans_up(self):
        import httpx

        mode = self.om.ensure_ollama()
        self.assertEqual(mode, "bundled", "expected the real vendored binary to spawn and answer")

        proc = self.om._proc
        self.assertIsNotNone(proc)
        self.assertIsNone(proc.poll(), "child should still be alive right after a successful spawn")

        res = httpx.get(f"{self.config.OLLAMA_URL}/api/version", timeout=3)
        self.assertEqual(res.status_code, 200)

        pid = proc.pid
        self.om.terminate()

        # No orphan left behind.
        self.assertIsNotNone(proc.poll(), "child must be reaped after terminate()")
        self.assertFalse(_pid_alive(pid), f"pid {pid} still alive after terminate()")


class DownloadFallbackTest(unittest.TestCase):
    """Renames the real vendored binary out of the way so bundled_binary_path()
    genuinely finds nothing, then exercises the self-provisioning download
    path. The network call itself is mocked (api.github.com is unreachable
    from this machine's current network -- confirmed with a 4s-timeout curl
    before writing this test, so this substitutes a local copy of the
    already-fetched vendored binary instead of hitting the real API) but
    everything downstream (jobs.py job tracking, mode transition, spawning
    the "downloaded" binary, health reporting) runs for real."""

    @classmethod
    def setUpClass(cls):
        if not REAL_VENDOR_BINARY.exists():
            raise unittest.SkipTest("packaging/vendor/ollama/ollama not fetched in this checkout")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mve-ollama-dlfallback-test-")
        self.addCleanup(self.tmp.cleanup)

        from magic_video_editor import config, ollama_manager

        self.om = ollama_manager
        self.config = config
        self._orig_data_dir = config.DATA_DIR
        self._orig_url = config.OLLAMA_URL
        config.DATA_DIR = Path(self.tmp.name)
        config.OLLAMA_URL = "http://127.0.0.1:8847"  # dead port
        self.om.terminate()

        # Rename the real vendor binary out of the way for the duration of
        # this test so bundled_binary_path() really returns None.
        self._vendor_backup = REAL_VENDOR_BINARY.with_suffix(".bak-test")
        shutil.move(str(REAL_VENDOR_BINARY), str(self._vendor_backup))
        self.addCleanup(self._restore_vendor_binary)

    def _restore_vendor_binary(self):
        if self._vendor_backup.exists():
            shutil.move(str(self._vendor_backup), str(REAL_VENDOR_BINARY))

    def tearDown(self):
        self.om.terminate()
        self.config.DATA_DIR = self._orig_data_dir
        self.config.OLLAMA_URL = self._orig_url

    def test_download_fallback_spawns_and_reports_downloaded(self):
        self.assertIsNone(self.om.bundled_binary_path(), "vendor binary should be renamed away")

        def _fake_download(log):
            log("looking up latest ollama release (mocked -- api.github.com unreachable here)")
            dest = self.om._downloaded_binary_path()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._vendor_backup, dest)
            os.chmod(dest, dest.stat().st_mode | stat.S_IEXEC)
            log.progress(1.0)
            log("done (mocked)")

        with mock.patch.object(self.om, "_download_ollama_binary", side_effect=_fake_download):
            mode = self.om.ensure_ollama()

        self.assertEqual(mode, "downloaded")
        self.assertTrue(self.om._downloaded_binary_path().exists())
        self.assertIsNotNone(self.om.download_job_id())

        from magic_video_editor import jobs as jobs_module

        job = jobs_module.get(self.om.download_job_id())
        self.assertEqual(job["status"], "done")

        pid = self.om._proc.pid
        self.om.terminate()
        self.assertFalse(_pid_alive(pid))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
