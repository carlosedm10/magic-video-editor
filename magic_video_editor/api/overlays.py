"""Manual overlay track API (spec v5.9b): CRUD for project["overlays"] — a
second timeline track (video-over-video / PiP), strictly manual.

    project["overlays"] = [{id, clip_id, t_start (timeline seconds),
        duration, clip_in (source offset seconds), x, y (0..1 fractions of
        frame), scale (0..1 fraction of frame width), opacity (0..1)}]

THIS ROUTER IS THE ONLY WRITER OF project["overlays"] IN THE WHOLE APP. The
AI pipeline (takes/order/reviewer/etc.) must NEVER create, modify, or delete
overlay items — enforce this by construction: no other module imports or
mutates project["overlays"]. magic_video_editor/pipeline/render.py only
READS this list (to apply overlays after concat, both final and preview
renders).

v1 constraints (spec): no audio from overlays (main audio always wins,
overlay clips' audio is simply never mapped by the renderer); an overlay
must lie within the final cut's duration.

NOTE for the integrator: this router needs mounting in server.py:

    from .api import overlays as overlays_api
    app.include_router(overlays_api.router)
    (and add `overlays` to the Makefile `smoke` import list alongside edl)
"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import store

router = APIRouter(prefix="/api", tags=["overlays"])

MIN_SCALE = 0.02
MAX_SCALE = 1.0
EPS = 0.05  # tolerance for float rounding at range/duration boundaries


class OverlayItem(BaseModel):
    id: str | None = None
    clip_id: str
    t_start: float
    duration: float
    clip_in: float = 0.0
    x: float = 0.0
    y: float = 0.0
    scale: float = 0.3
    opacity: float = 1.0


class OverlaysUpdate(BaseModel):
    overlays: list[OverlayItem] = Field(default_factory=list)


def _cut_duration(project: dict) -> float:
    """Nominal duration of the final cut (sum of EDL segment lengths). This
    is the same "total" render.run reports on completion; it doesn't shave
    off the few hundred ms a crossfade junction merges away, which is fine
    for a validation bound. 0.0 (no EDL yet) skips the bound rather than
    rejecting every overlay outright."""
    edl = project.get("edl") or []
    return sum(max(0.0, seg["end"] - seg["start"]) for seg in edl)


def _validate_overlays(project: dict, overlays: list[OverlayItem]) -> None:
    cut_duration = _cut_duration(project)
    for ov in overlays:
        try:
            clip = store.get_clip(project, ov.clip_id)
        except KeyError:
            raise HTTPException(400, f"clip {ov.clip_id} not found") from None

        if ov.duration <= 0:
            raise HTTPException(400, "duration must be > 0")
        if ov.clip_in < 0:
            raise HTTPException(400, "clip_in must be >= 0")
        if ov.t_start < 0:
            raise HTTPException(400, "t_start must be >= 0")

        clip_duration = (clip.get("info") or {}).get("duration")
        if clip_duration and ov.clip_in + ov.duration > clip_duration + EPS:
            raise HTTPException(
                400,
                f"overlay window [{ov.clip_in}, {ov.clip_in + ov.duration}] exceeds "
                f"clip {ov.clip_id} duration {clip_duration:.1f}s",
            )
        if cut_duration > 0 and ov.t_start + ov.duration > cut_duration + EPS:
            raise HTTPException(
                400,
                f"overlay window [{ov.t_start}, {ov.t_start + ov.duration}] exceeds "
                f"the final cut's duration {cut_duration:.1f}s",
            )

        if not (0.0 <= ov.x <= 1.0):
            raise HTTPException(400, "x must be within 0..1")
        if not (0.0 <= ov.y <= 1.0):
            raise HTTPException(400, "y must be within 0..1")
        if not (MIN_SCALE <= ov.scale <= MAX_SCALE):
            raise HTTPException(400, f"scale must be within {MIN_SCALE}..{MAX_SCALE}")
        if not (0.0 <= ov.opacity <= 1.0):
            raise HTTPException(400, "opacity must be within 0..1")


@router.get("/projects/{pid}/overlays")
def overlays_get(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    return {"overlays": project.get("overlays") or []}


@router.put("/projects/{pid}/overlays")
def overlays_put(pid: str, body: OverlaysUpdate):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    _validate_overlays(project, body.overlays)

    normalized = []
    for ov in body.overlays:
        d = ov.model_dump()
        d["id"] = d["id"] or uuid.uuid4().hex[:8]
        normalized.append(d)

    project["overlays"] = normalized
    store.save(project)
    return {"overlays": project["overlays"]}
