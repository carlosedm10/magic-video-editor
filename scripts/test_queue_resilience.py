#!/usr/bin/env python3
"""Resilience tests for the single global queue-worker thread (magic_video_editor.
queue) and the per-job ffmpeg child-process tracking it relies on
(magic_video_editor.jobs / magic_video_editor.ffmpeg_utils).

Covers four confirmed findings (concurrency-core hardening pass):

  1. A project deleted while its queue item is RUNNING must never kill the
     sole worker thread -- every worker-internal store.load(pid) is now
     guarded against store.ProjectNotFound, and _worker_loop's own
     except-handler can no longer raise either. The worker must keep
     processing every OTHER project's queue afterwards.
  2. An unforeseen exception that escapes _worker_loop entirely (not just
     the ones the hardened except-handler catches) must still self-heal:
     _ensure_worker() detects a dead/absent worker thread and respawns it.
  3. Cancelling one job must terminate ONLY that job's own ffmpeg children
     -- a sibling job running concurrently (different lock_key, via
     jobs.start()) must be left completely alone.
  4. queue.cancel_item() must release the global _state_lock BEFORE
     jobs.cancel()'s blocking terminate-wait, so an unrelated project's
     queue operations (enqueue, etc.) are never stalled behind one
     project's cancel.

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor). No real ffmpeg encodes
anywhere -- child processes are plain `sleep`/`bash` stand-ins (per the
task's own suggestion), monkeypatched runners, or synthetic exceptions.
Every wait is bounded (short polling loops, ~30s max), never unbounded.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_reel_previews.py. MVE_DATA must be set (to a fresh tmp dir)
BEFORE importing anything from magic_video_editor, since magic_video_editor.
config reads it at import time.

Usage:
    uv run python scripts/test_queue_resilience.py
    uv run python scripts/test_queue_resilience.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_queue_resilience_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from magic_video_editor import config, ffmpeg_utils, jobs, queue, store  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

_BOUND = 15.0  # generic bounded-poll ceiling for this file, well under the 30s hard rule


def _wait_for_item_status(pid: str, item_id: str, statuses: set, timeout: float = _BOUND) -> dict:
    """Bounded poll for queue item `item_id` in project `pid` to reach one
    of `statuses`. Raises AssertionError on timeout (never loops forever)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        project = store.load(pid)
        item = next((i for i in project.get("queue", []) if i["id"] == item_id), None)
        if item is not None:
            last = item
            if item["status"] in statuses:
                return item
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {pid}/{item_id} to reach {statuses}: {last}")


def _wait_until(predicate, timeout: float = _BOUND, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _noop_runner(log, project, payload) -> None:
    pass


queue.register_runner("test:noop_done", _noop_runner)


class Test1WorkerSurvivesDeletedProject(unittest.TestCase):
    """Finding 1 (root cause): a project deleted while its item is running
    used to raise store.ProjectNotFound a SECOND time inside _worker_loop's
    own except-handler, escaping it and killing the sole worker thread
    forever -- every future job in every project would then spin as
    "pending" forever. Guarded now in _run_item/_mark_item; this proves it
    end to end, plus a couple of direct unit-level checks on the guards
    themselves."""

    def test_01_mark_item_and_run_item_are_noops_for_a_gone_project(self):
        # Direct, deterministic check of the exact guard: neither function
        # may raise for a project id that simply doesn't exist on disk.
        queue._mark_item("does-not-exist-xyz", "whatever", status="error", error="boom")
        queue._run_item("does-not-exist-xyz", "whatever")

    def test_02_project_deleted_mid_run_does_not_kill_the_worker(self):
        def _rmtree_self(log, project, payload):
            # Simulates api/projects.py's project_delete racing this exact
            # running item: the project's directory disappears WHILE the
            # runner is still executing on the sole worker thread.
            shutil.rmtree(store.project_dir(project["id"]), ignore_errors=True)

        queue.register_runner("test:delete_self", _rmtree_self)

        project = store.new_project("delete-race-src")
        pid = project["id"]
        queue.enqueue(pid, "test:delete_self", {})

        # Can't poll the item's own status afterward (its project.json is
        # gone) -- bound this on the directory actually disappearing.
        self.assertTrue(
            _wait_until(lambda: not store.project_dir(pid).exists()),
            "runner never got to rmtree its own project dir",
        )
        # Give the worker a brief moment to run its (now guarded, no-op)
        # post-run bookkeeping and loop back around for more work.
        time.sleep(0.3)
        self.assertIsNotNone(queue._worker_thread)
        self.assertTrue(
            queue._worker_thread.is_alive(),
            "worker thread died after a project was deleted mid-job (finding 1)",
        )

        # Prove it's still actually SERVING the queue, not just technically
        # alive: a fresh job on a different, still-existing project must
        # complete normally.
        project2 = store.new_project("delete-race-followup")
        pid2 = project2["id"]
        item2 = queue.enqueue(pid2, "test:noop_done", {})
        settled = _wait_for_item_status(pid2, item2["id"], {"done", "error", "cancelled"})
        self.assertEqual(settled["status"], "done", settled)


class Test2EnsureWorkerRespawnsDeadThread(unittest.TestCase):
    """Finding 1c: _ensure_worker() must not rely solely on the one-shot
    _worker_started latch -- it must detect a dead/absent worker thread and
    respawn it. Forces a genuinely fatal crash (SystemExit, a BaseException
    that escapes even the hardened `except Exception` around _next_item())
    to kill the real worker thread, then proves a subsequent enqueue()
    notices and replaces it."""

    def test_ensure_worker_respawns_after_a_fatal_crash(self):
        # Make sure a (real) worker exists first.
        project = store.new_project("respawn-src")
        pid = project["id"]
        item = queue.enqueue(pid, "test:noop_done", {})
        _wait_for_item_status(pid, item["id"], {"done"})

        dead_thread = queue._worker_thread
        self.assertIsNotNone(dead_thread)

        original_next_item = queue._next_item

        def _boom():
            queue._next_item = original_next_item  # self-restore: fires exactly once
            raise SystemExit("simulated fatal worker crash")

        queue._next_item = _boom
        queue._wake.set()  # nudge the idle worker to call _next_item() again promptly

        self.assertTrue(
            _wait_until(lambda: not dead_thread.is_alive()),
            "simulated crash never actually killed the worker thread",
        )

        # Self-heal: a new enqueue must detect the dead thread and respawn.
        project2 = store.new_project("respawn-followup")
        pid2 = project2["id"]
        item2 = queue.enqueue(pid2, "test:noop_done", {})

        self.assertIsNotNone(queue._worker_thread)
        self.assertIsNot(
            queue._worker_thread, dead_thread, "_ensure_worker did not respawn a new thread"
        )
        settled = _wait_for_item_status(pid2, item2["id"], {"done", "error", "cancelled"})
        self.assertEqual(settled["status"], "done", settled)


class Test3PerJobCancelIsolation(unittest.TestCase):
    """Finding 12+17: cancelling one job must terminate ONLY that job's own
    ffmpeg children. Uses two concurrently-running jobs.start() jobs (real
    concurrency needs different lock_keys -- queue.py's own single worker
    only ever runs one item at a time) with `sleep` stand-ins for ffmpeg
    (never a real encode)."""

    @staticmethod
    def _sleep_job(seconds: float):
        def fn(log):
            proc = ffmpeg_utils._spawn(["sleep", str(seconds)])
            proc.wait()
            if proc.returncode != 0:
                # Mirrors ffmpeg_utils.run()'s own "nonzero exit -> raise"
                # behavior, so a SIGTERM'd child is correctly attributed to
                # cancellation by jobs._execute (same as a real ffmpeg run).
                raise ffmpeg_utils.FFmpegError(f"sleep exited with {proc.returncode}")

        return fn

    def test_cancel_kills_only_that_jobs_children(self):
        job_a = jobs.start("test:sleepA", self._sleep_job(6), lock_key="resil-test-lock-a")
        job_b = jobs.start("test:sleepB", self._sleep_job(6), lock_key="resil-test-lock-b")

        self.assertTrue(
            _wait_until(
                lambda: ffmpeg_utils._procs_by_job.get(job_a)
                and ffmpeg_utils._procs_by_job.get(job_b)
            ),
            "both jobs' children should have registered under their own job_id",
        )
        proc_b = next(iter(ffmpeg_utils._procs_by_job[job_b]))
        self.assertIsNone(proc_b.poll(), "job B's child should still be running before any cancel")

        self.assertTrue(jobs.cancel(job_a))

        self.assertTrue(
            _wait_until(lambda: jobs.get(job_a)["status"] != "running"),
            "job A never left running after cancel",
        )
        self.assertEqual(jobs.get(job_a)["status"], "cancelled", jobs.get(job_a))

        # The actual isolation check: job B's process must be completely
        # untouched by job A's cancellation.
        self.assertIsNone(
            proc_b.poll(), "cancelling job A killed job B's ffmpeg child too (finding 12+17)"
        )
        self.assertEqual(jobs.get(job_b)["status"], "running")

        # Cleanup: cancel job B too rather than waiting out its full sleep.
        jobs.cancel(job_b)
        self.assertTrue(_wait_until(lambda: jobs.get(job_b)["status"] != "running"))
        self.assertEqual(jobs.get(job_b)["status"], "cancelled")


class Test4CancelItemReleasesLockBeforeWait(unittest.TestCase):
    """Finding 13: queue.cancel_item() must drop the global _state_lock
    before jobs.cancel()'s blocking terminate-children wait (up to ~5s, see
    ffmpeg_utils._terminate's grace period), so it doesn't stall every other
    project's queue reads/writes for that whole window."""

    def test_cancel_item_does_not_hold_lock_across_the_wait(self):
        def _stubborn_runner(log, project, payload):
            # Ignores SIGTERM outright, forcing terminate_job() to ride out
            # the full ~5s grace period before SIGKILL -- makes the
            # blocking wait long enough to reliably measure.
            proc = ffmpeg_utils._spawn(["bash", "-c", 'trap "" TERM; sleep 8'])
            proc.wait()
            if proc.returncode != 0:
                raise ffmpeg_utils.FFmpegError(f"exited {proc.returncode}")

        queue.register_runner("test:stubborn", _stubborn_runner)

        project = store.new_project("lockrelease-src")
        pid = project["id"]
        item = queue.enqueue(pid, "test:stubborn", {})
        _wait_for_item_status(pid, item["id"], {"running"})

        job_id = None

        def _has_job_id():
            nonlocal job_id
            reloaded = store.load(pid)
            it = next(i for i in reloaded["queue"] if i["id"] == item["id"])
            job_id = it.get("job_id")
            return bool(job_id)

        self.assertTrue(_wait_until(_has_job_id), "queue item never got a job_id")
        # job_id is stashed (via on_start) BEFORE jobs.run_sync actually
        # calls the runner -- also wait for the runner's bash child to have
        # actually spawned and registered, or cancel_item could race ahead
        # of it and terminate_job() would have nothing to terminate yet.
        self.assertTrue(
            _wait_until(lambda: bool(ffmpeg_utils._procs_by_job.get(job_id))),
            "stubborn runner's child process never registered",
        )

        result: dict = {}

        def _do_cancel():
            t0 = time.monotonic()
            queue.cancel_item(pid, item["id"])
            result["dur"] = time.monotonic() - t0

        th = threading.Thread(target=_do_cancel)
        th.start()
        time.sleep(0.5)  # let cancel_item enter (and, if buggy, hold) the lock

        project2 = store.new_project("lockrelease-probe")
        pid2 = project2["id"]
        t0 = time.monotonic()
        queue.enqueue(pid2, "test:noop_done", {})
        probe_dur = time.monotonic() - t0

        th.join(timeout=_BOUND)
        self.assertFalse(th.is_alive(), "cancel_item's own thread never finished")

        self.assertLess(
            probe_dur,
            1.0,
            f"enqueue() on an unrelated project took {probe_dur:.2f}s while cancel_item's "
            "blocking terminate-wait was in flight -- _state_lock held across it? (finding 13)",
        )
        self.assertGreater(
            result.get("dur", 0),
            1.0,
            "expected cancel_item's own call to take a noticeable while (the bash child "
            "traps SIGTERM, forcing the ~5s SIGKILL grace wait) -- otherwise this test "
            "isn't actually exercising the blocking wait at all",
        )


def _tearDownModule_wait_for_quiescence() -> None:
    """Bounded best-effort drain so the scratch dir isn't rmtree'd out from
    under the still-daemon worker thread mid-item (harmless either way --
    daemon thread, process is exiting -- but avoids noisy logging)."""
    deadline = time.time() + _BOUND
    while time.time() < deadline:
        try:
            busy = any(
                any(
                    i["status"] in ("pending", "running")
                    for i in store.load(p["id"]).get("queue", [])
                )
                for p in store.list_projects()
            )
        except Exception:
            break
        if not busy:
            break
        time.sleep(0.2)


def tearDownModule():
    _tearDownModule_wait_for_quiescence()
    shutil.rmtree(_SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
