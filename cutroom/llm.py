"""Ollama client. The LLM only ever returns JSON decisions — it never touches media."""

import json

import httpx

from . import config


class LLMError(RuntimeError):
    pass


def available() -> bool:
    try:
        httpx.get(f"{config.OLLAMA_URL}/api/version", timeout=3)
        return True
    except Exception:
        return False


def chat_json(system: str, user: str, timeout: float = 300.0) -> dict | list:
    """One-shot chat forced into JSON output."""
    try:
        resp = httpx.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.2, "num_ctx": 16384},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama request failed ({config.OLLAMA_MODEL}): {e}") from e

    content = resp.json().get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned invalid JSON: {content[:400]}") from e
