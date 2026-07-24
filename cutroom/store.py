"""Project persistence: one folder per project under ~/CutRoom/projects/<id>,
with a single project.json plus generated artifacts (wavs, renders, reels)."""

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
    }
    _pdir(project["id"]).mkdir(parents=True, exist_ok=True)
    (_pdir(project["id"]) / "work").mkdir(exist_ok=True)
    save(project)
    return project


def save(project: dict) -> None:
    with _lock:
        path = _pfile(project["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(project, indent=1))
        tmp.replace(path)


def load(project_id: str) -> dict:
    return json.loads(_pfile(project_id).read_text())


def list_projects() -> list[dict]:
    config.ensure_dirs()
    out = []
    for d in sorted(config.PROJECTS_DIR.iterdir()):
        f = d / "project.json"
        if f.exists():
            p = json.loads(f.read_text())
            out.append({"id": p["id"], "name": p["name"], "created_at": p["created_at"],
                        "clips": len(p["clips"]), "stages": p.get("stages", {})})
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
        "status": status, "at": time.strftime("%H:%M:%S"), "detail": detail,
    }
    save(project)


def get_clip(project: dict, clip_id: str) -> dict:
    for c in project["clips"]:
        if c["id"] == clip_id:
            return c
    raise KeyError(f"clip {clip_id} not found")
