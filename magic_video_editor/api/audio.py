"""Voice-enhancement API: the project-level "Enhance voice" toggle (consumed
by render.run / reels.render_reel via magic_video_editor/pipeline/audio_enhance.py),
the 8-band audio EQ (project["audio_eq"], consumed at render time via
magic_video_editor/pipeline/eq.py's build_audio_filter), and an A/B preview
endpoint that extracts a short sample from a clip, runs it through the same
enhance() used at render time plus the current EQ chain, and hands back URLs
for both the original and enhanced wav so the UI can play them side by
side."""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from .. import store
from ..ffmpeg_utils import FFmpegError, ffmpeg_bin, run
from ..pipeline import audio_enhance, eq

router = APIRouter(prefix="/api", tags=["audio"])

PREVIEW_SECONDS = 10.0


class AudioEnhanceUpdate(BaseModel):
    enabled: bool


class AudioEqUpdate(BaseModel):
    gains: list[float]

    @field_validator("gains")
    @classmethod
    def _validate_gains(cls, v):
        if len(v) != eq.EQ_BAND_COUNT:
            raise ValueError(f"gains must have exactly {eq.EQ_BAND_COUNT} values")
        for g in v:
            if not (eq.EQ_MIN_DB <= g <= eq.EQ_MAX_DB):
                raise ValueError(f"each gain must be between {eq.EQ_MIN_DB} and {eq.EQ_MAX_DB} dB")
        return v


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


@router.get("/projects/{pid}/audio-eq")
def audio_eq_get(pid: str):
    project = store.load(pid)
    gains = eq.normalize_gains(project.get("audio_eq"))
    return {"gains": gains, "freqs": eq.EQ_FREQS_HZ}


@router.put("/projects/{pid}/audio-eq")
def audio_eq_put(pid: str, body: AudioEqUpdate):
    project = store.load(pid)
    project["audio_eq"] = eq.normalize_gains(body.gains)
    store.save(project)
    return {"gains": project["audio_eq"], "freqs": eq.EQ_FREQS_HZ}


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


def _apply_eq(src: str, dst: str, gains8) -> None:
    """Apply the 8-band EQ filter to a wav (src -> dst). Straight stream
    copy when the EQ is flat (no `-af`, since build_audio_filter returns "")."""
    filt = eq.build_audio_filter(gains8)
    cmd = [ffmpeg_bin(), "-y", "-i", src]
    if filt:
        cmd += ["-af", filt]
    cmd += ["-c:a", "pcm_s16le", dst]
    run(cmd)


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
    enhanced_raw_path = work_dir / f"audio_preview_{token}_enhanced_raw.wav"
    enhanced_path = work_dir / f"audio_preview_{token}_enhanced.wav"

    gains8 = eq.normalize_gains(project.get("audio_eq"))

    try:
        _extract_wav_segment(clip["path"], body.t, PREVIEW_SECONDS, str(original_path))
        audio_enhance.enhance(str(original_path), str(enhanced_raw_path))
        _apply_eq(str(enhanced_raw_path), str(enhanced_path), gains8)
    except FFmpegError as e:
        raise HTTPException(400, str(e)) from None

    def media_url(path):
        return f"/api/projects/{pid}/media/file?path={quote(str(path))}"

    return {
        "original_url": media_url(original_path),
        "enhanced_url": media_url(enhanced_path),
    }
