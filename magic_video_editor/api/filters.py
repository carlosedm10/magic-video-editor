"""Color-filter API (v5.7): persist project["color"] (new professional
schema, see pipeline/filters.py), serve a live filtered preview frame
(before/after), and manage the settings-level LUT library (~/.../luts/)."""

import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import ffmpeg_utils, settings, store
from ..pipeline import filters

router = APIRouter(prefix="/api", tags=["filters"])

_LUT_EXTS = (".cube", ".3dl", ".png")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]*$")


class LutConfig(BaseModel):
    name: str | None = None
    intensity: float = 1.0


class ColorUpdate(BaseModel):
    preset: str = "none"
    exposure: float = 0
    temperature: float = 0
    tint: float = 0
    black_point: float = 0
    white_point: float = 0
    brightness: float = 0
    contrast: float = 0
    saturation: float = 0
    vibrance: float = 0
    sharpness: float = 0
    lut: LutConfig = Field(default_factory=LutConfig)


@router.get("/filters/ping")
def filters_ping():
    return {"ok": True}


def _sanitize_lut_name(name: str) -> str:
    name = Path(name).name  # strip any directory components
    if not name or not _SAFE_NAME.match(name) or Path(name).suffix.lower() not in _LUT_EXTS:
        raise HTTPException(400, f"invalid LUT filename {name!r}")
    return name


def _dedup_path(dst_dir: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = dst_dir / name
    n = 2
    while candidate.exists():
        candidate = dst_dir / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


@router.put("/projects/{pid}/color")
def color_put(pid: str, body: ColorUpdate):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    if body.preset not in filters.PRESETS:
        raise HTTPException(400, f"unknown preset {body.preset!r}")
    if body.lut.name:
        lut_path = settings.luts_dir() / _sanitize_lut_name(body.lut.name)
        if not lut_path.is_file():
            raise HTTPException(400, f"LUT {body.lut.name!r} not found in the library")
    project["color"] = body.model_dump()
    store.save(project)
    return project["color"]


@router.get("/projects/{pid}/preview-frame")
def preview_frame(
    pid: str,
    clip_id: str,
    t: float = 0,
    preset: str = "none",
    exposure: float = 0,
    temperature: float = 0,
    tint: float = 0,
    black_point: float = 0,
    white_point: float = 0,
    brightness: float = 0,
    contrast: float = 0,
    saturation: float = 0,
    vibrance: float = 0,
    sharpness: float = 0,
    lut_name: str | None = None,
    lut_intensity: float = 1.0,
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
            "exposure": exposure,
            "temperature": temperature,
            "tint": tint,
            "black_point": black_point,
            "white_point": white_point,
            "brightness": brightness,
            "contrast": contrast,
            "saturation": saturation,
            "vibrance": vibrance,
            "sharpness": sharpness,
            "lut": {"name": lut_name, "intensity": lut_intensity},
        },
        lut_dir=settings.luts_dir(),
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


@router.get("/luts")
def luts_list():
    d = settings.luts_dir()
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in _LUT_EXTS:
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
    return out


@router.post("/luts/import")
async def luts_import(
    request: Request,
    file: UploadFile | None = File(None),  # noqa: B008 (standard FastAPI upload idiom)
    path: str | None = Form(None),  # noqa: B008
):
    """Multipart upload of a .cube/.3dl/.png file, OR a JSON body
    {"path": "/abs/path/to.cube"} for the native-dialog flow (pywebview
    already gave the user a real file picker; we just copy from disk)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body") from None
        path = body.get("path")
        file = None

    d = settings.luts_dir()

    if file is not None:
        name = _sanitize_lut_name(file.filename or "")
        dst = _dedup_path(d, name)
        with dst.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        return {"name": dst.name, "size": dst.stat().st_size}

    if path:
        src = Path(path).expanduser()
        if not src.is_file():
            raise HTTPException(400, f"file not found: {path}")
        name = _sanitize_lut_name(src.name)
        dst = _dedup_path(d, name)
        shutil.copyfile(src, dst)
        return {"name": dst.name, "size": dst.stat().st_size}

    raise HTTPException(400, "provide either a multipart `file` or a `path`")


@router.delete("/luts/{name}")
def luts_delete(name: str):
    safe = _sanitize_lut_name(name)
    p = settings.luts_dir() / safe
    if not p.is_file():
        raise HTTPException(404, f"LUT {name!r} not found")
    p.unlink()
    return {"deleted": safe}
