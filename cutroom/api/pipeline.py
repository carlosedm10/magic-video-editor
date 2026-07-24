"""Stage-running endpoints: individual pipeline stages, the all-in-one
run-all job, reel rendering, and job status polling."""

from fastapi import APIRouter, HTTPException

from .. import jobs, store
from ..jobs import JobBusyError, JobCancelled
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


def _busy(e: JobBusyError) -> HTTPException:
    return HTTPException(
        409,
        detail={"message": "a job is already running for this project", "job": e.job_id},
    )


@router.post("/projects/{pid}/run/{stage}")
def run_stage(pid: str, stage: str):
    if stage not in STAGES:
        raise HTTPException(400, f"unknown stage {stage}")
    project = store.load(pid)
    fn = STAGES[stage]

    def task(log, project=project, stage=stage):
        try:
            fn(log, project)
            store.mark_stage(project, stage, "done")
        except JobCancelled:
            store.mark_stage(project, stage, "error", "cancelled")
            raise
        except Exception as e:
            store.mark_stage(project, stage, "error", str(e)[:300])
            raise

    try:
        return {"job": jobs.start(f"{stage}:{pid}", task, lock_key=pid)}
    except JobBusyError as e:
        raise _busy(e) from e


class _StageLogProxy:
    """Wraps the job's JobLog so a stage's own log.progress() calls update
    both that stage's entry in job['stages'] (via JobLog.stage) and the
    overall run-all job progress: (completed_stages + stage_frac) / total."""

    def __init__(self, log: jobs.JobLog, stage: str, index: int, total: int):
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


@router.post("/projects/{pid}/run-all")
def run_all(pid: str):
    """One background job running every stage in order. A failure in the
    final (reels) stage is reported but does not fail the job — any earlier
    failure stops the run and errors the job, same as today's per-stage run."""
    project = store.load(pid)

    def task(log, project=project):
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
                    # Last stage, non-critical: finish the run-all job as
                    # successful even though reels errored.
                    log.progress(1.0)
                    return
                raise

    try:
        return {"job": jobs.start(f"run-all:{pid}", task, lock_key=pid)}
    except JobBusyError as e:
        raise _busy(e) from e


@router.post("/projects/{pid}/reels/{rid}/render")
def reel_render(pid: str, rid: str):
    project = store.load(pid)
    # Same per-project lock as run/{stage} and run-all: without it, a reel
    # render can race a concurrent pipeline job's store.save() on the same
    # project.json (last-write-wins), the same class of bug as two servers
    # sharing a project directory.
    try:
        return {
            "job": jobs.start(
                f"reel:{rid}", lambda log: reels.render_reel(log, project, rid), lock_key=pid
            )
        }
    except JobBusyError as e:
        raise _busy(e) from e


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
