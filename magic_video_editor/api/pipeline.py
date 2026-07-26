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

from .. import jobs, queue, settings, store
from ..jobs import JobCancelled
from ..pipeline import (
    ingest,
    judge,
    ordering,
    paragraphs,
    reels,
    render,
    review,
    sync,
    takes,
    transcribe,
)
from . import ollama as ollama_api

router = APIRouter(prefix="/api", tags=["pipeline"])

STAGES = {
    "ingest": ingest.run,
    "sync": sync.run,
    "transcribe": transcribe.run,
    "takes": takes.run,
    "order": ordering.run,
    "paragraphs": paragraphs.run,
    "review": review.run,
    "judge": judge.run,
    "render": render.run,
    "reels": reels.suggest,
}

# Shorts (spec vNext "shorts are a separate, explicit step"): the reels stage
# is no longer part of run-all -- the main cut (render) is the last run-all
# step, and "reels" is only ever run standalone via POST
# /projects/{pid}/run/reels (or the generic .../queue {"kind":"stage:reels"}
# endpoint), typically triggered by the Reels tab's "Generar shorts a partir
# del vídeo final" button once the user is happy with the final cut. STAGES
# above still lists "reels" (run_stage()'s validation and _run_stage_kind
# both key off STAGES, not STAGE_ORDER) -- only run-all's own STAGE_ORDER
# excludes it.
STAGE_ORDER = [s for s in STAGES if s != "reels"]

# Friendly labels for the run-all progress panel (spec: Pipeline orchestration UX).
STAGE_LABELS = {
    "ingest": "Reading files",
    "sync": "Syncing cameras",
    "transcribe": "Transcribing",
    "takes": "Analyzing takes",
    "order": "Ordering the story",
    "paragraphs": "Marking paragraph breaks",
    "review": "Checking for suggestions",
    "judge": "Judging the edit",
    "render": "Editing the video",
    "reels": "Making shorts",
}


# --------------------------------------------------------------------------
# KIND_RUNNERS: callable(log, project, payload) registered into magic_video_editor.queue
# --------------------------------------------------------------------------

# Root-cause fix (2026-07-25): which settings.py task(s) each LLM-backed
# stage resolves a model for (see the get_agent(...) calls in the matching
# magic_video_editor/pipeline/*.py module). Stages not listed here (ingest,
# sync, transcribe, render) never touch an Ollama agent and are skipped by
# _preflight_stage below. This is the ONE place both _run_stage_kind and
# _run_all_kind go through before invoking a stage, so every LLM stage gets
# the same preflight regardless of whether it's run individually or as part
# of run-all.
LLM_TASKS_BY_STAGE: dict[str, list[str]] = {
    "takes": [
        "transcript_cleaner",
        "take_sequencer",
        "video_topic",
        "context_check",
        "dedup_judge",
        "take_judge",
        # Root-cause fix (2026-07-26): blooper_reviewer (pipeline/takes.py's
        # full-clip review pass) was calling get_agent() without ever going
        # through this preflight -- an oversized/uninstalled model for this
        # task could attempt a real Ollama load (and swap an 8GB Mac) before
        # its own try/except ever got a chance to fail open.
        "blooper_reviewer",
    ],
    "order": [
        "clip_order",
        # Root-cause fix (2026-07-26): clip_digest (pipeline/ordering.py's
        # per-clip compression for oversized projects) had the same gap as
        # blooper_reviewer above -- same fix. Note this preflights the
        # *configured* clip_digest/clip_order models only; the opportunistic
        # "thinking model" upgrade in ordering._resolve_ordering_model is
        # separately gated by its own non-raising model_installed_and_fits()
        # check right before use, so it never contradicts this hard guard.
        "clip_digest",
    ],
    "paragraphs": ["paragraph_break"],
    "review": ["reviewer"],
    "judge": ["edit_judge"],
    "reels": [
        "reel_composer",
        "reel_scorer",
        # Same bug class as clip_digest/blooper_reviewer above, found during
        # the 2026-07-26 audit: reel_dedup (pipeline/reels.py's dedup pass)
        # runs inside this stage but was never preflighted either.
        "reel_dedup",
    ],
}


def _preflight_stage(stage: str) -> None:
    """Preflight guard: before an LLM-backed stage actually runs, verify its
    resolved model(s) are reachable/installed/fit RAM (see
    api/ollama.py's preflight_check_models). Runs INSIDE the job (called
    from the queue runners below, never from the FastAPI event loop) so a
    failure becomes a visible job/stage error instead of a raw ollama
    error or, on an oversized model, a silent hang while the Mac swaps.
    No-ops for stages that don't call any agent."""
    tasks = LLM_TASKS_BY_STAGE.get(stage)
    if not tasks:
        return
    models = {settings.model_for(t) for t in tasks}
    ollama_api.preflight_check_models(models)


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
        _preflight_stage(stage)
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
    """Runner for queue kind "run-all": every stage in STAGE_ORDER, which ends
    at "render" (the finished main cut) -- "reels"/shorts generation is a
    separate, explicit, standalone step (see STAGE_ORDER's comment above) and
    is never run as part of this. Any stage failure stops the run and errors
    it."""
    total = len(STAGE_ORDER)
    for name in STAGE_ORDER:
        log.stage(name, status="pending", progress=0.0)
    for i, stage in enumerate(STAGE_ORDER):
        fn = STAGES[stage]
        log(f"--- {STAGE_LABELS.get(stage, stage)} ---")
        log.stage(stage, status="running", progress=0.0)
        proxy = _StageLogProxy(log, stage, i, total)
        try:
            _preflight_stage(stage)
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
