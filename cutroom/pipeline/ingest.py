"""Stage 1 — Ingest: probe every clip, extract analysis audio.
Originals are referenced in place, never copied or modified."""

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


def add_clips(project: dict, paths: list[str]) -> list[dict]:
    added = []
    existing = {c["path"] for c in project["clips"]}
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists() or p.suffix.lower() not in MEDIA_EXTS or str(p) in existing:
            continue
        clip = {
            "id": uuid.uuid4().hex[:8],
            "path": str(p),
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


def run(log, project: dict) -> None:
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
