"""Stage 1 — Ingest: probe every clip, extract analysis audio.

Clips are imported into the project's own `media/` directory (hardlink when
possible, else a copy) so that render-time ffmpeg access never depends on the
original file staying put or staying readable — this sidesteps macOS TCC
sandboxing on ~/Downloads, ~/Desktop, ~/Documents and lets the user move/delete
the source afterwards."""

import os
import shutil
import uuid
from pathlib import Path

from .. import ffmpeg_utils, queue, store
from . import ordering

MEDIA_EXTS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".mts",
    ".m4a",
    ".wav",
    ".mp3",
    ".aac",
    ".flac",
}
AUDIO_EXTS = {".m4a", ".wav", ".mp3", ".aac", ".flac"}

# Main audio track / music bed (vNext "MUSIC BED WITH AUTO-DUCKING"): files
# imported through add_audio_assets/register_uploaded_audio_assets below
# become project["audio_assets"] entries -- deliberately a SEPARATE list from
# project["clips"], never touched by the camera-clip pipeline (build_edl/
# ordering/takes all filter role=="camera" over project["clips"]; an
# audio_assets entry has no "role" field and isn't in that list at all, so
# it structurally can't leak in). No proxy/thumbs/transcribe -- those are
# camera-clip concerns this import path skips entirely. Deliberately a
# strict subset of AUDIO_EXTS/MEDIA_EXTS above (this is a distinct import
# path -- api/audio.py's /audio-assets endpoints -- from the existing
# role="audio" external-mic-sync clips that go through add_clips/MEDIA_EXTS).
MUSIC_EXTS = {".mp3", ".wav", ".m4a"}


def _import_into_project(src: Path, project_id: str) -> Path:
    """Hardlink (instant, same-volume) or copy `src` into
    `<project_dir>/media/<name>`, deduping name collisions with a numeric
    suffix. Returns the destination path."""
    media_dir = store.project_dir(project_id) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dst = media_dir / src.name
    if dst.exists() and dst.resolve() != src.resolve():
        stem, suffix = src.stem, src.suffix
        n = 1
        while dst.exists() and dst.resolve() != src.resolve():
            dst = media_dir / f"{stem}_{n}{suffix}"
            n += 1
    if dst.exists() and dst.resolve() == src.resolve():
        return dst
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return dst


def set_main_group(project: dict, group: str) -> None:
    """Mark every clip whose camera_group == `group` as the main camera,
    clearing is_main on every other clip (exactly one GROUP is main)."""
    for c in project["clips"]:
        c["is_main"] = c.get("camera_group") == group


def _probe_info_for_import(path: Path) -> dict | None:
    """Best-effort ffmpeg_utils.clip_info() probe run at IMPORT time (not just
    at the "ingest" pipeline STAGE's own deferred probe, ~line 324 below) so a
    freshly-imported clip has a usable duration immediately -- the manual
    editor (Editor.insertClip, ui/editor/state.js) needs clip.info.duration to
    build a whole-clip segment, and previously ONLY the pipeline populated it,
    which meant "drag a clip onto the timeline" silently no-op'd until at
    least one pipeline run. Returns None (not raises) on an unreadable/corrupt
    file -- the clip still imports with info=None, same as before this fix,
    and the ingest stage's own probe (which already has to tolerate this) gets
    another chance later; this never aborts the batch (mirrors
    add_audio_assets' try/except a few functions down)."""
    try:
        return ffmpeg_utils.clip_info(str(path))
    except Exception:
        return None


def _new_clip_record(imported: Path, source: Path, group: str) -> dict:
    """The one clip-dict shape, shared by add_clips (path-based import) and
    register_uploaded_clips (bytes already streamed onto disk by the v5.3
    upload endpoint) so both register clips identically. `info` is probed
    right here at import time (see _probe_info_for_import) rather than left
    for the pipeline's "ingest" stage to fill in later -- see that function's
    docstring for why."""
    return {
        "id": uuid.uuid4().hex[:8],
        "path": str(imported),
        "source_path": str(source),
        "filename": imported.name,
        "role": "audio" if imported.suffix.lower() in AUDIO_EXTS else "camera",
        "camera_group": group,
        "is_main": False,
        "info": _probe_info_for_import(imported),
        "wav": None,
        "transcript": None,
        "language": None,
    }


def _finalize_main_group(project: dict, added: list[dict]) -> None:
    """First batch of clips ever added to a project auto-picks a main camera
    group (the first camera clip's group) if nothing is main yet."""
    if added and not any(c["is_main"] for c in project["clips"]):
        cams = [c for c in project["clips"] if c["role"] == "camera"]
        if cams:
            set_main_group(project, cams[0]["camera_group"])


def _completed_pipeline(project: dict) -> bool:
    """v7.3 "Incremental clip addition": true once the narrative order or the
    final render has completed at least once -- the signal that this project
    is past its first (full run-all/manual) pass and any further clips added
    are an INCREMENTAL addition, not part of the initial import."""
    stages = project.get("stages", {})
    return any(stages.get(s, {}).get("status") == "done" for s in ("order", "render"))


def _enqueue_analyze_for_new_clips(
    project: dict, added: list[dict], pipeline_was_completed: bool
) -> None:
    """v7.3: once the pipeline has already completed, newly added CAMERA
    clips each get their own `analyze_clip:<id>` queue item -- import/proxy/
    thumbs/wav, transcription, and the per-clip cleaner+sequencer for that
    clip ONLY, followed by a placement suggestion (never an auto-cut/auto-
    reorder; see magic_video_editor/pipeline/placement.py, which registers the
    "analyze_clip:*" runner). Audio-only clips (role != "camera") aren't part
    of clip_order/EDL, so they're left for the next full run instead.

    `pipeline_was_completed` is `_completed_pipeline(project)` evaluated by
    the caller BEFORE ordering.invalidate_after_clipset_change() ran on this
    same project dict. That invalidation un-marks the order/render/reels
    stage badges the moment the clip set changes (by design -- see its
    docstring), so re-deriving _completed_pipeline(project) here, AFTER
    invalidation, would always see a just-invalidated project and could never
    observe "pipeline already completed" -- silently killing this whole
    incremental path. The caller must capture the flag pre-invalidation and
    pass it straight through.

    A project's very first batch used to be skipped entirely here (left for
    the normal run-all/manual stage flow) -- but manual editing is now
    first-class and doesn't require ever running the pipeline, so a
    first-batch clip that needs an H.264 preview proxy (HEVC/10-bit iPhone
    .MOV etc., see _proxy_needed) would otherwise never get one: server.py's
    media_preview then has nothing to serve but the undecodable original,
    which decodes audio but not video in Chromium/WKWebView -- permanent
    black screen + sound (see docs/PLATFORM-SPEC.md streaming section). The
    full analyze_clip pass (transcription + a placement suggestion) doesn't
    make sense yet here though: _ask_placement already no-ops with no
    narrative order, and there's no reason to pay for transcription before
    the user has run anything. So the first batch instead only gets a
    lightweight `make_proxy:<id>` job per clip that actually needs a proxy
    (see run_make_proxy below) -- idempotent with ingest.run's own
    proxy-making step (same "proxy" not in clip / _proxy_needed check), so a
    later Run Pipeline just no-ops for any clip already proxied here."""
    if not added:
        return

    if pipeline_was_completed:
        for clip in added:
            if clip["role"] != "camera":
                continue
            queue.enqueue(project["id"], f"analyze_clip:{clip['id']}", {"clip_id": clip["id"]})
        return

    for clip in added:
        info = clip.get("info")
        if not info or not info.get("has_video"):
            continue
        if _proxy_needed(info):
            queue.enqueue(project["id"], f"make_proxy:{clip['id']}", {"clip_id": clip["id"]})


def run_make_proxy(log, project: dict, payload: dict) -> None:
    """KIND_RUNNERS callable for queue kind "make_proxy:<clip_id>" -- the
    lightweight first-batch counterpart to placement.run_analyze_clip
    ("analyze_clip:*"): makes ONLY the preview proxy for one clip, nothing
    else (no wav/transcribe/placement). Idempotent and safe to run even if
    the full pipeline (ingest.run) or a later analyze_clip job races it --
    both share the exact same "proxy" not in clip / _proxy_needed gate, so
    whichever runs first wins and the other no-ops."""
    clip_id = payload.get("clip_id") or payload["_kind"].split(":", 1)[1]
    clip = store.get_clip(project, clip_id)

    if "proxy" in clip:
        return  # already handled (pipeline run, or a previous make_proxy job)

    info = clip.get("info")
    if info is None:
        try:
            info = ffmpeg_utils.clip_info(clip["path"])
        except Exception as e:
            log(f"Could not probe {clip['filename']}: {e}")
            return
        clip["info"] = info
        store.save(project)

    if not info.get("has_video"):
        return
    if not _proxy_needed(info):
        clip["proxy"] = None
        store.save(project)
        return

    log(f"Generating preview proxy for {clip['filename']}")
    media_dir = store.project_dir(project["id"]) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    proxy_path = media_dir / f"{clip['id']}_proxy.mp4"
    ffmpeg_utils.make_proxy(clip["path"], str(proxy_path), info.get("fps") or 0)
    clip["proxy"] = str(proxy_path)
    store.save(project)


def add_clips(project: dict, paths: list[str], camera_group: str | None = None) -> list[dict]:
    """paths may be files or directories. A directory expands to its media
    files (sorted by name) and — unless `camera_group` is given — uses the
    directory NAME as the camera_group for all of them. Loose files default
    to camera_group "main" (or the explicit override)."""
    added = []
    existing = {c["path"] for c in project["clips"]}
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            files = sorted(
                f for f in p.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXTS
            )
            group = camera_group or p.name
        else:
            files = [p]
            group = camera_group or "main"
        for f in files:
            if not f.exists() or f.suffix.lower() not in MEDIA_EXTS or str(f) in existing:
                continue
            imported = _import_into_project(f, project["id"])
            clip = _new_clip_record(imported, f, group)
            project["clips"].append(clip)
            added.append(clip)
            existing.add(str(f))
    _finalize_main_group(project, added)
    pipeline_was_completed = _completed_pipeline(project)
    if added:
        ordering.invalidate_after_clipset_change(project)
    store.save(project)
    _enqueue_analyze_for_new_clips(project, added, pipeline_was_completed)
    return added


def register_uploaded_clips(project: dict, saved: list[tuple[Path, str]]) -> list[dict]:
    """Register files the v5.3 upload endpoint already streamed straight
    onto disk inside <project>/media/ -- exactly the same clip-dict shape
    and main-group defaulting as add_clips, minus the hardlink/copy step
    (there's no separate source path to import from; the upload IS the
    destination). `saved` is (dest_path_in_media_dir, camera_group) pairs."""
    added = []
    existing = {c["path"] for c in project["clips"]}
    for dest, group in saved:
        if dest.suffix.lower() not in MEDIA_EXTS or str(dest) in existing:
            continue
        clip = _new_clip_record(dest, dest, group)
        project["clips"].append(clip)
        added.append(clip)
        existing.add(str(dest))
    _finalize_main_group(project, added)
    pipeline_was_completed = _completed_pipeline(project)
    if added:
        ordering.invalidate_after_clipset_change(project)
    store.save(project)
    _enqueue_analyze_for_new_clips(project, added, pipeline_was_completed)
    return added


def _new_audio_asset_record(imported: Path) -> dict:
    info = ffmpeg_utils.clip_info(str(imported))
    return {
        "id": uuid.uuid4().hex[:8],
        "path": str(imported),
        "filename": imported.name,
        "duration": round(info.get("duration") or 0.0, 3),
    }


def add_audio_assets(project: dict, paths: list[str]) -> list[dict]:
    """Path-based import of music-bed audio files (native pywebview picker /
    "add by path" power-user link) into project["audio_assets"]. Mirrors
    add_clips' hardlink-import (_import_into_project, same macOS-TCC
    sidestep) but appends to the separate audio_assets list -- never to
    project["clips"] -- and skips proxy/thumbs/wav-extract/transcribe
    entirely (see MUSIC_EXTS comment above)."""
    added = []
    existing = {a["path"] for a in project.get("audio_assets", [])}
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.is_file() or p.suffix.lower() not in MUSIC_EXTS or str(p) in existing:
            continue
        imported = _import_into_project(p, project["id"])
        try:
            asset = _new_audio_asset_record(imported)
        except Exception:
            continue  # unreadable/unprobeable file -- skip rather than fail the whole batch
        project.setdefault("audio_assets", []).append(asset)
        added.append(asset)
        existing.add(str(p))
    if added:
        store.save(project)
    return added


def register_uploaded_audio_assets(project: dict, saved: list[Path]) -> list[dict]:
    """Same as add_audio_assets, for files api/audio.py's upload endpoint
    (v5.3-style streaming multipart, browser-mode drag&drop/file-picker
    fallback for the music bed) already streamed straight onto disk."""
    added = []
    existing = {a["path"] for a in project.get("audio_assets", [])}
    for dest in saved:
        if dest.suffix.lower() not in MUSIC_EXTS or str(dest) in existing:
            continue
        try:
            asset = _new_audio_asset_record(dest)
        except Exception:
            continue
        project.setdefault("audio_assets", []).append(asset)
        added.append(asset)
        existing.add(str(dest))
    if added:
        store.save(project)
    return added


def _is_readable(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def repair_clip_paths(log, project: dict) -> None:
    """Self-healing for projects created before the import-on-add fix (or
    whose media dir got orphaned): re-import any clip whose path isn't
    inside the project dir, or that ffmpeg can't read (TCC denial etc.)."""
    pdir = store.project_dir(project["id"])
    media_root = pdir / "media"
    changed = False
    for clip in project["clips"]:
        path = clip.get("path")
        if not path:
            continue
        p = Path(path)
        try:
            inside_project = media_root.resolve() in p.resolve().parents
        except OSError:
            inside_project = False
        if inside_project and _is_readable(path):
            continue
        source = Path(clip.get("source_path") or path).expanduser()
        if not source.exists():
            continue
        log(f"Repairing clip path for {clip.get('filename', source.name)}")
        try:
            imported = _import_into_project(source, project["id"])
        except OSError as e:
            log(f"Could not repair {clip.get('filename', source.name)}: {e}")
            continue
        clip["source_path"] = str(source)
        clip["path"] = str(imported)
        changed = True
    if changed:
        store.save(project)


def _backfill_camera_groups(project: dict) -> None:
    """Legacy clips created before camera_group existed default to "main"."""
    changed = False
    for clip in project["clips"]:
        if not clip.get("camera_group"):
            clip["camera_group"] = "main"
            changed = True
    if changed:
        store.save(project)


def _proxy_needed(info: dict) -> bool:
    """Skip proxy generation for clips already safely browser-playable:
    h264 + yuv420p + <=1080p tall. Anything else (HEVC, 10-bit, 4K, etc.)
    gets an H.264 preview proxy so Chrome's <video> player can decode it."""
    return not (
        info.get("codec_name") == "h264"
        and info.get("pix_fmt") == "yuv420p"
        and info.get("height", 0) <= 1080
    )


def run(log, project: dict) -> None:
    repair_clip_paths(log, project)
    _backfill_camera_groups(project)
    pdir = store.project_dir(project["id"])
    wav_dir = pdir / "wav"
    media_dir = pdir / "media"
    wav_dir.mkdir(exist_ok=True)
    media_dir.mkdir(exist_ok=True)
    clips = project["clips"]
    if not clips:
        raise RuntimeError("No clips added yet.")
    for i, clip in enumerate(clips):
        if clip["info"] is None:
            log(f"Probing {clip['filename']}")
            clip["info"] = ffmpeg_utils.clip_info(clip["path"])
        if clip["wav"] is None and clip["info"]["has_audio"]:
            log(f"Extracting analysis audio from {clip['filename']}")
            wav = wav_dir / f"{clip['id']}.wav"
            ffmpeg_utils.extract_wav(clip["path"], str(wav))
            clip["wav"] = str(wav)
        if clip["info"]["has_video"] and "proxy" not in clip:
            if _proxy_needed(clip["info"]):
                log(f"Generating preview proxy for {clip['filename']}")
                proxy_path = media_dir / f"{clip['id']}_proxy.mp4"
                ffmpeg_utils.make_proxy(clip["path"], str(proxy_path), clip["info"]["fps"])
                clip["proxy"] = str(proxy_path)
            else:
                clip["proxy"] = None
        log.progress((i + 1) / len(clips))
        store.save(project)
    n_video = sum(1 for c in clips if c["info"]["has_video"])
    log(f"Ingested {len(clips)} files ({n_video} with video).")


queue.register_runner("make_proxy:*", run_make_proxy)
