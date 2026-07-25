#!/usr/bin/env python3
"""Unit tests for the auto-update first-relaunch ffprobe self-heal
(magic_video_editor/ffmpeg_utils.py).

FIELD BUG this guards against: after the app auto-updates and relaunches
itself, ffprobe fails on that first relaunch only -- fully quitting and
reopening the app fixes it. Suspected causes: (a) a Gatekeeper first-launch
assessment / App Translocation race on the just-swapped bundle (handled in
packaging/update_helper.sh, not testable here without a real .app -- see
that script's header), and (b) ffmpeg_bin()/ffprobe_bin()/
export_binaries_to_path() caching or reusing a resolved path/PATH shim
whose target pointed into the PREVIOUS bundle and is now stale/dangling.
This file exercises (b): every resolution here MUST self-heal (re-resolve/
recreate) instead of trusting a cached-but-now-broken path.

EDR SAFETY (Cortex XDR has killed this dev session twice over packaging
activity -- see docs/PLATFORM-SPEC.md's "no local packaging activity"
rule): this file NEVER builds with PyInstaller, NEVER touches dist/, NEVER
spawns the real vendored ffmpeg/ffprobe binary, and NEVER simulates the
real app-bundle swap. Every "binary" used below is a HARMLESS throwaway
`#!/bin/sh` stand-in created fresh under a scratch temp dir for this test
run only, and the fake ffmpeg_bin()/ffprobe_bin() candidate resolution is
monkeypatched -- the real resolution chain (vendor/static-ffmpeg/system) is
never exercised or executed here.

No pytest in this project's dependency set -- stdlib unittest, same
convention as scripts/test_updater.py.

Usage:
    uv run python scripts/test_binaries_selfheal.py
    uv run python scripts/test_binaries_selfheal.py -v
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

from magic_video_editor import ffmpeg_utils  # noqa: E402


def _make_fake_bin(path: Path) -> Path:
    """A harmless `#!/bin/sh` stand-in executable -- never a real ffmpeg/
    ffprobe binary, never spawned by these tests (only stat()'d for
    exists()/X_OK, per the EDR rule)."""
    path.write_text("#!/bin/sh\necho fake\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class SelfHealTestCase(unittest.TestCase):
    """Common scratch-dir setup/teardown + full cache reset around every
    test, so tests can't leak cached state into each other (mirrors the
    module's own _exported_shim_dir reset convention documented in
    ffmpeg_utils.export_binaries_to_path())."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mve-selfheal-test-")
        self.addCleanup(self.tmp.cleanup)
        self.scratch = Path(self.tmp.name)
        self._reset_module_state()
        self.addCleanup(self._reset_module_state)

    def _reset_module_state(self):
        ffmpeg_utils._ffmpeg_bin_cache = None
        ffmpeg_utils._ffprobe_bin_cache = None
        ffmpeg_utils._exported_shim_dir = None
        ffmpeg_utils._supports_ass.cache_clear()
        ffmpeg_utils._bin_works.cache_clear()


class DanglingShimRecreatedTests(SelfHealTestCase):
    """(a) export_binaries_to_path() must RECREATE a dangling PATH shim
    (symlink to a since-removed target) rather than leave it dangling just
    because the shim dir already exists."""

    def test_dangling_shim_is_recreated_not_skipped(self):
        real_ffmpeg = _make_fake_bin(self.scratch / "real_ffmpeg")
        real_ffprobe = _make_fake_bin(self.scratch / "real_ffprobe")
        data_dir = self.scratch / "data"
        data_dir.mkdir()

        with (
            mock.patch.object(ffmpeg_utils, "ffmpeg_bin", return_value=str(real_ffmpeg)),
            mock.patch.object(ffmpeg_utils, "ffprobe_bin", return_value=str(real_ffprobe)),
            mock.patch.object(ffmpeg_utils, "_resolves_on_path", return_value=False),
            mock.patch.object(ffmpeg_utils.config, "DATA_DIR", data_dir),
        ):
            shim_dir_1 = ffmpeg_utils.export_binaries_to_path()
            self.assertIsNotNone(shim_dir_1)
            shim_ffmpeg = Path(shim_dir_1) / "ffmpeg"
            shim_ffprobe = Path(shim_dir_1) / "ffprobe"
            self.assertTrue(shim_ffmpeg.is_symlink())
            self.assertTrue(shim_ffprobe.is_symlink())
            self.assertTrue(shim_ffmpeg.exists())  # resolves through the symlink

            # Simulate the swap: the target the shim points at (the OLD
            # bundle's binary) is gone now -- a dangling symlink, exactly
            # what a stale post-update PATH shim looks like.
            real_ffmpeg.unlink()
            self.assertTrue(shim_ffmpeg.is_symlink())
            self.assertFalse(shim_ffmpeg.exists())  # dangling: symlink present, target gone

            # New target for the (post-swap) resolution.
            new_real_ffmpeg = _make_fake_bin(self.scratch / "new_real_ffmpeg")
            with mock.patch.object(ffmpeg_utils, "ffmpeg_bin", return_value=str(new_real_ffmpeg)):
                shim_dir_2 = ffmpeg_utils.export_binaries_to_path()

            self.assertEqual(shim_dir_1, shim_dir_2)
            self.assertTrue(
                shim_ffmpeg.exists(), "dangling shim must be recreated, not left dangling"
            )
            self.assertEqual(Path(os.readlink(shim_ffmpeg)).resolve(), new_real_ffmpeg.resolve())

    def test_no_shim_needed_shortcut_is_revalidated(self):
        """The `_exported_shim_dir == ""` ("no shim needed") shortcut must
        also be re-checked, not trusted forever -- if the bare command no
        longer matches the resolved binary after a swap, a shim must still
        get created instead of silently returning None."""
        real_ffmpeg = _make_fake_bin(self.scratch / "real_ffmpeg")
        real_ffprobe = _make_fake_bin(self.scratch / "real_ffprobe")
        data_dir = self.scratch / "data"
        data_dir.mkdir()

        with (
            mock.patch.object(ffmpeg_utils, "ffmpeg_bin", return_value=str(real_ffmpeg)),
            mock.patch.object(ffmpeg_utils, "ffprobe_bin", return_value=str(real_ffprobe)),
            mock.patch.object(ffmpeg_utils, "_resolves_on_path", return_value=True),
            mock.patch.object(ffmpeg_utils.config, "DATA_DIR", data_dir),
        ):
            result = ffmpeg_utils.export_binaries_to_path()
            self.assertIsNone(result)
            self.assertEqual(ffmpeg_utils._exported_shim_dir, "")

            # Now the bare command no longer matches (swap happened).
            with mock.patch.object(ffmpeg_utils, "_resolves_on_path", return_value=False):
                result2 = ffmpeg_utils.export_binaries_to_path()

        self.assertIsNotNone(result2, "must create a shim once the bare command stops matching")


class StaleCacheReResolvesTests(SelfHealTestCase):
    """(b) ffprobe_bin()/ffmpeg_bin() must never return a non-existent/
    non-executable cached path -- they must re-resolve instead."""

    def test_ffprobe_bin_does_not_return_deleted_cached_path(self):
        fake = _make_fake_bin(self.scratch / "ffprobe_v1")
        with mock.patch.object(ffmpeg_utils, "_resolve_ffprobe_bin", return_value=str(fake)):
            first = ffmpeg_utils.ffprobe_bin()
        self.assertEqual(first, str(fake))

        # Simulate the update swap removing the old bundle's binary.
        fake.unlink()

        fake2 = _make_fake_bin(self.scratch / "ffprobe_v2")
        with mock.patch.object(ffmpeg_utils, "_resolve_ffprobe_bin", return_value=str(fake2)):
            second = ffmpeg_utils.ffprobe_bin()

        self.assertEqual(second, str(fake2))
        self.assertNotEqual(second, str(fake), "must not keep returning the deleted cached path")

    def test_ffmpeg_bin_does_not_return_non_executable_cached_path(self):
        fake = self.scratch / "ffmpeg_v1"
        fake.write_text("#!/bin/sh\necho fake\n")  # NOT chmod +x -- non-executable on purpose
        with mock.patch.object(ffmpeg_utils, "_resolve_ffmpeg_bin", return_value=str(fake)):
            first = ffmpeg_utils.ffmpeg_bin()
        self.assertEqual(first, str(fake))

        # ffmpeg_bin() should treat a non-executable cached path as unusable
        # immediately, without needing anything external to change.
        fake2 = _make_fake_bin(self.scratch / "ffmpeg_v2")
        with mock.patch.object(ffmpeg_utils, "_resolve_ffmpeg_bin", return_value=str(fake2)):
            second = ffmpeg_utils.ffmpeg_bin()

        self.assertEqual(second, str(fake2))

    def test_healthy_cached_path_is_reused_without_reresolving(self):
        """Sanity check the self-heal doesn't defeat the point of caching:
        a still-valid path must NOT trigger re-resolution on every call."""
        fake = _make_fake_bin(self.scratch / "ffprobe_stable")
        resolve_mock = mock.Mock(return_value=str(fake))
        with mock.patch.object(ffmpeg_utils, "_resolve_ffprobe_bin", resolve_mock):
            ffmpeg_utils.ffprobe_bin()
            ffmpeg_utils.ffprobe_bin()
            ffmpeg_utils.ffprobe_bin()
        self.assertEqual(resolve_mock.call_count, 1, "a healthy cached path must not re-resolve")


class StartupSelfHealTests(SelfHealTestCase):
    """(c) the startup self-heal (ensure_binaries_healthy_at_startup(),
    wired into magic_video_editor/app.py's main()) must re-run resolution
    when the health check finds a missing/non-executable binary, and must
    NEVER spawn anything to determine health."""

    def test_reresolves_once_when_unhealthy_then_reports_healthy(self):
        bad = self.scratch / "does_not_exist"  # never created
        good_ffmpeg = _make_fake_bin(self.scratch / "good_ffmpeg")
        good_ffprobe = _make_fake_bin(self.scratch / "good_ffprobe")

        healthy_sequence = [False, True]  # unhealthy first check, healthy after self-heal

        def fake_healthy():
            return healthy_sequence.pop(0) if healthy_sequence else True

        def fake_export():
            # Simulate the re-resolution actually fixing the cached paths.
            ffmpeg_utils._ffmpeg_bin_cache = str(good_ffmpeg)
            ffmpeg_utils._ffprobe_bin_cache = str(good_ffprobe)
            return None

        with (
            mock.patch.object(ffmpeg_utils, "binaries_healthy", side_effect=fake_healthy),
            mock.patch.object(
                ffmpeg_utils, "invalidate_binary_caches"
            ) as invalidate_mock,
            mock.patch.object(
                ffmpeg_utils, "export_binaries_to_path", side_effect=fake_export
            ) as export_mock,
        ):
            result = ffmpeg_utils.ensure_binaries_healthy_at_startup()

        self.assertTrue(result)
        invalidate_mock.assert_called_once()
        export_mock.assert_called_once()
        self.assertEqual(str(bad), str(bad))  # `bad` intentionally never touched/spawned

    def test_healthy_at_startup_is_a_no_op(self):
        with (
            mock.patch.object(ffmpeg_utils, "binaries_healthy", return_value=True),
            mock.patch.object(ffmpeg_utils, "invalidate_binary_caches") as invalidate_mock,
            mock.patch.object(ffmpeg_utils, "export_binaries_to_path") as export_mock,
        ):
            result = ffmpeg_utils.ensure_binaries_healthy_at_startup()

        self.assertTrue(result)
        invalidate_mock.assert_not_called()
        export_mock.assert_not_called()

    def test_still_unhealthy_after_one_retry_reports_false_not_raise(self):
        with (
            mock.patch.object(ffmpeg_utils, "binaries_healthy", return_value=False),
            mock.patch.object(ffmpeg_utils, "invalidate_binary_caches"),
            mock.patch.object(ffmpeg_utils, "export_binaries_to_path"),
        ):
            result = ffmpeg_utils.ensure_binaries_healthy_at_startup()

        self.assertFalse(result)  # reported, never raised -- app.py just logs a warning

    def test_never_spawns_a_subprocess(self):
        """Belt-and-suspenders for the EDR rule: the whole self-heal path
        must be exec-free. subprocess.Popen/run must not be called at all
        while running the health check + one self-heal pass."""
        good_ffmpeg = _make_fake_bin(self.scratch / "good_ffmpeg")
        good_ffprobe = _make_fake_bin(self.scratch / "good_ffprobe")
        with (
            mock.patch.object(ffmpeg_utils, "ffmpeg_bin", return_value=str(good_ffmpeg)),
            mock.patch.object(ffmpeg_utils, "ffprobe_bin", return_value=str(good_ffprobe)),
            mock.patch("subprocess.run") as run_mock,
            mock.patch("subprocess.Popen") as popen_mock,
        ):
            ffmpeg_utils.ensure_binaries_healthy_at_startup()

        run_mock.assert_not_called()
        popen_mock.assert_not_called()


class IsUsablePathTests(SelfHealTestCase):
    """Unit coverage for the exists()+X_OK primitive everything above is
    built on."""

    def test_none_and_empty_are_unusable(self):
        self.assertFalse(ffmpeg_utils._is_usable_path(None))
        self.assertFalse(ffmpeg_utils._is_usable_path(""))

    def test_bare_command_name_is_trusted(self):
        # A relative/bare name (e.g. "ffmpeg") isn't stat()'d -- its
        # liveness is re-checked by _bin_works()/_supports_ass() at
        # resolve time instead.
        self.assertTrue(ffmpeg_utils._is_usable_path("ffmpeg"))

    def test_existing_executable_absolute_path_is_usable(self):
        fake = _make_fake_bin(self.scratch / "usable_bin")
        self.assertTrue(ffmpeg_utils._is_usable_path(str(fake)))

    def test_missing_absolute_path_is_unusable(self):
        missing = self.scratch / "nope"
        self.assertFalse(ffmpeg_utils._is_usable_path(str(missing)))

    def test_non_executable_absolute_path_is_unusable(self):
        f = self.scratch / "not_exec"
        f.write_text("#!/bin/sh\necho hi\n")  # no chmod +x
        self.assertFalse(ffmpeg_utils._is_usable_path(str(f)))

    def test_dangling_symlink_is_unusable(self):
        target = self.scratch / "gone"
        _make_fake_bin(target)
        link = self.scratch / "link_to_gone"
        link.symlink_to(target)
        target.unlink()
        self.assertFalse(ffmpeg_utils._is_usable_path(str(link)))


if __name__ == "__main__":
    unittest.main()
