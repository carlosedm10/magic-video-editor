"""Ollama integration: installed models, the model manager's library search
(proxy of https://ollama.com/search, v4 section 5, fixed up per v5.1), pull
(streamed into a background job), delete, and a hardware-aware model
recommendation.

v5.1 rewrite: the search page itself carries no tag sizes, so a query's
first few results are enriched EAGERLY by fetching each model's own tags
page (https://ollama.com/library/<name>/tags) for real tag names + sizes;
the rest are enriched LAZILY via GET /api/ollama/library/<name>/tags on
demand (e.g. when the UI expands a result). Both search results and tags
pages are best-effort HTML scraping, cached per-key, and fall back to a
small curated catalog (also used verbatim as the empty-query default view)
on any parse failure or network error."""

import re
import threading
import time

import httpx
import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, ollama_manager
from .. import jobs as jobs_module

router = APIRouter(prefix="/api/ollama", tags=["ollama"])

# Finding #10: a stalled `ollama pull` used to hang the job forever
# (timeout=None) -- Install button stuck disabled, no error, no cancel.
# Bounded connect/read/write timeout so a stall raises promptly instead;
# read is the effective idle timeout since httpx re-arms it on every line
# received, so a healthy (but slow) real pull is unaffected.
_PULL_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

_SEARCH_CACHE_TTL_S = 60 * 60
_TAGS_CACHE_TTL_S = 24 * 60 * 60
_EAGER_ENRICH_COUNT = 6

_cache_lock = threading.Lock()
_search_cache: dict[str, tuple[float, list[dict], bool]] = {}  # q -> (fetched_at, entries, live)
_tags_cache: dict[str, tuple[float, list[dict], bool]] = {}  # name -> (fetched_at, tags, live)

# Fallback catalog used when the ollama.com scrape fails or is offline, AND
# as the curated default view for an empty query (spec: "default view = a
# curated popular list, not whatever the scrape returns first"). size_gb are
# the well-known approximate download sizes for these tags.
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
_STATIC_BY_NAME = {m["name"]: m for m in _STATIC_CATALOG}


def _filter_catalog(catalog: list[dict], q: str) -> list[dict]:
    if not q:
        return catalog
    needle = q.strip().lower()
    return [
        m
        for m in catalog
        if needle in m["name"].lower() or needle in m.get("description", "").lower()
    ]


# --------------------------------------------------------------------------
# Search page parsing (names + descriptions only -- no sizes here)
# --------------------------------------------------------------------------

_SEARCH_ITEM_RE = re.compile(r'href="/library/([a-zA-Z0-9._-]+)"\s+class="group w-full">')
_SEARCH_DESC_RE = re.compile(r'<p class="max-w-lg[^"]*"[^>]*>(.*?)</p>', re.DOTALL)


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = (
        text.replace("&#39;", "'")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


def _parse_ollama_search_html(html: str) -> list[dict]:
    """Best-effort, liberal parse of ollama.com/search results: model name +
    its description paragraph (previously dropped -- v5.1 fix). No sizes:
    the search page doesn't carry per-tag sizes, those come from
    _fetch_tags() per model (eagerly for the first few results, lazily for
    the rest -- see ollama_library() below)."""
    matches = list(_SEARCH_ITEM_RE.finditer(html))
    if not matches:
        raise ValueError("no models parsed from ollama.com/search response")

    entries = []
    seen: set[str] = set()
    for i, m in enumerate(matches):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        window_end = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 1500
        window = html[m.end() : window_end]
        dm = _SEARCH_DESC_RE.search(window)
        description = _clean_html_text(dm.group(1)) if dm else ""
        entries.append({"name": name, "description": description})
    return entries


def _fetch_search(q: str) -> tuple[list[dict], bool]:
    """Returns (entries, was_live) -- was_live False means the static
    fallback catalog was used (offline or parse failure). Live entries have
    no "tags" yet; the caller fills those in (eager/lazy, see below)."""
    try:
        res = httpx.get("https://ollama.com/search", params={"q": q} if q else {}, timeout=6)
        res.raise_for_status()
        return _parse_ollama_search_html(res.text), True
    except Exception:
        catalog = _filter_catalog(_STATIC_CATALOG, q)
        return [{"name": m["name"], "description": m["description"]} for m in catalog], False


# --------------------------------------------------------------------------
# Per-model tags page parsing (real tag names + sizes)
# --------------------------------------------------------------------------

_TAG_LINK_RE = re.compile(r'href="/library/[a-zA-Z0-9._-]+:([a-zA-Z0-9._-]+)"')
_TAG_SIZE_RE = re.compile(r"•\s*([\d.]+)\s*(GB|MB)")
_SIMPLE_TAG_RE = re.compile(r"^\d+(\.\d+)?[bm]$")  # e.g. "7b", "0.5b" -- skip quant/base noise
_TAG_WINDOW = 1000


def _parse_ollama_tags_html(html: str) -> list[dict]:
    seen: dict[str, float | None] = {}
    order: list[str] = []
    for m in _TAG_LINK_RE.finditer(html):
        tag = m.group(1)
        if tag not in seen:
            seen[tag] = None
            order.append(tag)
        elif seen[tag] is not None:
            continue
        sm = _TAG_SIZE_RE.search(html[m.end() : m.end() + _TAG_WINDOW])
        if sm:
            size = float(sm.group(1))
            if sm.group(2) == "MB":
                size = round(size / 1024, 2)
            seen[tag] = size
    if not order:
        raise ValueError("no tags parsed from ollama.com/library/<name>/tags response")

    tags = [{"tag": t, "size_gb": seen[t]} for t in order if _SIMPLE_TAG_RE.match(t)]
    if not tags:
        # No "clean" size tag (e.g. name has only a "latest" tag) -- fall
        # back to whatever we did find rather than showing nothing.
        tags = [{"tag": t, "size_gb": seen[t]} for t in order if t == "latest"] or [
            {"tag": order[0], "size_gb": seen[order[0]]}
        ]
    return tags


def _fetch_tags(name: str) -> tuple[list[dict], bool]:
    """Returns (tags, was_live). Falls back to the static catalog's tags for
    a known name, else a single "latest / unknown size" placeholder."""
    try:
        res = httpx.get(f"https://ollama.com/library/{name}/tags", timeout=6)
        res.raise_for_status()
        return _parse_ollama_tags_html(res.text), True
    except Exception:
        fallback = _STATIC_BY_NAME.get(name)
        if fallback:
            return fallback["tags"], False
        return [{"tag": "latest", "size_gb": None}], False


def _cached_tags(name: str) -> tuple[list[dict], bool]:
    now = time.time()
    with _cache_lock:
        cached = _tags_cache.get(name)
        if cached and now - cached[0] < _TAGS_CACHE_TTL_S:
            return cached[1], cached[2]
    tags, live = _fetch_tags(name)
    with _cache_lock:
        _tags_cache[name] = (now, tags, live)
    return tags, live


def _compatibility(size_gb: float | None, ram_gb: float) -> str | None:
    if size_gb is None:
        return None
    if size_gb <= ram_gb * 0.5:
        return "great"
    if size_gb <= ram_gb * 0.75:
        return "tight"
    return "too_big"


def _shape_tags(tags: list[dict], ram_gb: float) -> list[dict]:
    return [
        {
            "tag": t["tag"],
            "size_gb": t["size_gb"],
            "compatibility": _compatibility(t["size_gb"], ram_gb),
        }
        for t in tags
    ]


@router.get("/library")
def ollama_library(q: str = ""):
    ram_gb = psutil.virtual_memory().total / (1024**3)

    if not q.strip():
        # Empty query -> curated popular list, verbatim (spec: not whatever
        # the scrape returns first).
        out = [
            {
                "name": m["name"],
                "description": m["description"],
                "tags": _shape_tags(m["tags"], ram_gb),
            }
            for m in _STATIC_CATALOG
        ]
        return {"models": out, "live": False, "curated": True, "ram_gb": round(ram_gb, 1)}

    cache_key = q.strip().lower()
    now = time.time()
    with _cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL_S:
            _, entries, live = cached
        else:
            entries, live = _fetch_search(q)
            _search_cache[cache_key] = (now, entries, live)

    out = []
    for i, m in enumerate(entries):
        if live:
            if i < _EAGER_ENRICH_COUNT:
                tags, _ = _cached_tags(m["name"])
            else:
                # Lazy: leave tags empty, the UI fetches
                # GET /api/ollama/library/<name>/tags on expand/demand.
                tags = []
        else:
            # Fallback catalog entries already carry their own tags.
            tags = _STATIC_BY_NAME.get(m["name"], {}).get("tags", [])
        out.append(
            {
                "name": m["name"],
                "description": m.get("description", ""),
                "tags": _shape_tags(tags, ram_gb),
            }
        )
    return {"models": out, "live": live, "curated": False, "ram_gb": round(ram_gb, 1)}


@router.get("/library/{name}/tags")
def ollama_library_tags(name: str):
    """Lazy per-model enrichment (v5.1): called by the UI for results past
    the first _EAGER_ENRICH_COUNT, or whenever a result is expanded."""
    ram_gb = psutil.virtual_memory().total / (1024**3)
    tags, live = _cached_tags(name)
    return {"name": name, "tags": _shape_tags(tags, ram_gb), "live": live}


@router.get("/models")
def ollama_models():
    """Installed models (proxy of Ollama's /api/tags), each tagged with this
    machine's RAM compatibility (great/tight/too_big, same _compatibility()
    used by /library and /recommendation) -- Settings > Models surfaces this
    next to the default-model/per-task pickers so an oversized model is
    visibly flagged before the user selects it (rather than only failing
    later at pipeline preflight)."""
    try:
        res = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        res.raise_for_status()
    except Exception as e:
        raise HTTPException(
            503, f"Ollama isn't reachable at {config.OLLAMA_URL} ({e}). Is it running?"
        ) from e

    ram_gb = psutil.virtual_memory().total / (1024**3)
    models = []
    for m in res.json().get("models", []):
        size_bytes = m.get("size") or 0
        size_gb = round(size_bytes / (1024**3), 1)
        models.append(
            {
                "name": m.get("name") or m.get("model") or "",
                "size_gb": size_gb,
                "family": (m.get("details") or {}).get("family", ""),
                "compatibility": _compatibility(size_gb, ram_gb),
            }
        )
    models.sort(key=lambda m: m["name"])
    return models


# --------------------------------------------------------------------------
# v5.1 hardware-aware recommendation
# --------------------------------------------------------------------------

# Curated ranking, ordered by RAM tier from the top down. Structured-output-
# strong models are preferred (they must handle NativeOutput/JSON well --
# the qwen2.5 family does, per the model manager's own use in this app), so
# every tier reaches for a qwen2.5 size before anything else; the bottom
# (8GB) tier drops to llama3.2:3b since even qwen2.5:7b is too tight there.
# "best" = strongest model that still fits comfortably (<= ram*0.5); this
# table just encodes the RAM thresholds directly rather than recomputing
# compatibility live, since the *pairing* (best/optimal are one tier apart)
# is what needs to be curated, not just each one's raw fit.
_RECOMMENDATION_TIERS: list[dict] = [
    {
        "min_ram_gb": 48,
        "best": {
            "model": "qwen2.5:32b",
            "size_gb": 20.0,
            "why": "Strongest general model that still fits comfortably on 48GB+.",
        },
        "optimal": {
            "model": "qwen2.5:14b",
            "size_gb": 9.0,
            "why": "One tier down: noticeably faster, still very capable for structured output.",
        },
    },
    {
        "min_ram_gb": 24,
        "best": {
            "model": "qwen2.5:14b",
            "size_gb": 9.0,
            "why": "Best quality that comfortably fits 24-32GB of RAM.",
        },
        "optimal": {
            "model": "qwen2.5:7b-instruct",
            "size_gb": 4.7,
            "why": "Faster and lighter, still strong at JSON/tool-style output.",
        },
    },
    {
        "min_ram_gb": 16,
        "best": {
            "model": "qwen2.5:7b-instruct",
            "size_gb": 4.7,
            "why": "Solid general quality that fits well within 16GB.",
        },
        "optimal": {
            "model": "llama3.2:3b",
            "size_gb": 2.0,
            "why": "Small and fast for quick iterations on this machine.",
        },
    },
    {
        "min_ram_gb": 0,
        "best": {
            "model": "llama3.2:3b",
            "size_gb": 2.0,
            "why": "Fits safely on lower-RAM Macs without swapping.",
        },
        "optimal": {
            "model": "llama3.2:3b",
            "size_gb": 2.0,
            "why": "Same pick: no comfortably lighter alternative is worth the quality trade here.",
        },
    },
]


# "Thinking"/reasoning-tuned tier table (2026-07-26, workstream D --
# pipeline/ordering.py's full-context clip ordering), mirroring
# _RECOMMENDATION_TIERS' shape and RAM boundaries (48/24/16/0) exactly, but
# curated for reasoning models that benefit narrative-ordering judgements:
# qwen3's thinking variants and deepseek-r1's distills. Same "best"/"optimal"
# one-tier-apart pairing convention as _RECOMMENDATION_TIERS.
_THINKING_MODEL_TIERS: list[dict] = [
    {
        "min_ram_gb": 48,
        "best": {
            "model": "qwen3:32b",
            "size_gb": 20.0,
            "why": "Strongest thinking model that still fits comfortably on 48GB+.",
        },
        "optimal": {
            "model": "qwen3:14b",
            "size_gb": 9.0,
            "why": "One tier down: faster, still a strong reasoner.",
        },
    },
    {
        "min_ram_gb": 24,
        "best": {
            "model": "qwen3:14b",
            "size_gb": 9.0,
            "why": "Best reasoning quality that comfortably fits 24-32GB of RAM.",
        },
        "optimal": {
            "model": "deepseek-r1:14b",
            "size_gb": 9.0,
            "why": "Alternate reasoning-distill pick at the same size.",
        },
    },
    {
        "min_ram_gb": 16,
        "best": {
            "model": "qwen3:8b",
            "size_gb": 5.2,
            "why": "Solid reasoning quality that fits well within 16GB.",
        },
        "optimal": {
            "model": "deepseek-r1:7b",
            "size_gb": 4.7,
            "why": "Lighter reasoning-distill alternative.",
        },
    },
    {
        "min_ram_gb": 0,
        "best": {
            "model": "qwen3:4b",
            "size_gb": 2.6,
            "why": "Small thinking model that fits safely on lower-RAM Macs.",
        },
        "optimal": {
            "model": "llama3.2:3b",
            "size_gb": 2.0,
            "why": "Non-thinking fallback when even qwen3:4b is too tight.",
        },
    },
]


def _pick_thinking_tier(ram_gb: float) -> dict:
    for tier in _THINKING_MODEL_TIERS:
        if ram_gb >= tier["min_ram_gb"]:
            return tier
    return _THINKING_MODEL_TIERS[-1]


def recommended_thinking_model() -> str:
    """Hardware-aware "thinking"/reasoning model recommendation, mirroring
    recommended_default_model() but against _THINKING_MODEL_TIERS. Falls back
    to the bottom tier's pick only if psutil (or anything else here) blows
    up, so this never raises."""
    try:
        ram_gb = psutil.virtual_memory().total / (1024**3)
        tier = _pick_thinking_tier(ram_gb)
        return tier["best"]["model"]
    except Exception:
        return "qwen3:4b"


def model_installed_and_fits(model_name: str) -> bool:
    """Non-raising twin of preflight_check_models() for a SINGLE model:
    True only if Ollama is reachable, `model_name` is actually installed, and
    its real installed size fits this machine's RAM (not "too_big" per
    _compatibility). Reuses the exact same checks as preflight_check_models,
    but returns False instead of raising on ANY failure (unreachable Ollama,
    timeout, not installed, too big, or any unexpected error) -- meant for a
    caller like pipeline/ordering.py's thinking-model degrade ladder that
    wants to proactively probe availability and quietly fall back, never
    hang or crash the calling stage just because a bigger reasoning model
    isn't there."""
    if not model_name:
        return False
    try:
        preflight_check_models([model_name])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Live-verification fix (2026-07-26, gate 3c): recommended_thinking_model()
# only ever proposes THIS machine's own tier's "best" pick (e.g. qwen3:32b on
# a 48GB Mac) and pipeline/ordering.py's old degrade ladder gave up entirely
# the instant that one name wasn't installed -- even when a perfectly good
# thinking model from a LOWER tier (e.g. deepseek-r1:14b, the 24GB tier's
# "optimal" pick) was already installed and would easily fit. Real machine
# this was verified against: 48GB RAM, deepseek-r1:14b installed, qwen3:*
# NOT installed -- the old ladder silently fell back to the plain task model
# + forced digests, never trying deepseek-r1:14b at all.
#
# recommended_installed_thinking_model() fixes this by scanning every tier
# at or BELOW this machine's own tier (top to bottom, "best" then "optimal"
# at each), returning the first candidate that's actually installed and fits
# -- never a tier ABOVE this machine's (that would defeat the RAM-fit
# purpose of having tiers at all). Returns None (never raises, never hangs --
# each candidate check is model_installed_and_fits()'s already-bounded,
# non-raising single /api/tags round trip) if nothing in the whole ladder is
# installed, so the caller can fall back to its own plain task model.
# --------------------------------------------------------------------------


def recommended_installed_thinking_model() -> str | None:
    """Hardware-aware thinking-model pick that's actually INSTALLED: scans
    _THINKING_MODEL_TIERS from this machine's own tier down to the smallest
    (best pick, then optimal pick, at each tier), returning the first name
    that model_installed_and_fits() confirms. None if nothing in the ladder
    is installed/reachable -- never raises."""
    try:
        ram_gb = psutil.virtual_memory().total / (1024**3)
    except Exception:
        return None

    start_idx = len(_THINKING_MODEL_TIERS) - 1
    for i, tier in enumerate(_THINKING_MODEL_TIERS):
        if ram_gb >= tier["min_ram_gb"]:
            start_idx = i
            break

    for tier in _THINKING_MODEL_TIERS[start_idx:]:
        for pick_key in ("best", "optimal"):
            name = tier[pick_key]["model"]
            if model_installed_and_fits(name):
                return name
    return None


def _get_cpu_brand() -> str:
    try:
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        brand = out.stdout.strip()
        return brand or "Unknown chip"
    except Exception:
        return "Unknown chip"


def _pick_tier(ram_gb: float) -> dict:
    for tier in _RECOMMENDATION_TIERS:
        if ram_gb >= tier["min_ram_gb"]:
            return tier
    return _RECOMMENDATION_TIERS[-1]


@router.get("/recommendation")
def ollama_recommendation():
    ram_gb = psutil.virtual_memory().total / (1024**3)
    tier = _pick_tier(ram_gb)
    return {
        "chip": _get_cpu_brand(),
        "ram_gb": round(ram_gb, 1),
        "best_overall": tier["best"],
        "optimal": tier["optimal"],
    }


# --------------------------------------------------------------------------
# Root-cause fix (2026-07-25): a static "qwen2.5:14b" default_model was being
# seeded into settings.json on EVERY first run regardless of hardware -- on
# an 8GB Mac that model either isn't installed (raw ollama error) or, worse,
# gets loaded anyway and swaps the machine to death. `recommended_default_model()`
# is the single place that answers "what model should THIS machine default
# to", reusing the same RAM-tier table as /api/ollama/recommendation above
# (magic_video_editor/settings.py imports this for its first-run seed instead
# of duplicating the tier logic). `preflight_check_models()` is the runtime
# guard: called from the queue chokepoint in api/pipeline.py before any
# LLM-backed stage actually runs, so a bad model choice becomes one clear,
# visible job error instead of a raw ollama error or a silent hang.
# --------------------------------------------------------------------------


def recommended_default_model() -> str:
    """Hardware-aware first-run default (settings.py). Same tier table as
    /api/ollama/recommendation; returns the "best" pick for this machine's
    RAM. Falls back to the historical static "qwen2.5:14b" only if psutil
    (or anything else here) blows up -- see settings.DEFAULT_MODEL."""
    try:
        ram_gb = psutil.virtual_memory().total / (1024**3)
        tier = _pick_tier(ram_gb)
        return tier["best"]["model"]
    except Exception:
        return "qwen2.5:14b"


def preflight_check_models(model_names) -> None:
    """Verify every name in `model_names` is (a) reachable -- ollama is up,
    (b) actually INSTALLED (GET /api/tags, short timeout so a hung ollama
    can't block the caller), and (c) fits this machine's RAM (not
    "too_big" per _compatibility, using the model's REAL installed size).
    Raises RuntimeError with a single clear, user-facing (Spanish) message
    on the first failing model -- meant to be called from inside a queue
    job (see api/pipeline.py's _preflight_stage) so the failure lands as a
    visible job/stage error, never a raw stack trace or a silent hang."""
    names = sorted({m for m in model_names if m})
    if not names:
        return

    ram_gb = psutil.virtual_memory().total / (1024**3)
    try:
        res = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        res.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"No se pudo conectar con Ollama en {config.OLLAMA_URL} ({e}). "
            "Comprueba que esté en marcha e inténtalo de nuevo."
        ) from e

    installed: dict[str, float] = {}
    for m in res.json().get("models", []):
        name = m.get("name") or m.get("model") or ""
        if not name:
            continue
        installed[name] = (m.get("size") or 0) / (1024**3)

    for name in names:
        if name not in installed:
            raise RuntimeError(
                f"El modelo '{name}' no está instalado. Descárgalo en Ajustes → Modelos."
            )
        size_gb = installed[name]
        if _compatibility(size_gb, ram_gb) == "too_big":
            rec = recommended_default_model()
            raise RuntimeError(
                f"El modelo '{name}' necesita ~{size_gb:.0f}GB de RAM pero este Mac "
                f"tiene {ram_gb:.0f}GB. Elige un modelo más pequeño en Ajustes → "
                f"Modelos (recomendado para tu equipo: {rec})."
            )


class PullRequest(BaseModel):
    model: str


def _run_pull(log, model: str) -> None:
    log(f"pulling {model}")
    try:
        with httpx.stream(
            "POST",
            f"{config.OLLAMA_URL}/api/pull",
            json={"name": model, "stream": True},
            timeout=_PULL_TIMEOUT,
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
    """Ollama model pulls aren't project-scoped (magic_video_editor/queue.py's FIFO
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


@router.post("/retry")
def ollama_retry():
    """Finding #16 (c): ensure_ollama_async() only ever runs once per
    process, so a transient startup failure (GitHub rate-limited, a
    stalled self-provisioning download, a momentary network blip) left
    `ollama_mode` stuck at "unreachable" for the rest of the session with
    no way back. This re-arms it -- a UI "Retry" action on an
    unreachable/errored Ollama state should call this, then poll
    GET /api/health's ollama_mode the same way startup does."""
    ollama_manager.retry_ensure_ollama()
    return {"mode": ollama_manager.current_mode()}


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
