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

from .. import ffmpeg_utils, store

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


def _new_clip_record(imported: Path, source: Path, group: str) -> dict:
    """The one clip-dict shape, shared by add_clips (path-based import) and
    register_uploaded_clips (bytes already streamed onto disk by the v5.3
    upload endpoint) so both register clips identically."""
    return {
        "id": uuid.uuid4().hex[:8],
        "path": str(imported),
        "source_path": str(source),
        "filename": imported.name,
        "role": "audio" if imported.suffix.lower() in AUDIO_EXTS else "camera",
        "camera_group": group,
        "is_main": False,
        "info": None,
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


def _enqueue_analyze_for_new_clips(project: dict, added: list[dict]) -> None:
    """v7.3: once the pipeline has already completed, newly added CAMERA
    clips each get their own `analyze_clip:<id>` queue item -- import/proxy/
    thumbs/wav, transcription, and the per-clip cleaner+sequencer for that
    clip ONLY, followed by a placement suggestion (never an auto-cut/auto-
    reorder; see magic_video_editor/pipeline/placement.py, which registers the
    "analyze_clip:*" runner). Skipped for a project's very first batch of
    clips -- those go through the normal run-all/manual stage flow. Audio-only
    clips (role != "camera") aren't part of clip_order/EDL, so they're left
    for the next full run instead."""
    if not added or not _completed_pipeline(project):
        return
    from .. import queue

    for clip in added:
        if clip["role"] != "camera":
            continue
        queue.enqueue(project["id"], f"analyze_clip:{clip['id']}", {"clip_id": clip["id"]})


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
    store.save(project)
    _enqueue_analyze_for_new_clips(project, added)
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
    store.save(project)
    _enqueue_analyze_for_new_clips(project, added)
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
