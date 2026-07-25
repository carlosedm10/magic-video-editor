"""Voice-enhancement API: the project-level "Enhance voice" toggle (consumed
by render.run / reels.render_reel via magic_video_editor/pipeline/audio_enhance.py),
the 8-band audio EQ (project["audio_eq"], consumed at render time via
magic_video_editor/pipeline/eq.py's build_audio_filter), and an A/B preview
endpoint that extracts a short sample from a clip, runs it through the same
enhance() used at render time plus the current EQ chain, and hands back URLs
for both the original and enhanced wav so the UI can play them side by
side. POST .../audio-preview-at is the newer, cursor-driven variant of the
same idea (spec v7.13 "audio preview UX"): given an absolute EDL/timeline
position it resolves the active segment's clip audio at that point and runs
ONLY audio_enhance.enhance() (no EQ -- matching what pipeline/render.py's
final render actually applies) over a short bounded window, for the
"Probar (desde el cursor)" button in ui/panels/audio.js.

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
from ..pipeline import audio_enhance, eq, ingest, sync

router = APIRouter(prefix="/api", tags=["audio"])

PREVIEW_SECONDS = 10.0
_UPLOAD_CHUNK = 1024 * 1024
GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 12.0

# ---------- cursor-position enhance preview (on-demand, spec v7.13 "audio
# preview UX") ----------
# "Enhance voice" is neural (DeepFilterNet3, see pipeline/audio_enhance.py)
# and can't run live in the browser like the 8-band EQ does -- today it only
# ever audibly applies at a real render/export. This endpoint lets the UI
# generate a short A/B sample (original vs. enhanced) of the CURRENT program
# audio at the player's cursor position, on demand, WITHOUT rendering the
# whole project -- reusing the exact same audio_enhance.enhance() chain
# render.py's _build() calls on the final render (see that module's
# `if project.get("audio_enhance"):` block). Deliberately does NOT also
# apply the 8-band EQ (unlike the older clip-picker /audio-preview below) --
# pipeline/render.py's actual final render never applies the EQ filter
# either, so this preview stays an honest 1:1 match of what enhance-voice
# alone sounds like on export.
PREVIEW_AT_DEFAULT_SECONDS = 8.0
PREVIEW_AT_MAX_SECONDS = 15.0
PREVIEW_AT_MIN_SECONDS = 0.3


class AudioPreviewAtCursorRequest(BaseModel):
    start_s: float = 0.0
    duration_s: float = PREVIEW_AT_DEFAULT_SECONDS

    @field_validator("start_s")
    @classmethod
    def _validate_start_s(cls, v):
        if v < 0:
            raise ValueError("start_s must be >= 0")
        return v

    @field_validator("duration_s")
    @classmethod
    def _validate_duration_s(cls, v):
        if v <= 0:
            raise ValueError("duration_s must be > 0")
        return min(v, PREVIEW_AT_MAX_SECONDS)


def _resolve_edl_position(segments: list[dict], start_s: float):
    """Map an absolute EDL/program-time cursor position to (segment,
    clip_local_start, remaining_in_segment) -- the same segment coordinate
    space pipeline/render.py._build walks (`seg["start"]`/`seg["end"]` are
    clip-local; program time is the cumulative sum of each segment's
    `end - start`). Caller is expected to have already checked start_s
    against the total program duration."""
    program_t = 0.0
    for seg in segments:
        seg_len = max(0.0, seg["end"] - seg["start"])
        if start_s < program_t + seg_len:
            offset_in_seg = max(0.0, start_s - program_t)
            clip_local_start = seg["start"] + offset_in_seg
            return seg, clip_local_start, seg_len - offset_in_seg
        program_t += seg_len
    # Floating-point edge case (start_s lands exactly on the total duration):
    # clamp into the tail of the last segment rather than raising.
    last = segments[-1]
    return last, last["end"], 0.0


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


@router.post("/projects/{pid}/audio-preview-at")
def audio_preview_at_cursor(pid: str, body: AudioPreviewAtCursorRequest):
    """On-demand "probar desde el cursor" preview: extracts a short window of
    the CURRENT program audio at `start_s` (an absolute EDL/timeline
    position, e.g. window.EditorUI.player.currentEdlTime() in the frontend)
    -- the active segment's clip audio at that position, same source
    resolution render.py._build uses (sync.audio_source_for for
    synced-external-audio clips, falling back to the clip's own audio) --
    runs it through audio_enhance.enhance() (the identical DeepFilterNet3 /
    -16 LUFS / limiter chain, with the same noisereduce fallback, used at
    final render), and returns both the enhanced sample and the untouched
    original window as small Range-servable wav files (via the existing
    media/file streaming route) for an A/B comparison in the UI. Bounded to
    a short window (default 8s, capped at 15s) and clipped to not cross a
    segment boundary -- this is a preview of the enhance chain's effect on
    ONE segment's audio, not a partial re-render of the assembled program.
    """
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None

    segments = project.get("edl") or []
    if not segments:
        raise HTTPException(
            400, "project has no EDL segments to preview yet — cut the project first"
        )

    total_duration = sum(max(0.0, s["end"] - s["start"]) for s in segments)
    if body.start_s >= total_duration:
        raise HTTPException(
            400,
            f"start_s ({body.start_s:.2f}) is beyond the program duration ({total_duration:.2f})",
        )

    seg, clip_local_start, remaining_in_seg = _resolve_edl_position(segments, body.start_s)
    if remaining_in_seg < PREVIEW_AT_MIN_SECONDS:
        raise HTTPException(400, "cursor is too close to the end of its segment to preview")
    duration = max(PREVIEW_AT_MIN_SECONDS, min(body.duration_s, remaining_in_seg))

    try:
        clip = store.get_clip(project, seg["clip_id"])
    except KeyError:
        raise HTTPException(404, f"clip {seg['clip_id']} not found") from None

    # Same source resolution as render.py._build: prefer a synced external
    # audio recording over the camera clip's own audio track, when present.
    audio_src, audio_start = clip["path"], clip_local_start
    synced = sync.audio_source_for(project, seg["clip_id"])
    if synced:
        ext_path, delta = synced
        ext_start = clip_local_start + delta
        if ext_start >= 0:
            audio_src, audio_start = ext_path, ext_start

    work_dir = store.project_dir(pid) / "work"
    work_dir.mkdir(exist_ok=True)
    token = uuid.uuid4().hex[:8]
    original_path = work_dir / f"audio_cursor_preview_{token}_original.wav"
    enhanced_path = work_dir / f"audio_cursor_preview_{token}_enhanced.wav"

    try:
        _extract_wav_segment(audio_src, audio_start, duration, str(original_path))
        audio_enhance.enhance(str(original_path), str(enhanced_path))
    except FFmpegError as e:
        raise HTTPException(400, str(e)) from None

    def media_url(path):
        return f"/api/projects/{pid}/media/file?path={quote(str(path))}"

    return {
        "clip_id": seg["clip_id"],
        "start_s": body.start_s,
        "duration_s": duration,
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
