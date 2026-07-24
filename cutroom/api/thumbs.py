"""Timeline-asset endpoints (spec v4 #4): serves the filmstrip sprite,
its grid metadata, and the audio-peaks JSON that cutroom/pipeline/thumbs.py
generates into <project>/thumbs/.

Mounted in server.py. Also imports cutroom.pipeline.thumbs (for its side
effect only -- registering the "thumbs" queue runner at module-import time):
nothing else in the app's import graph ever touches that module (it's not a
pipeline STAGE, just a queue kind), so without this import here its
`queue.register_runner("thumbs", ...)` call never runs and every enqueued
thumbs item errors with "no runner registered for queue kind 'thumbs'"
(reproduced live before this import was added)."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import store
from ..pipeline import thumbs as _thumbs_pipeline  # noqa: F401  (registers queue runner)

router = APIRouter(prefix="/api", tags=["thumbs"])


def _clip_thumbs(pid: str, cid: str) -> dict:
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "project not found") from None
    try:
        clip = store.get_clip(project, cid)
    except KeyError:
        raise HTTPException(404, f"clip {cid} not found") from None
    thumbs = clip.get("thumbs")
    if not thumbs:
        raise HTTPException(404, "thumbs not generated for this clip yet")
    return thumbs


@router.get("/projects/{pid}/thumbs/{cid}/strip")
def thumbs_strip(pid: str, cid: str):
    thumbs = _clip_thumbs(pid, cid)
    path = thumbs.get("strip")
    if not path:
        raise HTTPException(404, "clip has no filmstrip (no video track)")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/projects/{pid}/thumbs/{cid}/meta")
def thumbs_meta(pid: str, cid: str):
    thumbs = _clip_thumbs(pid, cid)
    path = thumbs.get("meta")
    if not path:
        raise HTTPException(404, "clip has no filmstrip meta (no video track)")
    return json.loads(Path(path).read_text())


@router.get("/projects/{pid}/thumbs/{cid}/peaks")
def thumbs_peaks(pid: str, cid: str):
    thumbs = _clip_thumbs(pid, cid)
    path = thumbs.get("peaks")
    if not path:
        return []
    return json.loads(Path(path).read_text())
