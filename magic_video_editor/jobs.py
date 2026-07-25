"""Minimal background-job runner: one worker thread per job, in-memory registry,
UI polls /api/jobs/<id> for status and log lines.

Resource safety additions (spec: "Resource safety"): per-lock-key exclusivity
(one running job per project) via `lock_key`, and cooperative cancellation
(`cancel()`) that tears down only THAT job's own ffmpeg children
(ffmpeg_utils.terminate_job(), keyed off a per-thread job context set by
_execute() via ffmpeg_utils.begin_job/end_job) -- different lock_keys (i.e.
different projects) can have jobs running concurrently via start(), so a
global terminate_all() on every cancel would kill unrelated work; that stays
reserved for real process shutdown."""

import threading
import time
import traceback
import uuid

from . import ffmpeg_utils

_jobs: dict[str, dict] = {}
_lock_owners: dict[str, str] = {}  # lock_key -> job_id of its running job
_lock = threading.Lock()


class JobCancelled(Exception):
    """Raised inside a running job (via JobLog) once cancel() has been called."""


class JobBusyError(Exception):
    """Raised by start() when lock_key already has a running job."""

    def __init__(self, job_id: str):
        super().__init__(f"a job is already running for this key: {job_id}")
        self.job_id = job_id


class JobLog:
    def __init__(self, job: dict):
        self._job = job

    def _check_cancel(self) -> None:
        with _lock:
            requested = self._job.get("cancel_requested", False)
        if requested:
            raise JobCancelled()

    def __call__(self, msg: str) -> None:
        with _lock:
            self._job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self._check_cancel()

    def progress(self, frac: float) -> None:
        with _lock:
            self._job["progress"] = round(min(1.0, max(0.0, frac)), 3)
        self._check_cancel()

    def stage(self, name: str, status: str | None = None, progress: float | None = None) -> None:
        """Maintain job["stages"][name] = {"status", "progress"} for
        multi-stage jobs (e.g. run-all). status one of pending/running/done/error;
        either argument may be omitted to leave it unchanged."""
        with _lock:
            st = self._job.setdefault("stages", {}).setdefault(
                name, {"status": "pending", "progress": 0.0}
            )
            if status is not None:
                st["status"] = status
            if progress is not None:
                st["progress"] = round(min(1.0, max(0.0, progress)), 3)
        self._check_cancel()


def _new_job(name: str, lock_key: str | None) -> dict:
    """Caller must hold _lock. Raises JobBusyError if lock_key is already
    owned by a running job."""
    if lock_key is not None:
        existing = _lock_owners.get(lock_key)
        if existing is not None and _jobs.get(existing, {}).get("status") == "running":
            raise JobBusyError(existing)
    job = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "status": "running",
        "progress": 0.0,
        "log": [],
        "error": None,
        "started_at": time.time(),
        "stages": {},
        "cancel_requested": False,
        "lock_key": lock_key,
    }
    _jobs[job["id"]] = job
    if lock_key is not None:
        _lock_owners[lock_key] = job["id"]
    return job


def _job_cancel_requested(job: dict) -> bool:
    with _lock:
        return bool(job.get("cancel_requested"))


def _execute(job: dict, fn, args) -> None:
    """Run fn(log, *args) against job, updating job["status"]/["error"]/["log"]
    to done/cancelled/error. Shared by start()'s worker thread and run_sync()'s
    synchronous execution -- the only difference between the two is which
    thread calls this.

    Registers this job as the "current job" on THIS thread for the duration
    (ffmpeg_utils.begin_job/end_job) so, without needing to thread a job_id
    or cancel-check through every pipeline module's ffmpeg_utils.run()/
    probe() call site: (a) _wait_for_ram()/_EncodeGate.acquire() can poll
    this job's own cancel_requested and bail out promptly (finding 7), and
    (b) every ffmpeg child this job spawns is filed under its own job_id so
    cancel(job_id) can terminate only THOSE children (finding 12+17)."""
    log = JobLog(job)
    ffmpeg_utils.begin_job(job["id"], lambda: _job_cancel_requested(job))
    try:
        fn(log, *args)
        with _lock:
            job["status"] = "done"
            job["progress"] = 1.0
    except JobCancelled:
        with _lock:
            job["status"] = "cancelled"
            job["log"].append(f"[{time.strftime('%H:%M:%S')}] cancelled")
    except Exception as e:
        with _lock:
            # cancel() terminates the job's OWN ffmpeg children directly,
            # which usually surfaces as some other exception (e.g.
            # FFmpegError/FFmpegCancelled from the terminated/interrupted
            # process) rather than JobCancelled -- attribute it to the
            # cancellation the caller asked for.
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["log"].append(f"[{time.strftime('%H:%M:%S')}] cancelled")
            else:
                job["status"] = "error"
                job["error"] = str(e)
                job["log"].append(traceback.format_exc()[-2000:])
    finally:
        ffmpeg_utils.end_job(job["id"])
        _release_lock(job)


def _release_lock(job: dict) -> None:
    lock_key = job.get("lock_key")
    if lock_key is None:
        return
    with _lock:
        if _lock_owners.get(lock_key) == job["id"]:
            del _lock_owners[lock_key]


def start(name: str, fn, *args, lock_key: str | None = None) -> str:
    """Start fn(log, *args) in a background thread. If lock_key is given and
    already has a running job, raises JobBusyError(existing_job_id) instead
    of starting a second one."""
    with _lock:
        job = _new_job(name, lock_key)
    threading.Thread(
        target=_execute, args=(job, fn, args), daemon=True, name=f"job-{name}"
    ).start()
    return job["id"]


def run_sync(name: str, fn, *args, lock_key: str | None = None, on_start=None) -> dict:
    """Like start(), but runs fn(log, *args) synchronously on the CALLING
    thread instead of spawning a new one, returning the finished job dict.

    Used by queue.py's single worker thread, which is already its own thread
    of execution and just wants the job-dict/JobLog/cancel machinery that
    start() provides, without a second thread per queue item.

    `on_start`, if given, fires with the new job id right after the job is
    registered (and before fn starts running) so a caller can stash the id
    somewhere reachable (e.g. onto its own queue item) so jobs.cancel() can
    reach it while fn is still executing."""
    with _lock:
        job = _new_job(name, lock_key)
    if on_start is not None:
        on_start(job["id"])
    _execute(job, fn, args)
    with _lock:
        return dict(job)


def cancel(job_id: str) -> bool:
    """Request cooperative cancellation of a running job and terminate ONLY
    that job's ffmpeg children (finding 12+17: previously terminate_all()
    killed every tracked ffmpeg child process-wide, so cancelling one job
    also killed unrelated concurrent jobs and the untracked live
    color-preview-frame path). Returns False if the job isn't running
    (already finished/unknown)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "running":
            return False
        job["cancel_requested"] = True
    ffmpeg_utils.terminate_job(job_id)
    return True


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def running() -> list[dict]:
    with _lock:
        return [dict(j) for j in _jobs.values() if j["status"] == "running"]
