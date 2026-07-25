"""Project, clip, sentence, and clip-order endpoints."""

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import queue, store
from ..pipeline import copywriter, ingest, ordering

router = APIRouter(prefix="/api", tags=["projects"])

# v5.2: manual, user-set organizational status (distinct from the automatic
# store.processing_level derived from stages/queue).
WORKFLOW_STATUSES = {"todo", "in_progress", "done", "uploaded"}

# v5.3 streaming upload: never buffer a whole file in memory -- GB-sized
# iPhone clips over loopback are fast, so chunk-copy to disk instead.
_UPLOAD_CHUNK = 1024 * 1024


class NewProject(BaseModel):
    name: str


class AddClips(BaseModel):
    paths: list[str]
    camera_group: str | None = None


class ClipUpdate(BaseModel):
    role: str | None = None
    is_main: bool | None = None


class SentenceUpdate(BaseModel):
    kept: bool


class OrderUpdate(BaseModel):
    clip_order: list[str]


class ProjectUpdate(BaseModel):
    name: str | None = None
    workflow_status: str | None = None


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


@router.patch("/projects/{pid}")
def project_update(pid: str, body: ProjectUpdate):
    """v5.2: rename (sanitized non-empty name) and/or set the manual
    workflow_status. Either field is optional so callers can send just one."""
    project = store.load(pid)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name must be a non-empty string")
        project["name"] = name
    if body.workflow_status is not None:
        if body.workflow_status not in WORKFLOW_STATUSES:
            raise HTTPException(
                422, f"workflow_status must be one of {sorted(WORKFLOW_STATUSES)}"
            )
        project["workflow_status"] = body.workflow_status
    store.save(project)
    return project


@router.delete("/projects/{pid}")
def project_delete(pid: str):
    store.delete_project(pid)
    return {"ok": True}


@router.post("/projects/{pid}/clips")
def clips_add(pid: str, body: AddClips):
    project = store.load(pid)
    added = ingest.add_clips(project, body.paths, camera_group=body.camera_group)
    return {"added": len(added), "clips": project["clips"]}


@router.post("/projects/{pid}/upload")
async def clips_upload(
    pid: str,
    files: list[UploadFile] = File(...),  # noqa: B008 (standard FastAPI upload idiom)
    camera_group: str | None = Form(None),  # noqa: B008
):
    """v5.3: streaming multipart upload for the browser-mode drag&drop /
    file-picker fallback (pywebview mode keeps native hardlink import via
    /clips above). Accepts many files at once; a dropped folder arrives as
    files whose `filename` carries the relative path (e.g.
    "GroupA/clip1.mp4" -- browsers send forward-slash-joined
    webkitRelativePath-style names), which we use as the camera_group for
    that folder's files when no explicit `camera_group` override is given.
    Registers clips exactly like add_clips (ingest.register_uploaded_clips)
    and enqueues the same follow-up work ingest's own stage does
    (proxies/wav via stage:ingest, filmstrips/peaks via the thumbs kind)."""
    project = store.load(pid)
    media_dir = store.project_dir(pid) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[Path, str]] = []
    for f in files:
        raw_name = (f.filename or "upload").replace("\\", "/").lstrip("/")
        parts = [p for p in raw_name.split("/") if p and p != ".."]
        if not parts:
            await f.close()
            continue
        name = parts[-1]
        if Path(name).suffix.lower() not in ingest.MEDIA_EXTS:
            await f.close()
            continue
        folder_group = parts[0] if len(parts) > 1 else None
        group = camera_group or folder_group or "main"

        stem, suffix = Path(name).stem, Path(name).suffix
        dest = media_dir / name
        n = 1
        while dest.exists():
            dest = media_dir / f"{stem}_{n}{suffix}"
            n += 1

        with open(dest, "wb") as out:
            while chunk := await f.read(_UPLOAD_CHUNK):
                out.write(chunk)
        await f.close()
        saved.append((dest, group))

    added = ingest.register_uploaded_clips(project, saved)
    if added:
        queue.enqueue(pid, "stage:ingest", {"stage": "ingest"})
        queue.enqueue(pid, "thumbs", {})
    return {"added": len(added), "clips": project["clips"]}


@router.post("/projects/{pid}/groups/{name}/main")
def group_set_main(pid: str, name: str):
    project = store.load(pid)
    ingest.set_main_group(project, name)
    store.save(project)
    return {"ok": True, "clips": project["clips"]}


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


@router.post("/projects/{pid}/publish")
def publish_generate(pid: str):
    """v5 addendum "SEO copywriter + brand profile": generate (or
    regenerate, on demand) the project-level Publish block -- a video title
    suggestion + SEO description for the main cut -- and store it as
    project["publish"]. GET /api/projects/{pid} returns it as-is thereafter."""
    project = store.load(pid)
    project["publish"] = copywriter.copy_for_video(project)
    store.save(project)
    return project["publish"]


@router.post("/projects/{pid}/order")
def order_update(pid: str, body: OrderUpdate):
    project = store.load(pid)
    project["clip_order"] = body.clip_order
    project["order_notes"] = "manual order"
    store.save(project)
    return {"ok": True}
