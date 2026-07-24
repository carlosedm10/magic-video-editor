"""Voice-enhancement API: the project-level "Enhance voice" toggle (consumed
by render.run / reels.render_reel via cutroom/pipeline/audio_enhance.py) plus
an A/B preview endpoint that extracts a short sample from a clip, runs it
through the same enhance() used at render time, and hands back URLs for both
the original and enhanced wav so the UI can play them side by side."""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store
from ..ffmpeg_utils import FFmpegError, ffmpeg_bin, run
from ..pipeline import audio_enhance

router = APIRouter(prefix="/api", tags=["audio"])

PREVIEW_SECONDS = 10.0


class AudioEnhanceUpdate(BaseModel):
    enabled: bool


class AudioPreviewRequest(BaseModel):
    clip_id: str
    t: float = 0.0


@router.get("/audio/ping")
def audio_ping():
    return {"ok": True}


@router.post("/projects/{pid}/audio-enhance")
def audio_enhance_toggle(pid: str, body: AudioEnhanceUpdate):
    project = store.load(pid)
    project["audio_enhance"] = body.enabled
    store.save(project)
    return {"audio_enhance": project["audio_enhance"]}


def _extract_wav_segment(src: str, start: float, duration: float, dst: str) -> None:
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            src,
            "-vn",
            "-c:a",
            "pcm_s16le",
            dst,
        ]
    )


@router.post("/projects/{pid}/audio-preview")
def audio_preview(pid: str, body: AudioPreviewRequest):
    project = store.load(pid)
    try:
        clip = store.get_clip(project, body.clip_id)
    except KeyError:
        raise HTTPException(404, f"clip {body.clip_id} not found") from None

    work_dir = store.project_dir(pid) / "work"
    work_dir.mkdir(exist_ok=True)
    token = uuid.uuid4().hex[:8]
    original_path = work_dir / f"audio_preview_{token}_original.wav"
    enhanced_path = work_dir / f"audio_preview_{token}_enhanced.wav"

    try:
        _extract_wav_segment(clip["path"], body.t, PREVIEW_SECONDS, str(original_path))
        audio_enhance.enhance(str(original_path), str(enhanced_path))
    except FFmpegError as e:
        raise HTTPException(400, str(e)) from None

    def media_url(path):
        return f"/api/projects/{pid}/media/file?path={quote(str(path))}"

    return {
        "original_url": media_url(original_path),
        "enhanced_url": media_url(enhanced_path),
    }
