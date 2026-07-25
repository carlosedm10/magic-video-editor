"""Ollama process lifecycle (v6 packaging Option B, + field-bug follow-up).

Prefer an already-running system Ollama reachable at config.OLLAMA_URL --
that's the user's own install and wins whenever it's up. Only when nothing
answers there do we fall back to the Ollama binary we bundle ourselves
(packaging/fetch_ollama.sh vendors it into packaging/vendor/ollama/), spawned
as `ollama serve` with OLLAMA_MODELS pointed under our own app data dir so it
never touches the user's real model library. If no bundled binary shipped in
this build (or it fails to spawn/answer), we self-provision: download the
same official darwin-arm64 standalone runtime straight from GitHub Releases
into <data_dir>/bin/ollama and spawn that instead.

FIELD BUG (clean M2/8GB install, released dmg, Ollama.app installed but not
running): the packaged .app never called ensure_ollama() at all. The
reachability/spawn/download logic below was always sound -- the actual bug
was in the two process entry points (magic_video_editor/app.py, the real
`mve` entry PyInstaller builds, vs magic_video_editor/server.py's `mve-server`
dev entry): only server.py's main() called ensure_ollama(); app.py drives
uvicorn itself and skipped it entirely, so the packaged app silently ran in
whatever mode config.OLLAMA_URL happened to already be in -- "unreachable" on
a machine where Ollama.app was installed but not started. Fixed by adding an
ensure_ollama_async() call to app.py's main() too (see that file), on a
background thread so a slow probe / 30s bundled-serve wait / multi-hundred-MB
download never delays the native window opening.

The bundled/downloaded child is registered in ffmpeg_utils' own Popen
registry (same terminate_all() that tears down ffmpeg children) so it shares
one lifecycle: SIGTERM/SIGINT/atexit and the pywebview window-close hook all
already call ffmpeg_utils.terminate_all(), and that now cleans up
`ollama serve` too -- no separate shutdown wiring needed.
"""

import hashlib
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import config, ffmpeg_utils
from . import jobs as jobs_module

logger = logging.getLogger(__name__)

_STARTUP_TIMEOUT_S = 30.0
_OLLAMA_GITHUB_REPO = "ollama/ollama"
_DOWNLOAD_ASSET_NAME = "ollama-darwin.tgz"
_CHECKSUMS_ASSET_NAME = "sha256sum.txt"

_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
# "system" | "bundled" | "downloaded" | "starting" | "downloading" | "unreachable"
_mode = "unreachable"

_ensure_lock = threading.Lock()
_ensure_started = False
_download_job_id: str | None = None


# --------------------------------------------------------------------------
# where the binary lives (bundled at build time, or self-provisioned at run
# time)
# --------------------------------------------------------------------------


def _candidate_bundle_roots() -> list[Path]:
    """Where `packaging/vendor/ollama/ollama` might live. Tries several
    roots and de-dupes, in preference order:

    1. sys._MEIPASS -- PyInstaller's own "where bundled datas/binaries live"
       pointer, set for both onefile and onedir builds.
    2. Computed straight from sys.executable's real on-disk location rather
       than trusting _MEIPASS alone: for a macOS onedir .app bundle this is
       Contents/MacOS/<exe>, and empirically (verified against an actual
       `PyInstaller.building.build_main` COLLECT+BUNDLE run of this exact
       datas layout) `datas` end up copied into Contents/Frameworks (with
       Contents/Resources also getting a copy of some, so both are checked)
       -- NOT under Contents/MacOS/_internal as some PyInstaller versions/
       docs suggest. Kept as a second, independent path so a future
       PyInstaller version changing where _MEIPASS points doesn't silently
       break this lookup again the way the app.py/ensure_ollama() wiring gap
       did.
    3. The "sibling of the package dir" trick server.py already uses for
       ui/ -- correct for a bare dev checkout (repo root).
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        for parent in (exe, *exe.parents):
            if parent.suffix == ".app":
                roots.append(parent / "Contents" / "Frameworks")
                roots.append(parent / "Contents" / "Resources")
                break
    roots.append(Path(__file__).resolve().parent.parent)

    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def bundled_binary_path() -> Path | None:
    """Path to our vendored `ollama` executable, if one was fetched
    (packaging/fetch_ollama.sh) and is present in this build. None in dev
    checkouts that never ran the fetch script, or in builds that omitted it
    -- ensure_ollama() self-provisions (see _download_ollama_binary) when
    this returns None."""
    for root in _candidate_bundle_roots():
        candidate = root / "packaging" / "vendor" / "ollama" / "ollama"
        if candidate.exists():
            return candidate
    return None


def _downloaded_binary_path() -> Path:
    """Where a self-provisioned (runtime-downloaded) binary lives -- always
    under the app's own data dir, never inside the (read-only, potentially
    unwritable once installed) .app bundle itself."""
    return config.DATA_DIR / "bin" / "ollama"


def _models_dir() -> Path:
    d = config.DATA_DIR / "ollama-models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _serve_log_path() -> Path:
    d = config.DATA_DIR / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "ollama-serve.log"


def _tail(path: Path, n_bytes: int = 4000) -> str:
    try:
        data = path.read_bytes()
        return data[-n_bytes:].decode("utf-8", "replace").strip() or "(empty log)"
    except Exception as e:
        return f"(no log captured: {e})"


# --------------------------------------------------------------------------
# reachability
# --------------------------------------------------------------------------


def _reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{url}/api/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _wait_reachable(url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _reachable(url):
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------
# spawning
# --------------------------------------------------------------------------


def _spawn_binary(binary: Path) -> subprocess.Popen | None:
    """Spawn `<binary> serve` bound to config.OLLAMA_URL, retrying the
    reachability check for up to _STARTUP_TIMEOUT_S. Returns the tracked
    Popen on success, None (having already cleaned up) on failure. All
    stdout/stderr goes to a log file under the data dir so a failure can
    report a real stderr tail instead of just "didn't come up"."""
    try:
        os.chmod(binary, 0o755)
    except Exception:
        pass

    env = dict(os.environ)
    env["OLLAMA_MODELS"] = str(_models_dir())
    # Bind the bundled/downloaded server to the same host:port
    # config.OLLAMA_URL already points at, so every other module (llm.py,
    # agents/agents.py) keeps talking to the same configured URL unchanged.
    env["OLLAMA_HOST"] = config.OLLAMA_URL.split("://", 1)[-1]

    log_path = _serve_log_path()
    logger.info("ollama: spawning `%s serve` (log: %s)", binary, log_path)
    t0 = time.monotonic()

    log_f = None
    try:
        log_f = open(log_path, "wb")
    except Exception:
        logger.warning("ollama: couldn't open %s for logging, using DEVNULL", log_path)

    try:
        proc = ffmpeg_utils._spawn(
            [str(binary), "serve"],
            cwd=str(binary.parent),
            env=env,
            stdout=log_f or subprocess.DEVNULL,
            stderr=log_f or subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("ollama: failed to spawn `%s serve`", binary)
        return None
    finally:
        if log_f is not None:
            log_f.close()  # the child already holds its own dup'd fd

    if _wait_reachable(config.OLLAMA_URL, _STARTUP_TIMEOUT_S):
        logger.info(
            "ollama: ready in %.1fs (%s, pid %s)", time.monotonic() - t0, binary, proc.pid
        )
        return proc

    logger.warning(
        "ollama: `%s serve` did not answer /api/version within %ss; giving up. stderr tail:\n%s",
        binary,
        _STARTUP_TIMEOUT_S,
        _tail(log_path),
    )
    ffmpeg_utils._unregister(proc)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------
# self-provisioning download (field-bug follow-up: the owner asked that a
# MISSING bundled binary -- or one that fails to spawn -- fall back to
# fetching the real thing at runtime, same asset packaging/fetch_ollama.sh
# uses at build time, rather than just giving up)
# --------------------------------------------------------------------------


def _safe_extract(tf: tarfile.TarFile, dest_dir: Path) -> None:
    try:
        tf.extractall(dest_dir, filter="data")  # PEP 706, py>=3.12 (backported 3.11.4+)
    except TypeError:
        tf.extractall(dest_dir)  # older interpreter without the `filter` kwarg


def _download_ollama_binary(log) -> None:
    """jobs.py job body: fetch the latest official ollama-darwin.tgz release
    asset (same one packaging/fetch_ollama.sh vendors at build time),
    checksum-verify it against the release's sha256sum.txt, and extract it
    into _downloaded_binary_path().parent. Progress/log lines are visible
    like any other background job (GET /api/jobs/<id>)."""
    log("looking up latest ollama release")
    resp = httpx.get(
        f"https://api.github.com/repos/{_OLLAMA_GITHUB_REPO}/releases/latest", timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name", "unknown")
    assets = data.get("assets") or []
    asset_url = next(
        (a["browser_download_url"] for a in assets if a.get("name") == _DOWNLOAD_ASSET_NAME), None
    )
    checksums_url = next(
        (a["browser_download_url"] for a in assets if a.get("name") == _CHECKSUMS_ASSET_NAME), None
    )
    if not asset_url:
        raise RuntimeError(f"no {_DOWNLOAD_ASSET_NAME} asset found in release {tag}")

    log(f"downloading ollama {tag}")
    dest_dir = _downloaded_binary_path().parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mve-ollama-dl-") as tmp:
        tgz_path = Path(tmp) / _DOWNLOAD_ASSET_NAME
        with httpx.Client() as client:
            with client.stream("GET", asset_url, timeout=None, follow_redirects=True) as res:
                res.raise_for_status()
                total = int(res.headers.get("content-length") or 0)
                done = 0
                with open(tgz_path, "wb") as f:
                    for chunk in res.iter_bytes(1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            log.progress(done / total)

        if checksums_url:
            log("verifying checksum")
            sums_res = httpx.get(checksums_url, timeout=15)
            sums_res.raise_for_status()
            expected = None
            for line in sums_res.text.splitlines():
                if _DOWNLOAD_ASSET_NAME in line:
                    expected = line.split()[0].strip().lower()
                    break
            if expected:
                actual = hashlib.sha256(tgz_path.read_bytes()).hexdigest().lower()
                if actual != expected:
                    raise RuntimeError(f"checksum mismatch: expected {expected} got {actual}")
                log("checksum OK")
            else:
                logger.warning(
                    "ollama: no sha256 entry for %s in checksums file -- skipping verification",
                    _DOWNLOAD_ASSET_NAME,
                )
        else:
            logger.warning(
                "ollama: release %s has no %s asset -- skipping checksum verification",
                tag,
                _CHECKSUMS_ASSET_NAME,
            )

        log("extracting")
        with tarfile.open(tgz_path, "r:gz") as tf:
            _safe_extract(tf, dest_dir)

    binary = _downloaded_binary_path()
    if not binary.exists():
        raise RuntimeError(f"extracted archive did not contain {binary.name} at {binary}")
    os.chmod(binary, 0o755)
    log(f"done -- ollama {tag} ready at {binary}")


def _set_download_job_id(job_id: str) -> None:
    global _download_job_id
    _download_job_id = job_id


def download_job_id() -> str | None:
    """jobs.py job id of the last (or in-flight) self-provisioning download,
    if one ever ran. None if we've never needed to download. Exposed so the
    UI could poll GET /api/jobs/<id> for live progress if it wants to."""
    return _download_job_id


def _download_and_spawn() -> subprocess.Popen | None:
    """Runs the download as a tracked jobs.py job (so progress/log lines are
    visible the same way a model pull's are), then spawns the result.
    Returns the tracked Popen on success, None on any failure (download or
    spawn) -- the caller falls back to "unreachable"."""
    logger.info(
        "ollama: no usable bundled binary -- self-provisioning by downloading "
        "the official darwin-arm64 release"
    )
    job = jobs_module.run_sync(
        "ollama_provision",
        _download_ollama_binary,
        lock_key="ollama_provision",
        on_start=_set_download_job_id,
    )
    if job["status"] != "done":
        logger.warning("ollama: self-provisioning download failed: %s", job.get("error"))
        return None

    binary = _downloaded_binary_path()
    if not binary.exists():
        logger.warning("ollama: download job finished but binary missing at %s", binary)
        return None

    logger.info("ollama: downloaded binary ready at %s -- spawning", binary)
    return _spawn_binary(binary)


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------


def ensure_ollama() -> str:
    """Call once at server startup (idempotent -- safe to call again, e.g.
    from a health check, it just confirms/re-derives the current mode).
    Runs synchronously on the calling thread -- prefer ensure_ollama_async()
    from a real startup path so a slow probe/spawn/download never blocks the
    UI. Returns the resulting mode: "system" (an external Ollama answered at
    config.OLLAMA_URL -- always preferred), "bundled" (spawned our vendored
    binary), "downloaded" (no vendored binary/spawn worked, so we fetched
    the official release at runtime and spawned that), or "unreachable"
    (nothing worked)."""
    global _proc, _mode
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            return _mode

        logger.info("ollama: checking reachability at %s", config.OLLAMA_URL)
        if _reachable(config.OLLAMA_URL):
            logger.info("ollama: system install reachable at %s -- using it", config.OLLAMA_URL)
            _mode = "system"
            return _mode
        logger.info("ollama: unreachable at %s", config.OLLAMA_URL)

        binary = bundled_binary_path()
        if binary is not None:
            logger.info("ollama: bundled binary found at %s", binary)
            proc = _spawn_binary(binary)
            if proc is not None:
                _proc = proc
                _mode = "bundled"
                return _mode
            logger.warning("ollama: bundled binary present but failed to come up")
        else:
            logger.warning(
                "ollama: no bundled binary found (checked: %s)",
                ", ".join(
                    str(r / "packaging" / "vendor" / "ollama" / "ollama")
                    for r in _candidate_bundle_roots()
                ),
            )

        _mode = "downloading"
        proc = _download_and_spawn()
        if proc is not None:
            _proc = proc
            _mode = "downloaded"
            return _mode

        logger.warning(
            "ollama: neither system, bundled, nor a freshly-downloaded Ollama came up -- "
            "LLM features will be unavailable."
        )
        _mode = "unreachable"
        return _mode


def ensure_ollama_async() -> None:
    """Non-blocking startup hook: runs ensure_ollama() on a background
    daemon thread so a slow reachability probe, the up-to-30s bundled-serve
    wait, or a multi-hundred-MB self-provisioning download never delays the
    native window opening or uvicorn accepting requests (field bug: the
    packaged .app used to call ensure_ollama() nowhere at all -- see
    app.py/server.py's main()). current_mode() reports "starting" the
    instant this returns, then "downloading" if we end up self-provisioning,
    then settles on system/bundled/downloaded/unreachable -- poll GET
    /api/health to show progress. Idempotent: a second call while the first
    is still in flight (or already settled) is a no-op."""
    global _ensure_started, _mode
    with _ensure_lock:
        if _ensure_started:
            return
        _ensure_started = True
    _mode = "starting"
    threading.Thread(target=ensure_ollama, name="ollama-ensure", daemon=True).start()


def current_mode() -> str:
    """Last mode determined/being-determined by ensure_ollama(): "system"|
    "bundled"|"downloaded"|"starting"|"downloading"|"unreachable". Exposed
    on the health endpoint (server.py) so the UI can show live progress."""
    return _mode


def terminate() -> None:
    """Stop the bundled/downloaded `ollama serve` child if we started one.
    Idempotent -- safe to call when nothing was spawned (system mode, or
    ensure_ollama() never ran). ffmpeg_utils.terminate_all() already tears
    this process down as part of the shared registry; this is for callers
    that want to stop only Ollama (e.g. tests) without touching ffmpeg
    children. Also resets the "already started" latch so ensure_ollama_async()
    can be called again cleanly (test convenience)."""
    global _proc, _mode, _ensure_started
    with _proc_lock:
        proc, _proc = _proc, None
    if proc is not None:
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
    with _ensure_lock:
        _ensure_started = False
