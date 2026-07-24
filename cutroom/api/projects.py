"""Project, clip, sentence, and clip-order endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store
from ..pipeline import ingest, ordering

router = APIRouter(prefix="/api", tags=["projects"])


class NewProject(BaseModel):
    name: str


class AddClips(BaseModel):
    paths: list[str]


class ClipUpdate(BaseModel):
    role: str | None = None
    is_main: bool | None = None


class SentenceUpdate(BaseModel):
    kept: bool


class OrderUpdate(BaseModel):
    clip_order: list[str]


@router.get("/projects")
def projects_list():
    return store.list_projects()


@router.post("/projects")
def projects_create(body: NewProject):
    return store.new_project(body.name.strip() or "Untitled")


@router.get("/projects/{pid}")
def project_get(pid: str):
    try:
        p = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    p["edl_preview"] = ordering.build_edl(p) if p.get("sentences") else []
    return p


@router.delete("/projects/{pid}")
def project_delete(pid: str):
    store.delete_project(pid)
    return {"ok": True}


@router.post("/projects/{pid}/clips")
def clips_add(pid: str, body: AddClips):
    project = store.load(pid)
    added = ingest.add_clips(project, body.paths)
    return {"added": len(added), "clips": project["clips"]}


@router.post("/projects/{pid}/clips/{cid}")
def clip_update(pid: str, cid: str, body: ClipUpdate):
    project = store.load(pid)
    clip = store.get_clip(project, cid)
    if body.role in ("camera", "audio"):
        clip["role"] = body.role
    if body.is_main is not None:
        for c in project["clips"]:
            c["is_main"] = False
        clip["is_main"] = body.is_main
    store.save(project)
    return clip


@router.delete("/projects/{pid}/clips/{cid}")
def clip_remove(pid: str, cid: str):
    project = store.load(pid)
    project["clips"] = [c for c in project["clips"] if c["id"] != cid]
    project["sentences"] = [s for s in project.get("sentences", []) if s["clip_id"] != cid]
    store.save(project)
    return {"ok": True}


@router.post("/projects/{pid}/sentences/{sid}")
def sentence_update(pid: str, sid: str, body: SentenceUpdate):
    project = store.load(pid)
    for s in project["sentences"]:
        if s["id"] == sid:
            s["kept"] = body.kept
            s["reason"] = "" if body.kept else "excluded manually"
            store.save(project)
            return s
    raise HTTPException(404)


@router.post("/projects/{pid}/order")
def order_update(pid: str, body: OrderUpdate):
    project = store.load(pid)
    project["clip_order"] = body.clip_order
    project["order_notes"] = "manual order"
    store.save(project)
    return {"ok": True}
