#!/usr/bin/env python3
"""Unit tests for the single-instance guard (magic_video_editor/single_instance.py).

No pytest in this project's dependency set (see pyproject.toml) -- stdlib
unittest, same "no build step" spirit as scripts/test_updater.py. Runs
against a scratch tmp dir, never the real config.DATA_DIR, and never spawns
a real second process/server/window (see the bug-fix task's hard rules).

Usage:
    uv run python scripts/test_single_instance.py
    uv run python scripts/test_single_instance.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from magic_video_editor import single_instance  # noqa: E402


class SingleInstanceLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "mve.lock"

    def test_acquire_then_release_frees_it(self):
        lock = single_instance.SingleInstanceLock(self.lock_path)
        self.assertTrue(lock.acquire())
        self.assertTrue(self.lock_path.exists())
        # pid stamp written for debugging
        self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))

        lock.release()

        # Once released, a fresh lock object can acquire it again.
        other = single_instance.SingleInstanceLock(self.lock_path)
        self.assertTrue(other.acquire())
        other.release()

    def test_second_acquire_is_blocked_while_first_holds_it(self):
        first = single_instance.SingleInstanceLock(self.lock_path)
        second = single_instance.SingleInstanceLock(self.lock_path)

        self.assertTrue(first.acquire())
        try:
            # Distinct fd contending for the same OS-level flock -- this is
            # what a genuine second process launch looks like.
            self.assertFalse(second.acquire())
        finally:
            first.release()

    def test_acquire_is_idempotent_on_the_same_object(self):
        lock = single_instance.SingleInstanceLock(self.lock_path)
        self.assertTrue(lock.acquire())
        try:
            self.assertTrue(lock.acquire())  # already held by this object -> True, no re-lock
        finally:
            lock.release()

    def test_stale_lock_with_dead_pid_is_reclaimable(self):
        # Simulate a crashed previous instance: a lockfile left on disk
        # with a pid that no longer exists, but -- as would actually
        # happen on a real crash -- nobody holds the OS-level flock on it
        # anymore (the kernel released it when that process's fd closed).
        dead_pid = 999_999_999
        self.lock_path.write_text(str(dead_pid))
        self.assertFalse(single_instance._pid_alive(dead_pid))

        lock = single_instance.SingleInstanceLock(self.lock_path)
        self.assertTrue(lock.acquire(), "a lock nobody is actually holding must be reclaimable")
        self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))
        lock.release()

    def test_release_without_acquire_is_a_safe_noop(self):
        lock = single_instance.SingleInstanceLock(self.lock_path)
        lock.release()  # must not raise
        self.assertFalse(self.lock_path.exists())

    def test_flock_error_other_than_contention_raises_lock_unavailable(self):
        lock = single_instance.SingleInstanceLock(self.lock_path)
        with mock.patch(
            "magic_video_editor.single_instance.fcntl.flock",
            side_effect=OSError(38, "Function not implemented"),  # ENOSYS-ish, network volume
        ):
            with self.assertRaises(single_instance.LockUnavailable):
                lock.acquire()


class SingletonModuleLevelTests(unittest.TestCase):
    """Tests acquire_singleton()/release_singleton(), which track ONE lock
    per-process (mirrors how app.py/server.py actually call this module)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "mve.lock"
        # Defensive: never leak process-global state between tests.
        single_instance.release_singleton()
        self.addCleanup(single_instance.release_singleton)

    def test_acquire_singleton_then_second_call_returns_none(self):
        first = single_instance.acquire_singleton(self.lock_path)
        self.assertIsNotNone(first)

        # A second acquire_singleton() call in THIS process returns the
        # same held lock (idempotent), not None -- it doesn't re-contend
        # with itself. detect_existing_instance() below is what a second
        # process actually hits.
        second = single_instance.acquire_singleton(self.lock_path)
        self.assertIs(first, second)

    def test_release_singleton_allows_a_fresh_process_style_reacquire(self):
        single_instance.acquire_singleton(self.lock_path)
        single_instance.release_singleton()

        # Reset module state to simulate "a new process" acquiring fresh.
        single_instance._instance = None
        lock = single_instance.SingleInstanceLock(self.lock_path)
        self.assertTrue(lock.acquire())
        lock.release()


class DetectExistingInstanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        single_instance.release_singleton()
        self.addCleanup(single_instance.release_singleton)

    def test_no_existing_instance_when_lock_is_free(self):
        with mock.patch.object(single_instance, "probe_existing_instance") as probe:
            result = single_instance.detect_existing_instance(self.data_dir, "127.0.0.1", 8765)
        self.assertFalse(result)
        probe.assert_not_called()  # lock alone was decisive -- no need for the HTTP probe

    def test_existing_instance_detected_when_lock_already_held(self):
        # Hold the lock as if "this process" already launched once.
        holder = single_instance.SingleInstanceLock(self.data_dir / single_instance.LOCK_FILENAME)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)

        with mock.patch.object(single_instance, "probe_existing_instance") as probe:
            result = single_instance.detect_existing_instance(self.data_dir, "127.0.0.1", 8765)
        self.assertTrue(result)
        probe.assert_not_called()

    def test_falls_back_to_health_probe_when_lock_unavailable(self):
        with mock.patch.object(
            single_instance, "acquire_singleton", side_effect=single_instance.LockUnavailable("nope")
        ):
            with mock.patch.object(single_instance, "probe_existing_instance", return_value=True) as probe:
                result = single_instance.detect_existing_instance(self.data_dir, "127.0.0.1", 8765)
        self.assertTrue(result)
        probe.assert_called_once_with("127.0.0.1", 8765)


class ProbeExistingInstanceTests(unittest.TestCase):
    def test_probe_false_when_nothing_listening(self):
        # Nothing is bound on this port in the test environment -- urlopen
        # should raise and probe_existing_instance must swallow it.
        self.assertFalse(single_instance.probe_existing_instance("127.0.0.1", 1, timeout=0.2))

    def test_probe_true_when_health_identifies_this_app(self):
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"name": "Magic Video Editor"}'

        with mock.patch(
            "magic_video_editor.single_instance.urllib.request.urlopen",
            return_value=_FakeResp(),
        ):
            self.assertTrue(single_instance.probe_existing_instance("127.0.0.1", 8765))

    def test_probe_false_when_health_identifies_a_different_app(self):
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"name": "Something Else"}'

        with mock.patch(
            "magic_video_editor.single_instance.urllib.request.urlopen",
            return_value=_FakeResp(),
        ):
            self.assertFalse(single_instance.probe_existing_instance("127.0.0.1", 8765))


if __name__ == "__main__":
    unittest.main()
