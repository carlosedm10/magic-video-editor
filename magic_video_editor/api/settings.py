"""Settings API: read/write magic_video_editor/settings.py-backed settings.json.

The Ollama library/pull/installed-models endpoints live in api/ollama.py
(v4 section 5 -- the Models tab's model manager)."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import settings as settings_store

router = APIRouter(prefix="/api", tags=["settings"])

TASKS = (
    "take_judge",
    "transcript_cleaner",
    "clip_order",
    "reel_scorer",
    "reviewer",
    "dedup_judge",
)

SUBTITLE_STYLES = ("clean", "bold", "karaoke")
SUBTITLE_POSITIONS = ("bottom", "center")

# Field bug follow-up (2026-07-25): "auto" per-clip language auto-detection
# can misfire and TRANSLATE instead of transcribe. Shared with
# api/projects.py's per-project language_override (same value space).
LANGUAGE_CODES = ("auto", "es", "en", "fr", "de", "it", "pt", "ca")


class SettingsIn(BaseModel):
    default_model: str | None = None
    task_models: dict[str, str | None] | None = None
    whisper_model: str | None = None
    transcription_language: str | None = None
    export_dir: str | None = None
    subtitles: dict | None = None
    performance: dict | None = None
    brand_profile: str | None = None


@router.get("/settings")
def get_settings():
    return settings_store.load()


def _validate_export_dir(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(422, "export_dir must be a non-empty string")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, "export_dir must be an absolute path")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(422, f"export_dir isn't creatable: {e}") from e
    return str(path)


@router.put("/settings")
def put_settings(body: SettingsIn):
    current = settings_store.load()

    if body.default_model is not None:
        if not isinstance(body.default_model, str) or not body.default_model.strip():
            raise HTTPException(422, "default_model must be a non-empty string")
        current["default_model"] = body.default_model

    if body.task_models is not None:
        task_models = dict(current["task_models"])
        for task, model in body.task_models.items():
            if task not in TASKS:
                raise HTTPException(422, f"unknown task: {task}")
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise HTTPException(422, f"task_models.{task} must be a string or null")
            task_models[task] = model
        current["task_models"] = task_models

    if body.whisper_model is not None:
        if not isinstance(body.whisper_model, str) or not body.whisper_model.strip():
            raise HTTPException(422, "whisper_model must be a non-empty string")
        current["whisper_model"] = body.whisper_model

    if body.transcription_language is not None:
        if body.transcription_language not in LANGUAGE_CODES:
            raise HTTPException(
                422, f"transcription_language must be one of {LANGUAGE_CODES}"
            )
        current["transcription_language"] = body.transcription_language

    if body.export_dir is not None:
        current["export_dir"] = _validate_export_dir(body.export_dir)

    if body.brand_profile is not None:
        if not isinstance(body.brand_profile, str):
            raise HTTPException(422, "brand_profile must be a string")
        current["brand_profile"] = body.brand_profile

    if body.subtitles is not None:
        subs = dict(current["subtitles"])
        for key, value in body.subtitles.items():
            if key not in subs:
                raise HTTPException(422, f"unknown subtitles field: {key}")
            if key == "style" and value not in SUBTITLE_STYLES:
                raise HTTPException(422, f"subtitles.style must be one of {SUBTITLE_STYLES}")
            if key == "position" and value not in SUBTITLE_POSITIONS:
                raise HTTPException(
                    422, f"subtitles.position must be one of {SUBTITLE_POSITIONS}"
                )
            if key == "words_per_cue" and (not isinstance(value, int) or value < 1):
                raise HTTPException(422, "subtitles.words_per_cue must be a positive int")
            subs[key] = value
        current["subtitles"] = subs

    if body.performance is not None:
        perf = dict(current["performance"])
        for key, value in body.performance.items():
            if key not in perf:
                raise HTTPException(422, f"unknown performance field: {key}")
            if key == "max_parallel_ffmpeg" and (not isinstance(value, int) or value < 1):
                raise HTTPException(422, "performance.max_parallel_ffmpeg must be a positive int")
            if key == "ffmpeg_threads" and value is not None and (
                not isinstance(value, int) or value < 1
            ):
                raise HTTPException(
                    422, "performance.ffmpeg_threads must be a positive int or null"
                )
            if key == "min_free_ram_gb" and (
                not isinstance(value, int | float) or value < 0
            ):
                raise HTTPException(
                    422, "performance.min_free_ram_gb must be a non-negative number"
                )
            perf[key] = value
        current["performance"] = perf

    settings_store.save(current)
    return current
