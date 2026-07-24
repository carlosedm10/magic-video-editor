"""Minimal background-job runner: one worker thread per job, in-memory registry,
UI polls /api/jobs/<id> for status and log lines.

Resource safety additions (spec: "Resource safety"): per-lock-key exclusivity
(one running job per project) via `lock_key`, and cooperative cancellation
(`cancel()`) that also tears down any ffmpeg children via
ffmpeg_utils.terminate_all() -- acceptable globally since the per-project
lock means at most one heavy job runs at a time."""

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


def start(name: str, fn, *args, lock_key: str | None = None) -> str:
    """Start fn(log, *args) in a background thread. If lock_key is given and
    already has a running job, raises JobBusyError(existing_job_id) instead
    of starting a second one."""
    with _lock:
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
    log = JobLog(job)

    def release_lock() -> None:
        if lock_key is None:
            return
        with _lock:
            if _lock_owners.get(lock_key) == job["id"]:
                del _lock_owners[lock_key]

    def run():
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
                # cancel() kills the job's ffmpeg children directly, which
                # usually surfaces as some other exception (e.g. FFmpegError
                # from the terminated process) rather than JobCancelled --
                # attribute it to the cancellation the caller asked for.
                if job.get("cancel_requested"):
                    job["status"] = "cancelled"
                    job["log"].append(f"[{time.strftime('%H:%M:%S')}] cancelled")
                else:
                    job["status"] = "error"
                    job["error"] = str(e)
                    job["log"].append(traceback.format_exc()[-2000:])
        finally:
            release_lock()

    threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
    return job["id"]


def cancel(job_id: str) -> bool:
    """Request cooperative cancellation of a running job and terminate any
    ffmpeg children (global -- fine given the per-lock-key exclusivity).
    Returns False if the job isn't running (already finished/unknown)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "running":
            return False
        job["cancel_requested"] = True
    ffmpeg_utils.terminate_all()
    return True


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def running() -> list[dict]:
    with _lock:
        return [dict(j) for j in _jobs.values() if j["status"] == "running"]
