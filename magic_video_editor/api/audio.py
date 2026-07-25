"""Voice-enhancement API: the project-level "Enhance voice" toggle (consumed
by render.run / reels.render_reel via magic_video_editor/pipeline/audio_enhance.py),
the 8-band audio EQ (project["audio_eq"], consumed at render time via
magic_video_editor/pipeline/eq.py's build_audio_filter), and an A/B preview
endpoint that extracts a short sample from a clip, runs it through the same
enhance() used at render time plus the current EQ chain, and hands back URLs
for both the original and enhanced wav so the UI can play them side by
side.

vNext "Main audio track" (music bed with auto-ducking) additive section:
CRUD for project["audio_assets"] (imported .mp3/.wav/.m4a music files -- see
pipeline/ingest.py's add_audio_assets/register_uploaded_audio_assets, a
SEPARATE list from project["clips"]/the camera-clip pipeline) and
project["audio_track"] (the single main-audio-track placement: {asset_id,
start_s, gain_db, ducking}, or null). Mixed into the final render + reels'
audio by pipeline/render.py's _apply_music_bed (reused by
pipeline/reels.py's render_reel) -- this router only ever persists the
config, never touches ffmpeg."""

import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, field_validator

from .. import config, store
from ..ffmpeg_utils import FFmpegError, ffmpeg_bin, run
from ..pipeline import audio_enhance, eq, ingest

router = APIRouter(prefix="/api", tags=["audio"])

PREVIEW_SECONDS = 10.0
_UPLOAD_CHUNK = 1024 * 1024
GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 12.0


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


# ---------- main audio track / music bed (vNext) ----------


class AddAudioAssets(BaseModel):
    paths: list[str]


class AudioTrackUpdate(BaseModel):
    asset_id: str
    start_s: float = 0.0
    gain_db: float | None = None
    ducking: bool = True

    @field_validator("start_s")
    @classmethod
    def _validate_start(cls, v):
        if v < 0:
            raise ValueError("start_s must be >= 0")
        return v

    @field_validator("gain_db")
    @classmethod
    def _validate_gain(cls, v):
        if v is not None and not (GAIN_MIN_DB <= v <= GAIN_MAX_DB):
            raise ValueError(f"gain_db must be between {GAIN_MIN_DB} and {GAIN_MAX_DB}")
        return v


@router.get("/projects/{pid}/audio-assets")
def audio_assets_list(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    return {"audio_assets": project.get("audio_assets") or []}


@router.post("/projects/{pid}/audio-assets")
def audio_assets_add(pid: str, body: AddAudioAssets):
    """Native path-based import (pywebview picker / "add by path" power-user
    link) -- mirrors POST /projects/{pid}/clips but into the separate
    audio_assets list (pipeline/ingest.py.add_audio_assets)."""
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    added = ingest.add_audio_assets(project, body.paths)
    return {"added": len(added), "audio_assets": project.get("audio_assets") or []}


@router.post("/projects/{pid}/audio-assets/upload")
def audio_assets_upload(
    pid: str,
    files: list[UploadFile] = File(...),  # noqa: B008 (standard FastAPI upload idiom)
):
    """Browser-mode fallback (drag&drop / hidden file input) for importing
    music-bed audio, streamed straight to disk exactly like
    api/projects.py's clips_upload (plain `def`, not `async def` -- same
    threadpool reasoning documented there: a multi-GB write must never block
    the single asyncio event loop)."""
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None

    media_dir = store.project_dir(pid) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for f in files:
        raw_name = (f.filename or "upload").replace("\\", "/").lstrip("/")
        name = next((p for p in reversed(raw_name.split("/")) if p and p != ".."), None)
        if not name or Path(name).suffix.lower() not in ingest.MUSIC_EXTS:
            f.file.close()
            continue

        stem, suffix = Path(name).stem, Path(name).suffix
        dest = media_dir / name
        n = 1
        while dest.exists():
            dest = media_dir / f"{stem}_{n}{suffix}"
            n += 1

        with open(dest, "wb") as out:
            while chunk := f.file.read(_UPLOAD_CHUNK):
                out.write(chunk)
        f.file.close()
        saved.append(dest)

    added = ingest.register_uploaded_audio_assets(project, saved)
    return {"added": len(added), "audio_assets": project.get("audio_assets") or []}


@router.delete("/projects/{pid}/audio-assets/{aid}")
def audio_asset_remove(pid: str, aid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    project["audio_assets"] = [a for a in project.get("audio_assets") or [] if a["id"] != aid]
    # An audio_track pointing at the asset just removed would otherwise dangle
    # (render.py's _apply_music_bed already no-ops defensively on a missing
    # asset, but clearing it here keeps the UI's state honest too).
    if (project.get("audio_track") or {}).get("asset_id") == aid:
        project["audio_track"] = None
    store.save(project)
    return {"ok": True}


@router.get("/projects/{pid}/audio-track")
def audio_track_get(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    return {"audio_track": project.get("audio_track")}


@router.put("/projects/{pid}/audio-track")
def audio_track_put(pid: str, body: AudioTrackUpdate):
    """Sets the ONE main-audio-track placement (spec vNext): project
    ["audio_track"] = {asset_id, start_s, gain_db, ducking}. `gain_db` omitted
    -> config.MUSIC_GAIN_DEFAULT_DB (the sensible music-bed default when
    first placed); the UI can PUT again with an explicit value once the user
    adjusts the gain control."""
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    if not any(a["id"] == body.asset_id for a in project.get("audio_assets") or []):
        raise HTTPException(400, f"audio asset {body.asset_id} not found")
    gain_db = body.gain_db if body.gain_db is not None else config.MUSIC_GAIN_DEFAULT_DB
    project["audio_track"] = {
        "asset_id": body.asset_id,
        "start_s": body.start_s,
        "gain_db": gain_db,
        "ducking": body.ducking,
    }
    store.save(project)
    return {"audio_track": project["audio_track"]}


@router.delete("/projects/{pid}/audio-track")
def audio_track_clear(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    project["audio_track"] = None
    store.save(project)
    return {"ok": True}
