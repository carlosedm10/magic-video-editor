"""Stage-running endpoints: individual pipeline stages, the all-in-one
run-all job, reel rendering, the job queue, and job status polling.

Actually running a stage/run-all/reel-render is delegated to magic_video_editor.queue:
this module's *_kind functions are the KIND_RUNNERS registered for
"stage:*", "run-all" and "reel_render:*" respectively (registered at import
time, at the bottom of this file), and the HTTP endpoints below just
enqueue + return the queue item -- the queue's one-item-per-project rule
replaces the old per-endpoint 409 (JobBusyError) guard."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import jobs, queue, store
from ..jobs import JobCancelled
from ..pipeline import ingest, ordering, reels, render, review, sync, takes, transcribe

router = APIRouter(prefix="/api", tags=["pipeline"])

STAGES = {
    "ingest": ingest.run,
    "sync": sync.run,
    "transcribe": transcribe.run,
    "takes": takes.run,
    "order": ordering.run,
    "review": review.run,
    "render": render.run,
    "reels": reels.suggest,
}
STAGE_ORDER = list(STAGES.keys())

# Friendly labels for the run-all progress panel (spec: Pipeline orchestration UX).
STAGE_LABELS = {
    "ingest": "Reading files",
    "sync": "Syncing cameras",
    "transcribe": "Transcribing",
    "takes": "Analyzing takes",
    "order": "Ordering the story",
    "review": "Checking for suggestions",
    "render": "Editing the video",
    "reels": "Making shorts",
}


# --------------------------------------------------------------------------
# KIND_RUNNERS: callable(log, project, payload) registered into magic_video_editor.queue
# --------------------------------------------------------------------------


def _run_stage_kind(log, project: dict, payload: dict) -> None:
    """Runner for queue kind "stage:<name>". Accepts stage either explicitly
    in payload["stage"] (what run_stage() below sends) or implicitly from
    the kind's suffix via payload["_kind"] (queue.py always injects this —
    covers callers that hit the generic POST .../queue endpoint with just
    {"kind": "stage:ingest"} and no payload)."""
    stage = payload.get("stage") or payload["_kind"].split(":", 1)[1]
    if stage not in STAGES:
        raise RuntimeError(f"unknown stage {stage}")
    fn = STAGES[stage]
    try:
        fn(log, project)
        store.mark_stage(project, stage, "done")
    except JobCancelled:
        store.mark_stage(project, stage, "error", "cancelled")
        raise
    except Exception as e:
        store.mark_stage(project, stage, "error", str(e)[:300])
        raise


class _StageLogProxy:
    """Wraps the queue item's log so a stage's own log.progress() calls
    update both that stage's entry in job["stages"] (via JobLog.stage) and
    the overall run-all job progress: (completed_stages + stage_frac) / total."""

    def __init__(self, log, stage: str, index: int, total: int):
        self._log = log
        self._stage = stage
        self._index = index
        self._total = total

    def __call__(self, msg: str) -> None:
        self._log(msg)

    def progress(self, frac: float) -> None:
        frac = min(1.0, max(0.0, frac))
        self._log.stage(self._stage, status="running", progress=frac)
        self._log.progress((self._index + frac) / self._total)


def _run_all_kind(log, project: dict, payload: dict) -> None:
    """Runner for queue kind "run-all": every stage in order. A failure in
    the final (reels) stage is reported but does not fail the item — any
    earlier failure stops the run and errors it, same as a single stage."""
    total = len(STAGE_ORDER)
    for name in STAGE_ORDER:
        log.stage(name, status="pending", progress=0.0)
    for i, stage in enumerate(STAGE_ORDER):
        fn = STAGES[stage]
        log(f"--- {STAGE_LABELS.get(stage, stage)} ---")
        log.stage(stage, status="running", progress=0.0)
        proxy = _StageLogProxy(log, stage, i, total)
        try:
            fn(proxy, project)
            store.mark_stage(project, stage, "done")
            log.stage(stage, status="done", progress=1.0)
            log.progress((i + 1) / total)
        except JobCancelled:
            store.mark_stage(project, stage, "error", "cancelled")
            log.stage(stage, status="error", progress=1.0)
            raise
        except Exception as e:
            store.mark_stage(project, stage, "error", str(e)[:300])
            log.stage(stage, status="error", progress=1.0)
            log(f"{stage} failed: {e}")
            if stage == "reels":
                # Last stage, non-critical: finish the run-all item as
                # successful even though reels errored.
                log.progress(1.0)
                return
            raise


def _run_reel_kind(log, project: dict, payload: dict) -> None:
    """Runner for queue kind prefix "reel_render:*" (e.g. "reel_render:ab12cd34").
    Same explicit-or-from-kind fallback as _run_stage_kind above."""
    rid = payload.get("reel_id") or payload["_kind"].split(":", 1)[1]
    reels.render_reel(log, project, rid)


queue.register_runner("stage:*", _run_stage_kind)
queue.register_runner("run-all", _run_all_kind)
queue.register_runner("reel_render:*", _run_reel_kind)


# --------------------------------------------------------------------------
# HTTP endpoints
# --------------------------------------------------------------------------


class ReorderBody(BaseModel):
    ids: list[str]


class EnqueueBody(BaseModel):
    kind: str
    payload: dict = {}


@router.post("/projects/{pid}/run/{stage}")
def run_stage(pid: str, stage: str):
    if stage not in STAGES:
        raise HTTPException(400, f"unknown stage {stage}")
    store.load(pid)  # raises if the project doesn't exist
    item = queue.enqueue(pid, f"stage:{stage}", {"stage": stage})
    return {"item": item}


@router.post("/projects/{pid}/run-all")
def run_all(pid: str):
    store.load(pid)
    item = queue.enqueue(pid, "run-all", {})
    return {"item": item}


@router.post("/projects/{pid}/reels/{rid}/render")
def reel_render(pid: str, rid: str):
    store.load(pid)
    item = queue.enqueue(pid, f"reel_render:{rid}", {"reel_id": rid})
    return {"item": item}


@router.post("/projects/{pid}/queue")
def queue_enqueue(pid: str, body: EnqueueBody):
    store.load(pid)
    item = queue.enqueue(pid, body.kind, body.payload)
    return {"item": item}


@router.get("/projects/{pid}/queue")
def queue_list(pid: str):
    store.load(pid)
    return {"queue": queue.list_queue(pid)}


@router.delete("/projects/{pid}/queue/{item_id}")
def queue_cancel(pid: str, item_id: str):
    store.load(pid)
    if not queue.cancel_item(pid, item_id):
        raise HTTPException(404, "queue item not found or already finished")
    return {"status": "ok"}


@router.post("/projects/{pid}/queue/reorder")
def queue_reorder(pid: str, body: ReorderBody):
    store.load(pid)
    return {"queue": queue.reorder(pid, body.ids)}


@router.get("/jobs/{jid}")
def job_get(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    return job


@router.post("/jobs/{jid}/cancel")
def job_cancel(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    if not jobs.cancel(jid):
        raise HTTPException(409, detail="job is not running")
    return {"status": "cancelling"}
