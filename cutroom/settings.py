"""Per-task model settings, persisted to ~/CutRoom/settings.json.

`null` for a task in `task_models` means "use default_model". Read via
`load()` / `model_for(task)`, written via `save(data)`. `cutroom/agents/agents.py`
calls `model_for` on every `get_agent()` call so changes apply without a
server restart.
"""

import json
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()

DEFAULT_MODEL = "qwen2.5:14b"

DEFAULTS: dict = {
    "default_model": DEFAULT_MODEL,
    "task_models": {
        "take_judge": None,
        "transcript_cleaner": None,
        "clip_order": None,
        "reel_scorer": None,
    },
    "whisper_model": config.WHISPER_MODEL,
}


def _path() -> Path:
    return config.DATA_DIR / "settings.json"


def load() -> dict:
    """Read settings.json, merged over DEFAULTS (missing/unknown keys are
    filled in / tolerated so the schema can grow without migrations)."""
    config.ensure_dirs()
    p = _path()
    data: dict = {}
    if p.exists():
        with _lock:
            try:
                data = json.loads(p.read_text())
            except Exception:
                data = {}
    merged = dict(DEFAULTS)
    merged.update(data)
    merged["task_models"] = {**DEFAULTS["task_models"], **(data.get("task_models") or {})}
    if not p.exists():
        save(merged)
    return merged


def save(data: dict) -> None:
    config.ensure_dirs()
    p = _path()
    with _lock:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(p)


def model_for(task: str) -> str:
    """Resolve the Ollama model to use for `task`: its own override, or
    default_model if unset/null."""
    data = load()
    return data["task_models"].get(task) or data["default_model"]
