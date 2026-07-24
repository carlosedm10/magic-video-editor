"""Ollama integration: installed models, the model manager's library search
(proxy of https://ollama.com/search, v4 section 5), pull (streamed into a
background job) and delete.

Library search is best-effort HTML scraping with a 1h in-process cache; any
parse failure or network error falls back to a small curated catalog so the
Models tab always has something to show."""

import re
import threading
import time

import httpx
import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from .. import jobs as jobs_module

router = APIRouter(prefix="/api/ollama", tags=["ollama"])

_CACHE_TTL_S = 60 * 60
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict], bool]] = {}  # query -> (fetched_at, entries, live)

# Fallback catalog used when the ollama.com scrape fails or is offline.
# size_gb are the well-known approximate download sizes for these tags.
_STATIC_CATALOG: list[dict] = [
    {
        "name": "qwen2.5",
        "description": "Qwen2.5 general-purpose chat/instruct models.",
        "tags": [
            {"tag": "7b", "size_gb": 4.7},
            {"tag": "14b", "size_gb": 9.0},
            {"tag": "32b", "size_gb": 20.0},
        ],
    },
    {
        "name": "qwen3",
        "description": "Qwen3 general-purpose chat/instruct models.",
        "tags": [
            {"tag": "8b", "size_gb": 5.2},
            {"tag": "14b", "size_gb": 9.3},
            {"tag": "32b", "size_gb": 20.0},
        ],
    },
    {
        "name": "llama3.1",
        "description": "Meta Llama 3.1 instruct models.",
        "tags": [
            {"tag": "8b", "size_gb": 4.7},
            {"tag": "70b", "size_gb": 40.0},
        ],
    },
    {
        "name": "llama3.2",
        "description": "Meta Llama 3.2 small instruct models.",
        "tags": [
            {"tag": "1b", "size_gb": 1.3},
            {"tag": "3b", "size_gb": 2.0},
        ],
    },
    {
        "name": "llama3.3",
        "description": "Meta Llama 3.3 instruct model.",
        "tags": [
            {"tag": "70b", "size_gb": 40.0},
        ],
    },
    {
        "name": "gemma2",
        "description": "Google Gemma 2 instruct models.",
        "tags": [
            {"tag": "2b", "size_gb": 1.6},
            {"tag": "9b", "size_gb": 5.4},
            {"tag": "27b", "size_gb": 16.0},
        ],
    },
    {
        "name": "gemma3",
        "description": "Google Gemma 3 instruct models.",
        "tags": [
            {"tag": "4b", "size_gb": 2.9},
            {"tag": "12b", "size_gb": 8.1},
            {"tag": "27b", "size_gb": 17.0},
        ],
    },
    {
        "name": "mistral",
        "description": "Mistral 7B instruct model.",
        "tags": [
            {"tag": "7b", "size_gb": 4.1},
        ],
    },
    {
        "name": "phi4",
        "description": "Microsoft Phi-4 reasoning-tuned model.",
        "tags": [
            {"tag": "14b", "size_gb": 9.1},
        ],
    },
    {
        "name": "deepseek-r1",
        "description": "DeepSeek-R1 distilled reasoning models.",
        "tags": [
            {"tag": "7b", "size_gb": 4.7},
            {"tag": "8b", "size_gb": 4.9},
            {"tag": "14b", "size_gb": 9.0},
            {"tag": "32b", "size_gb": 20.0},
        ],
    },
]


def _filter_catalog(catalog: list[dict], q: str) -> list[dict]:
    if not q:
        return catalog
    needle = q.strip().lower()
    return [
        m
        for m in catalog
        if needle in m["name"].lower() or needle in m.get("description", "").lower()
    ]


def _parse_ollama_search_html(html: str) -> list[dict]:
    """Best-effort, liberal parse of ollama.com/search results. The page is
    server-rendered React; model names appear as /library/<name> links. We
    don't attempt to parse per-tag sizes from the search page (not present
    there) -- callers merge in size_gb from the static catalog when a name
    matches, else fall back to the whole static catalog on total failure."""
    names = re.findall(r'/library/([a-zA-Z0-9._-]+)"', html)
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    if not seen:
        raise ValueError("no models parsed from ollama.com/search response")

    by_name = {m["name"]: m for m in _STATIC_CATALOG}
    entries = []
    for name in seen:
        if name in by_name:
            entries.append(by_name[name])
        else:
            entries.append(
                {
                    "name": name,
                    "description": "",
                    "tags": [{"tag": "latest", "size_gb": None}],
                }
            )
    return entries


def _compatibility(size_gb: float | None, ram_gb: float) -> str | None:
    if size_gb is None:
        return None
    if size_gb <= ram_gb * 0.5:
        return "great"
    if size_gb <= ram_gb * 0.75:
        return "tight"
    return "too_big"


def _fetch_library(q: str) -> tuple[list[dict], bool]:
    """Returns (entries, was_live) -- was_live False means the static
    fallback catalog was used (offline or parse failure)."""
    try:
        res = httpx.get("https://ollama.com/search", params={"q": q} if q else {}, timeout=6)
        res.raise_for_status()
        entries = _parse_ollama_search_html(res.text)
        return entries, True
    except Exception:
        return _filter_catalog(_STATIC_CATALOG, q), False


@router.get("/library")
def ollama_library(q: str = ""):
    cache_key = q.strip().lower()
    now = time.time()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_S:
            _, entries, live = cached
        else:
            entries, live = _fetch_library(q)
            _cache[cache_key] = (now, entries, live)

    ram_gb = psutil.virtual_memory().total / (1024**3)
    out = []
    for m in entries:
        tags = [
            {
                "tag": t["tag"],
                "size_gb": t["size_gb"],
                "compatibility": _compatibility(t["size_gb"], ram_gb),
            }
            for t in m["tags"]
        ]
        out.append({"name": m["name"], "description": m.get("description", ""), "tags": tags})
    return {"models": out, "live": live, "ram_gb": round(ram_gb, 1)}


@router.get("/models")
def ollama_models():
    """Installed models (proxy of Ollama's /api/tags)."""
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


class PullRequest(BaseModel):
    model: str


def _run_pull(log, model: str) -> None:
    log(f"pulling {model}")
    try:
        with httpx.stream(
            "POST",
            f"{config.OLLAMA_URL}/api/pull",
            json={"name": model, "stream": True},
            timeout=None,
        ) as res:
            res.raise_for_status()
            for line in res.iter_lines():
                if not line:
                    continue
                import json as _json

                try:
                    data = _json.loads(line)
                except ValueError:
                    continue
                status = data.get("status", "")
                total = data.get("total")
                completed = data.get("completed")
                if total and completed is not None:
                    log.progress(completed / total)
                if status:
                    log(status)
                if data.get("error"):
                    raise RuntimeError(data["error"])
    except httpx.HTTPError as e:
        raise RuntimeError(f"ollama pull failed: {e}") from e
    log(f"done pulling {model}")


def _start_pull_job(model: str) -> str:
    """Ollama model pulls aren't project-scoped (cutroom/queue.py's FIFO
    lives in project["queue"] and its item kinds are all
    stage/run-all/preview/final/reel_render/thumbs — a pull has no pid), so
    this always uses the plain background-job runner rather than the
    per-project queue."""
    return jobs_module.start("ollama_pull", _run_pull, model, lock_key=f"ollama_pull:{model}")


@router.post("/pull")
def ollama_pull(body: PullRequest):
    if not body.model or not body.model.strip():
        raise HTTPException(422, "model must be a non-empty string")
    try:
        job_id = _start_pull_job(body.model.strip())
    except jobs_module.JobBusyError as e:
        return {"job_id": e.job_id, "already_running": True}
    return {"job_id": job_id}


@router.delete("/models/{name}")
def ollama_delete_model(name: str):
    try:
        res = httpx.request(
            "DELETE", f"{config.OLLAMA_URL}/api/delete", json={"name": name}, timeout=10
        )
        if res.status_code == 404:
            raise HTTPException(404, f"model {name} not found")
        res.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"ollama delete failed: {e}") from e
    return {"deleted": name}
