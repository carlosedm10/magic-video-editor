"""Ollama availability check. The actual editorial LLM calls live in
cutroom/agents/ as pydantic_ai agents — they only ever return typed decisions
(schemas.py), never touch media."""

import httpx

from . import config


def available() -> bool:
    try:
        httpx.get(f"{config.OLLAMA_URL}/api/version", timeout=3)
        return True
    except Exception:
        return False
