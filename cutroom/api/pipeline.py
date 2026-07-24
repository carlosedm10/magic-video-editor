"""Stage-running endpoints: individual pipeline stages, the all-in-one
run-all job, reel rendering, and job status polling."""

from fastapi import APIRouter, HTTPException

from .. import jobs, store
from ..pipeline import ingest, ordering, reels, render, sync, takes, transcribe

router = APIRouter(prefix="/api", tags=["pipeline"])

STAGES = {
    "ingest": ingest.run,
    "sync": sync.run,
    "transcribe": transcribe.run,
    "takes": takes.run,
    "order": ordering.run,
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
    "render": "Editing the video",
    "reels": "Making shorts",
}


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
        except Exception as e:
            store.mark_stage(project, stage, "error", str(e)[:300])
            raise

    return {"job": jobs.start(f"{stage}:{pid}", task)}


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

    return {"job": jobs.start(f"run-all:{pid}", task)}


@router.post("/projects/{pid}/reels/{rid}/render")
def reel_render(pid: str, rid: str):
    project = store.load(pid)
    return {"job": jobs.start(f"reel:{rid}", lambda log: reels.render_reel(log, project, rid))}


@router.get("/jobs/{jid}")
def job_get(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    return job
