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


def add_clips(project: dict, paths: list[str]) -> list[dict]:
    added = []
    existing = {c["path"] for c in project["clips"]}
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists() or p.suffix.lower() not in MEDIA_EXTS or str(p) in existing:
            continue
        imported = _import_into_project(p, project["id"])
        clip = {
            "id": uuid.uuid4().hex[:8],
            "path": str(imported),
            "source_path": str(p),
            "filename": p.name,
            "role": "audio" if p.suffix.lower() in AUDIO_EXTS else "camera",
            "is_main": False,
            "info": None,
            "wav": None,
            "transcript": None,
            "language": None,
        }
        project["clips"].append(clip)
        added.append(clip)
    if added and not any(c["is_main"] for c in project["clips"]):
        cams = [c for c in project["clips"] if c["role"] == "camera"]
        if cams:
            cams[0]["is_main"] = True
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


def run(log, project: dict) -> None:
    repair_clip_paths(log, project)
    pdir = store.project_dir(project["id"])
    wav_dir = pdir / "wav"
    wav_dir.mkdir(exist_ok=True)
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
        log.progress((i + 1) / len(clips))
        store.save(project)
    n_video = sum(1 for c in clips if c["info"]["has_video"])
    log(f"Ingested {len(clips)} files ({n_video} with video).")
