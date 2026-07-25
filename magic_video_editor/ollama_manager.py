"""Ollama process lifecycle (v6 packaging Option B).

Prefer an already-running system Ollama reachable at config.OLLAMA_URL --
that's the user's own install and wins whenever it's up. Only when nothing
answers there do we fall back to the Ollama binary we bundle ourselves
(packaging/fetch_ollama.sh vendors it into packaging/vendor/ollama/), spawned
as `ollama serve` with OLLAMA_MODELS pointed under our own app data dir so it
never touches the user's real model library.

The bundled child is registered in ffmpeg_utils' own Popen registry (same
terminate_all() that tears down ffmpeg children) so it shares one lifecycle:
SIGTERM/SIGINT/atexit and the pywebview window-close hook all already call
ffmpeg_utils.terminate_all(), and that now cleans up `ollama serve` too --
no separate shutdown wiring needed.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from . import config, ffmpeg_utils

logger = logging.getLogger(__name__)

_STARTUP_TIMEOUT_S = 30.0

_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_mode = "unreachable"  # "system" | "bundled" | "unreachable"


def _candidate_bundle_roots() -> list[Path]:
    """Where `packaging/vendor/ollama/ollama` might live: a PyInstaller
    onefile temp extraction dir (sys._MEIPASS) if we're ever built that way,
    then the same "sibling of the package dir" trick server.py already uses
    for ui/ (works for both `dev` (repo root) and the onedir .app bundle,
    since mve.spec copies packaging/vendor/ollama into the bundle at the same
    relative path)."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parent.parent)
    return roots


def bundled_binary_path() -> Path | None:
    """Path to our vendored `ollama` executable, if one was fetched
    (packaging/fetch_ollama.sh) and is present in this build. None in dev
    checkouts that never ran the fetch script, or in builds that omitted it."""
    for root in _candidate_bundle_roots():
        candidate = root / "packaging" / "vendor" / "ollama" / "ollama"
        if candidate.exists():
            return candidate
    return None


def _reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{url}/api/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _models_dir() -> Path:
    d = config.DATA_DIR / "ollama-models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _wait_reachable(url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _reachable(url):
            return True
        time.sleep(0.5)
    return False


def ensure_ollama() -> str:
    """Call once at server startup (idempotent -- safe to call again, e.g.
    from a health check, it just confirms/re-derives the current mode).
    Returns the resulting mode: "system" (an external Ollama answered at
    config.OLLAMA_URL -- always preferred), "bundled" (we spawned our own
    vendored binary), or "unreachable" (neither worked)."""
    global _proc, _mode
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            return _mode

        if _reachable(config.OLLAMA_URL):
            _mode = "system"
            return _mode

        binary = bundled_binary_path()
        if binary is None:
            logger.warning(
                "No system Ollama reachable at %s and no bundled ollama binary found "
                "(packaging/vendor/ollama/ollama) -- LLM features will be unavailable.",
                config.OLLAMA_URL,
            )
            _mode = "unreachable"
            return _mode

        try:
            os.chmod(binary, 0o755)
        except Exception:
            pass

        env = dict(os.environ)
        env["OLLAMA_MODELS"] = str(_models_dir())
        # Bind the bundled server to the same host:port config.OLLAMA_URL
        # already points at, so every other module (llm.py, agents/agents.py)
        # keeps talking to the same configured URL unchanged.
        env["OLLAMA_HOST"] = config.OLLAMA_URL.split("://", 1)[-1]

        logger.info("Spawning bundled ollama serve from %s", binary)
        try:
            proc = ffmpeg_utils._spawn(
                [str(binary), "serve"],
                cwd=str(binary.parent),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            logger.exception("Failed to spawn bundled ollama serve")
            _mode = "unreachable"
            return _mode

        if _wait_reachable(config.OLLAMA_URL, _STARTUP_TIMEOUT_S):
            _proc = proc
            _mode = "bundled"
        else:
            logger.warning(
                "Bundled ollama serve did not answer /api/version within %ss; giving up.",
                _STARTUP_TIMEOUT_S,
            )
            ffmpeg_utils._unregister(proc)
            try:
                proc.terminate()
            except Exception:
                pass
            _proc = None
            _mode = "unreachable"
        return _mode


def current_mode() -> str:
    """Last mode determined by ensure_ollama(): "system"|"bundled"|
    "unreachable". Exposed on the health endpoint (server.py)."""
    return _mode


def terminate() -> None:
    """Stop the bundled `ollama serve` child if we started one. Idempotent --
    safe to call when nothing was spawned (system mode, or ensure_ollama()
    never ran). ffmpeg_utils.terminate_all() already tears this process down
    as part of the shared registry; this is for callers that want to stop
    only the bundled Ollama (e.g. tests) without touching ffmpeg children."""
    global _proc, _mode
    with _proc_lock:
        proc, _proc = _proc, None
    if proc is None:
        return
    try:
        ffmpeg_utils._unregister(proc)
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _mode = "unreachable"
