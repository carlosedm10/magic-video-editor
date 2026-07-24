"""Studio (manual editor) EDL API: read/write the persisted project["edl"]
(the ordered list of render segments render.run consumes), reset it back to
the AI-computed cut, and split a segment at a given absolute clip time."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store
from ..pipeline import ordering

router = APIRouter(prefix="/api", tags=["edl"])


class EdlSegment(BaseModel):
    clip_id: str
    start: float
    end: float
    text: str | None = ""


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
    project["edl"] = [s.model_dump() for s in body.segments]
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
    first = {**seg, "end": body.at}
    second = {**seg, "start": body.at, "text": text}
    segments = segments[: body.index] + [first, second] + segments[body.index + 1 :]
    project["edl"] = segments
    store.save(project)
    return {"segments": segments}
