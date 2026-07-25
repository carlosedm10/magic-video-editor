"""Project, clip, sentence, and clip-order endpoints."""

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import queue, store
from ..pipeline import copywriter, ingest, ordering, reels
from .settings import LANGUAGE_CODES

router = APIRouter(prefix="/api", tags=["projects"])

# v5.2: manual, user-set organizational status (distinct from the automatic
# store.processing_level derived from stages/queue).
WORKFLOW_STATUSES = {"todo", "in_progress", "done", "uploaded"}

# v5.8c: "Locutores" -- user-declared speaker count for diarization
# (pipeline/speakers.py). A known K makes clustering far more reliable than
# estimating it; "auto" falls back to the silhouette-based estimate.
SPEAKER_COUNTS: set = {1, 2, 3, 4, "auto"}

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


class SpeakerUpdate(BaseModel):
    id: str
    label: str | None = None
    color: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    workflow_status: str | None = None
    # v5.8c: "Locutores" (1/2/3/4/"auto", default 1) -- declares the speaker
    # count for pipeline/speakers.py; re-transcribing (or transcribing for
    # the first time) after this is set runs diarization. `speakers` edits
    # the editable label/color of already-diarized speakers by id (ignores
    # unknown ids -- diarization hasn't produced them (yet), or never will
    # for speaker_count=1).
    speaker_count: int | str | None = None
    speakers: list[SpeakerUpdate] | None = None
    # Field bug follow-up (2026-07-25): per-project transcription language
    # override -- "auto" (default, falls through to the settings-level
    # transcription_language) or an ISO code that pins every clip in this
    # project, skipping whisper's per-clip auto-detect. See
    # pipeline/transcribe.py _resolve_language / LANGUAGE_CODES.
    language_override: str | None = None


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
    _backfill_reel_previews_once(pid, p)
    return p


# Field bug fix (v7.14 addendum, SEAM 2): "reel_previews" was only ever
# auto-enqueued right after a reels-producing pipeline stage (run-all,
# stage:reels) or a composition-changing PATCH (api/reels.py's reel_patch).
# Any project whose reels predate this feature -- or whose preview render
# failed/was interrupted before it could flip preview_ready -- never gets a
# second chance: GET /media/reel-preview/{id} 404s forever and the drawer
# is back to the exact dead-player bug v7.14 exists to fix, just via a
# different path. This is the cheapest read-side hook available: the GET
# project payload IS the drawer's data source (ui/tabs/reels.js reads
# r.preview_ready straight off it, per spec v7.14's own frontend note), so
# checking here catches "opened a project" the same way the store.py
# `_self_heal_legacy_paths` self-heal catches "loaded a project" -- same
# _healed_once-style guard, so this only ever inspects a given project's
# reels once per process (not on every single poll/refreshProject() tick),
# and it must never enqueue on a fully-fresh project (verified in
# scripts/test_reel_previews.py). Hooking store.load() itself (every
# pipeline stage and the queue worker's own project loads go through it)
# was considered and rejected: the "reel_previews" job itself calls
# store.load() while running, which would let this hook re-enqueue itself
# mid-run -- a self-perpetuating loop. The HTTP read path is not on that
# call graph, so it can't recurse into the job it just started.
_reel_preview_backfill_checked: set[str] = set()


def _backfill_reel_previews_once(pid: str, project: dict) -> None:
    if pid in _reel_preview_backfill_checked:
        return
    _reel_preview_backfill_checked.add(pid)

    reels_list = project.get("reels") or []
    if not reels_list:
        return

    stale = False
    for reel in reels_list:
        # In-memory only (mirrors api/reels.py's _load_reel): normalizes the
        # legacy single-window shape so reel_content_hash/_preview_is_current
        # have segments/transform to hash, without persisting a write for a
        # plain read.
        reels.ensure_segments(reel)
        if not reels._preview_is_current(project, reel):
            stale = True
            break

    if stale:
        queue.enqueue(pid, "reel_previews", {}, dedupe=True)


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
    if body.speaker_count is not None:
        if body.speaker_count not in SPEAKER_COUNTS:
            raise HTTPException(422, 'speaker_count must be one of 1, 2, 3, 4, "auto"')
        project["speaker_count"] = body.speaker_count
    if body.language_override is not None:
        if body.language_override not in LANGUAGE_CODES:
            raise HTTPException(422, f"language_override must be one of {LANGUAGE_CODES}")
        project["language_override"] = body.language_override
    if body.speakers is not None:
        by_id = {sp["id"]: sp for sp in project.get("speakers", [])}
        for upd in body.speakers:
            sp = by_id.get(upd.id)
            if sp is None:
                continue
            if upd.label is not None:
                label = upd.label.strip()
                if label:
                    sp["label"] = label
            if upd.color is not None:
                if not (isinstance(upd.color, str) and len(upd.color) == 7 and upd.color[0] == "#"):
                    raise HTTPException(422, "speaker color must be a #RRGGBB hex string")
                sp["color"] = upd.color
    store.save(project)
    return project


@router.post("/projects/{pid}/duplicate")
def project_duplicate(pid: str):
    """Duplicate a project: new id + dir, whole-dir copy (see
    store.duplicate_project for exactly what's copied vs reset). Returns the
    new project so the UI can list/select/open it without a second round
    trip."""
    try:
        return store.duplicate_project(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None


@router.delete("/projects/{pid}")
def project_delete(pid: str):
    """Deleting a project whose queue has a RUNNING item used to be able to
    kill the sole global queue worker thread forever (queue.py's
    _worker_loop except-handler re-raising store.ProjectNotFound a second
    time once the rmtree'd project.json vanished out from under its own
    bookkeeping) -- every future job in every project would then spin
    forever, since the one-shot _worker_started latch never respawned it.

    Fixed at the root in queue.py (every worker-internal store.load is now
    guarded, and _ensure_worker respawns a dead/missing worker), but we
    additionally ask the running job to cancel and give it a bounded window
    to actually stop touching this project first (queue.cancel_running_and_wait)
    so a delete-while-running is clean -- ffmpeg children torn down
    promptly, the item settles into a normal terminal status -- rather than
    merely "didn't crash the app"."""
    queue.cancel_running_and_wait(pid)
    store.delete_project(pid)
    return {"ok": True}


@router.post("/projects/{pid}/clips")
def clips_add(pid: str, body: AddClips):
    project = store.load(pid)
    added = ingest.add_clips(project, body.paths, camera_group=body.camera_group)
    return {"added": len(added), "clips": project["clips"]}


@router.post("/projects/{pid}/upload")
def clips_upload(
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
    (proxies/wav via stage:ingest, filmstrips/peaks via the thumbs kind).

    Plain `def`, not `async def` (finding 2): this used to `await f.read()`
    into a plain synchronous `out.write(chunk)` on every chunk, straight on
    the event loop -- for a multi-GB import that froze the ENTIRE app (every
    other request, every poll) for however long the copy took, since a
    single-process asyncio event loop can't do anything else while a sync
    call is running on it. FastAPI runs a plain `def` path operation in its
    threadpool automatically (the same mechanism every other route in this
    file already relies on being a plain `def`), so the fix is simply to
    stop being `async` and read via UploadFile.file (the underlying
    SpooledTemporaryFile) directly instead of the async
    read()/close() wrappers, which just proxy to a threadpool themselves
    once large enough to have rolled to disk (see starlette.datastructures.
    UploadFile) -- functionally identical bytes on disk, same response
    shape, just not blocking this thread's *particular* event loop turn."""
    project = store.load(pid)
    media_dir = store.project_dir(pid) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[Path, str]] = []
    for f in files:
        raw_name = (f.filename or "upload").replace("\\", "/").lstrip("/")
        parts = [p for p in raw_name.split("/") if p and p != ".."]
        if not parts:
            f.file.close()
            continue
        name = parts[-1]
        if Path(name).suffix.lower() not in ingest.MEDIA_EXTS:
            f.file.close()
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
            while chunk := f.file.read(_UPLOAD_CHUNK):
                out.write(chunk)
        f.file.close()
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
    # The clip set just changed -- drop this clip out of clip_order, clear
    # the cached edl, and un-done the order/render/reels stage badges so
    # they get recomputed against the new (smaller) clip set instead of
    # silently going stale (the live "62e6cae7" phantom-clip_order bug).
    ordering.invalidate_after_clipset_change(project)
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
