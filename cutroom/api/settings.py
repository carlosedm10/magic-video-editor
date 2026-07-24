"""Settings API: read/write cutroom/settings.py-backed settings.json, plus a
GET /api/ollama/models proxy (Ollama's /api/tags) so the UI can populate model
pickers with whatever the user has actually pulled locally."""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from .. import settings as settings_store

router = APIRouter(prefix="/api", tags=["settings"])

TASKS = ("take_judge", "transcript_cleaner", "clip_order", "reel_scorer")


class SettingsIn(BaseModel):
    default_model: str | None = None
    task_models: dict[str, str | None] | None = None
    whisper_model: str | None = None


@router.get("/settings")
def get_settings():
    return settings_store.load()


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

    settings_store.save(current)
    return current


@router.get("/ollama/models")
def ollama_models():
    try:
        res = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        res.raise_for_status()
    except Exception as e:
        raise HTTPException(
            503, f"Ollama isn't reachable at {config.OLLAMA_URL} ({e}). Is it running?"
        ) from e

    models = []
    for m in res.json().get("models", []):
        size_bytes = m.get("size") or 0
        models.append(
            {
                "name": m.get("name") or m.get("model") or "",
                "size_gb": round(size_bytes / (1024**3), 1),
                "family": (m.get("details") or {}).get("family", ""),
            }
        )
    models.sort(key=lambda m: m["name"])
    return models
