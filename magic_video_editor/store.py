"""Project persistence: one folder per project under <data dir>/projects/<id>
(see config.DATA_DIR), with a single project.json plus generated artifacts
(wavs, renders, reels)."""

import json
import threading
import time
import uuid
from pathlib import Path

from . import config

_lock = threading.Lock()


def _pdir(project_id: str) -> Path:
    return config.PROJECTS_DIR / project_id


def _pfile(project_id: str) -> Path:
    return _pdir(project_id) / "project.json"


def new_project(name: str) -> dict:
    config.ensure_dirs()
    project = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "clips": [],
        "sync_groups": [],
        "sentences": [],
        "clip_order": [],
        "order_notes": "",
        "renders": [],
        "reels": [],
        "stages": {},  # stage name -> {"status": "done"|"error", "at": ts, "detail": str}
        "workflow_status": "todo",  # manual, user-set: todo|in_progress|done|uploaded
    }
    _pdir(project["id"]).mkdir(parents=True, exist_ok=True)
    (_pdir(project["id"]) / "work").mkdir(exist_ok=True)
    save(project)
    return project


def save(project: dict, *, preserve_queue: bool = True) -> None:
    """Persist `project`. By default, re-merges whatever "queue" currently
    holds ON DISK into what we're about to write.

    Why: store.save() is called from dozens of sites across the pipeline
    stages and API endpoints, usually holding an in-memory `project`
    snapshot loaded once at the start of a (sometimes multi-minute)
    operation. magic_video_editor/queue.py's project["queue"] is meanwhile mutated
    out-of-band (new items enqueued, cancelled, reordered, job_id/status
    updates) via its OWN fresh load-mutate-save cycles on the SAME
    project.json. Every plain store.save(project) from a long-running
    caller was silently overwriting project["queue"] with its own stale
    (pre-mutation) copy, permanently losing whatever queue.py had written
    in the meantime -- reproduced live: an item enqueued while a `run-all`
    stage was executing vanished the instant that stage's next internal
    store.save() landed, and the run-all item's own job_id (set by
    queue.py moments after starting it) got reverted to null the same way.
    Only magic_video_editor.queue itself should ever intentionally overwrite this
    field, so its call sites pass preserve_queue=False.

    This narrows the race to the (non-atomic-with-queue.py's own
    _state_lock) read-then-write window here -- microseconds, not the
    minutes-long window before this fix -- rather than eliminating it
    outright, which would need a shared lock/merge scheme across both
    modules; flagged as a residual risk, not attempted here."""
    with _lock:
        path = _pfile(project["id"])
        if preserve_queue and path.exists():
            try:
                on_disk_queue = json.loads(path.read_text()).get("queue")
                if on_disk_queue is not None:
                    project["queue"] = on_disk_queue
            except Exception:
                pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(project, indent=1))
        tmp.replace(path)


def load(project_id: str) -> dict:
    return json.loads(_pfile(project_id).read_text())


def processing_level(project: dict) -> str:
    """AUTOMATIC status derived from stages/queue (v5.2): "finalizado" once
    the render stage is done, "en_proceso" once any stage has completed or
    the project's queue has a pending/running item, else "por_empezar"."""
    stages = project.get("stages", {})
    if stages.get("render", {}).get("status") == "done":
        return "finalizado"
    any_done = any(s.get("status") == "done" for s in stages.values())
    queue_busy = any(i.get("status") in ("pending", "running") for i in project.get("queue", []))
    if any_done or queue_busy:
        return "en_proceso"
    return "por_empezar"


def list_projects() -> list[dict]:
    config.ensure_dirs()
    out = []
    for d in sorted(config.PROJECTS_DIR.iterdir()):
        f = d / "project.json"
        if f.exists():
            p = json.loads(f.read_text())
            out.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "created_at": p["created_at"],
                    "clips": len(p["clips"]),
                    "stages": p.get("stages", {}),
                    "workflow_status": p.get("workflow_status", "todo"),
                    "processing_level": processing_level(p),
                }
            )
    return out


def delete_project(project_id: str) -> None:
    import shutil

    d = _pdir(project_id)
    if d.exists():
        shutil.rmtree(d)


def project_dir(project_id: str) -> Path:
    return _pdir(project_id)


def mark_stage(project: dict, stage: str, status: str, detail: str = "") -> None:
    project.setdefault("stages", {})[stage] = {
        "status": status,
        "at": time.strftime("%H:%M:%S"),
        "detail": detail,
    }
    save(project)


def get_clip(project: dict, clip_id: str) -> dict:
    for c in project["clips"]:
        if c["id"] == clip_id:
            return c
    raise KeyError(f"clip {clip_id} not found")
