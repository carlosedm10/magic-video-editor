"""Color-filter API: persist project["color"] and serve a live filtered
preview frame (before/after) built from cutroom/pipeline/filters.build_vf,
which render.run / reels.render_reel already prepend to their vf chain."""

import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import ffmpeg_utils, store
from ..pipeline import filters

router = APIRouter(prefix="/api", tags=["filters"])


class ColorUpdate(BaseModel):
    preset: str = "none"
    brightness: float = 0
    contrast: float = 0
    saturation: float = 0
    temperature: float = 0


@router.get("/filters/ping")
def filters_ping():
    return {"ok": True}


@router.put("/projects/{pid}/color")
def color_put(pid: str, body: ColorUpdate):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    if body.preset not in filters.PRESETS:
        raise HTTPException(400, f"unknown preset {body.preset!r}")
    project["color"] = body.model_dump()
    store.save(project)
    return project["color"]


@router.get("/projects/{pid}/preview-frame")
def preview_frame(
    pid: str,
    clip_id: str,
    t: float = 0,
    preset: str = "none",
    brightness: float = 0,
    contrast: float = 0,
    saturation: float = 0,
    temperature: float = 0,
):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    try:
        clip = store.get_clip(project, clip_id)
    except KeyError:
        raise HTTPException(404, f"clip {clip_id} not found") from None

    if preset not in filters.PRESETS:
        raise HTTPException(400, f"unknown preset {preset!r}")

    vf = filters.build_vf(
        {
            "preset": preset,
            "brightness": brightness,
            "contrast": contrast,
            "saturation": saturation,
            "temperature": temperature,
        }
    )

    work = store.project_dir(pid) / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"preview_{int(time.time() * 1000)}.jpg"
    cmd = [
        ffmpeg_utils.ffmpeg_bin(),
        "-y",
        "-ss",
        f"{t:.3f}",
        "-i",
        clip["path"],
        "-frames:v",
        "1",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-q:v", "3", str(out)]
    ffmpeg_utils.run(cmd)

    return FileResponse(out, media_type="image/jpeg")
