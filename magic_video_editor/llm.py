"""Ollama availability check. The actual editorial LLM calls live in
magic_video_editor/agents/ as pydantic_ai agents — they only ever return typed decisions
(schemas.py), never touch media."""

import httpx

from . import config, ollama_manager


def available() -> bool:
    try:
        httpx.get(f"{config.OLLAMA_URL}/api/version", timeout=3)
        return True
    except Exception:
        return False


def mode() -> str:
    """Which Ollama is currently serving config.OLLAMA_URL: "system" (an
    external Ollama the user already had running -- always preferred),
    "bundled" (our packaged binary, spawned by
    ollama_manager.ensure_ollama() at server startup), or "unreachable".
    v6 packaging Option B (see ollama_manager.py)."""
    return ollama_manager.current_mode()
