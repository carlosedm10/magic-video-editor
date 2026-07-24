"""Minimal background-job runner: one worker thread per job, in-memory registry,
UI polls /api/jobs/<id> for status and log lines."""

import threading
import time
import traceback
import uuid

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class JobLog:
    def __init__(self, job: dict):
        self._job = job

    def __call__(self, msg: str) -> None:
        with _lock:
            self._job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def progress(self, frac: float) -> None:
        with _lock:
            self._job["progress"] = round(min(1.0, max(0.0, frac)), 3)


def start(name: str, fn, *args) -> str:
    job = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "status": "running",
        "progress": 0.0,
        "log": [],
        "error": None,
        "started_at": time.time(),
    }
    with _lock:
        _jobs[job["id"]] = job
    log = JobLog(job)

    def run():
        try:
            fn(log, *args)
            with _lock:
                job["status"] = "done"
                job["progress"] = 1.0
        except Exception as e:
            with _lock:
                job["status"] = "error"
                job["error"] = str(e)
                job["log"].append(traceback.format_exc()[-2000:])

    threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
    return job["id"]


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def running() -> list[dict]:
    with _lock:
        return [dict(j) for j in _jobs.values() if j["status"] == "running"]
