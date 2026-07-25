"""Project persistence: one folder per project under <data dir>/projects/<id>
(see config.DATA_DIR), with a single project.json plus generated artifacts
(wavs, renders, reels)."""

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# Guards against re-healing the same project id over and over on every
# load() within a single process -- once a project has been checked (healed
# or not, or already on the current data dir), never re-check it again.
_healed_once: set[str] = set()


class ProjectNotFound(FileNotFoundError):
    """Raised by load() when `project_id` has no project.json on disk.
    Subclasses FileNotFoundError on purpose: existing call sites written as
    `except FileNotFoundError` (edl.py, subtitles.py, overlays.py,
    suggestions.py, thumbs.py, reels.py, filters.py, ...) keep working
    unchanged, while server.py additionally registers an exception handler
    for this specific type so any call site that DIDN'T bother catching it
    still gets a clean 404 instead of a raw 500 traceback."""


def _pdir(project_id: str) -> Path:
    return config.PROJECTS_DIR / project_id


def _pfile(project_id: str) -> Path:
    return _pdir(project_id) / "project.json"


def _any_path_exists(obj) -> bool:
    """True if any string value under `obj` (recursively, through dicts and
    lists) is a filesystem path that currently exists. Used by the self-heal
    below to make sure we only rewrite legacy paths when the *rewritten*
    path is actually reachable -- never blindly rewrite into nothing."""
    if isinstance(obj, str):
        return len(obj) > 1 and Path(obj).exists()
    if isinstance(obj, dict):
        return any(_any_path_exists(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_any_path_exists(v) for v in obj)
    return False


def _self_heal_legacy_paths(project: dict, project_id: str) -> dict:
    """v5.13 migration follow-up: heals any project whose project.json still
    contains absolute paths under the hardcoded legacy `~/CutRoom` default
    (config._OLD_DEFAULT_DATA_DIR) even though the equivalent file now
    exists under the *current* data dir (config.DATA_DIR) -- i.e. someone
    who already went through a bad migration (moved the directory without
    migrate_data_dir()'s path rewrite, or hand-moved it before that existed).
    Rewrites + saves once, and only when the rewritten path actually
    resolves on disk (never "heals" into paths that don't exist either).
    Guarded by _healed_once so this only ever touches disk once per project
    id per process, not on every single load()."""
    if project_id in _healed_once:
        return project
    _healed_once.add(project_id)

    old_prefix = str(config._OLD_DEFAULT_DATA_DIR)
    new_prefix = str(config.DATA_DIR)
    if old_prefix == new_prefix:
        return project

    text = json.dumps(project)
    if old_prefix not in text:
        return project

    healed = json.loads(text.replace(old_prefix, new_prefix))
    if not _any_path_exists(healed):
        # Rewriting wouldn't actually point anywhere real either -- leave
        # the project untouched (and un-erroring) rather than "healing"
        # it into a still-broken state.
        return project

    logger.warning(
        "Self-healing stale legacy paths (%s -> %s) in project %s",
        old_prefix,
        new_prefix,
        project_id,
    )
    save(healed, preserve_queue=False)
    return healed


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
    try:
        project = json.loads(_pfile(project_id).read_text())
    except FileNotFoundError:
        raise ProjectNotFound(project_id) from None
    return _self_heal_legacy_paths(project, project_id)


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
