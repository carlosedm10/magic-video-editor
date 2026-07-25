"""Studio (manual editor) EDL API: read/write the persisted project["edl"]
(the ordered list of render segments render.run consumes), reset it back to
the AI-computed cut, and split a segment at a given absolute clip time.

Each segment may carry an optional "transition": the transition INTO that
segment (junction-level, chip sits between blocks in the timeline UI). For
the first segment, "fade" means fade-from-black; "crossfade" has no
predecessor and is ignored by the renderer. render.run (magic_video_editor/pipeline/
render.py) is the only consumer of this field.

Transitions catalog (spec v7.5): `type` now accepts, besides the legacy
"none"/"fade"/"crossfade", any named ffmpeg xfade transition from
GET /api/transitions (e.g. "circleopen", "dissolve", "pixelize", ...) —
validated against pipeline.render.valid_type_names() (the SAME source of
truth the catalog endpoint serves from — see that module's "transitions
catalog" section — so the accepted set can never drift from what's actually
advertised to the UI). render.py maps "fade" to the cheap per-segment
fade-to-black path and every other non-"none" type (including legacy
"crossfade") to an xfade merge at the junction."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import store
from ..pipeline import ordering
from ..pipeline import render as render_mod

router = APIRouter(prefix="/api", tags=["edl"])

TRANSITION_MIN_D = 0.2
TRANSITION_MAX_D = 1.5


class Transition(BaseModel):
    type: str = "none"
    duration: float = 0.5

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        valid = render_mod.valid_type_names()
        if v not in valid:
            raise ValueError(f"unknown transition type {v!r} (not in the transitions catalog)")
        return v


def _normalize_transition(t: Transition) -> Transition:
    if t.type == "none":
        return Transition(type="none", duration=0.5)
    duration = min(TRANSITION_MAX_D, max(TRANSITION_MIN_D, t.duration))
    return Transition(type=t.type, duration=duration)


class EdlSegment(BaseModel):
    clip_id: str
    start: float
    end: float
    text: str | None = ""
    transition: Transition = Field(default_factory=Transition)


class EdlUpdate(BaseModel):
    segments: list[EdlSegment]


class EdlSplit(BaseModel):
    index: int
    at: float


def _validate_segments(project: dict, segments: list[EdlSegment]) -> None:
    for seg in segments:
        try:
            clip = store.get_clip(project, seg.clip_id)
        except KeyError:
            raise HTTPException(400, f"clip {seg.clip_id} not found") from None
        duration = (clip.get("info") or {}).get("duration")
        if seg.start < 0 or seg.end <= seg.start:
            raise HTTPException(400, f"invalid segment range {seg.start}-{seg.end}")
        if duration is not None and seg.end > duration:
            raise HTTPException(
                400, f"segment end {seg.end} exceeds clip duration {duration}"
            )


@router.get("/projects/{pid}/edl")
def edl_get(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    segments = project.get("edl")
    if segments is None:
        segments = ordering.build_edl(project) if project.get("sentences") else []
        project["edl"] = segments
        store.save(project)
    return {"segments": segments}


@router.put("/projects/{pid}/edl")
def edl_put(pid: str, body: EdlUpdate):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    _validate_segments(project, body.segments)
    normalized = []
    for s in body.segments:
        d = s.model_dump()
        d["transition"] = _normalize_transition(s.transition).model_dump()
        normalized.append(d)
    project["edl"] = normalized
    store.save(project)
    return {"segments": project["edl"]}


@router.post("/projects/{pid}/edl/reset")
def edl_reset(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    segments = ordering.build_edl(project) if project.get("sentences") else []
    project["edl"] = segments
    store.save(project)
    return {"segments": segments}


@router.post("/projects/{pid}/edl/split")
def edl_split(pid: str, body: EdlSplit):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    segments = project.get("edl")
    if segments is None:
        segments = ordering.build_edl(project) if project.get("sentences") else []
    if not (0 <= body.index < len(segments)):
        raise HTTPException(400, f"index {body.index} out of range")
    seg = segments[body.index]
    if not (seg["start"] < body.at < seg["end"]):
        raise HTTPException(
            400, f"split point {body.at} must be strictly inside {seg['start']}-{seg['end']}"
        )
    text = seg.get("text", "") or ""
    # The transition INTO seg belongs to the first half (same junction before
    # it); the new mid-clip cut has no transition of its own.
    first = {**seg, "end": body.at}
    second = {
        **seg,
        "start": body.at,
        "text": text,
        "transition": {"type": "none", "duration": 0.5},
    }
    segments = segments[: body.index] + [first, second] + segments[body.index + 1 :]
    project["edl"] = segments
    store.save(project)
    return {"segments": segments}
