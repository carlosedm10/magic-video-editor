"""GitHub Releases auto-update (spec v6 "Auto-update via GitHub Releases").

Non-blocking check-for-update at startup (``start_check_async()``, spawned
from ``server.py:main()``): GETs the repo's latest release from the GitHub
API, compares its tag's semver against ``magic_video_editor.__version__``,
and stores the result for ``GET /api/update`` (``magic_video_editor/api/updater.py``)
to serve. The whole check is fail-silent -- a network hiccup or GitHub being
down must never affect app startup or show an error to the user, it just
means no update is reported this session.

"Install" (``start_install_job()``) downloads the release's ``.dmg`` +
``.dmg.sha256`` assets (same sidecar convention as ``packaging/make_dmg.sh``),
verifies the hash, and -- ONLY when this process is actually running from a
packaged ``.app`` bundle -- hands off to ``packaging/update_helper.sh``, which
waits for this process to exit, ditto-copies the new ``.app`` over the
current bundle, and relaunches it. In a dev checkout (``uv run mve`` /
``uv run mve-server``) there is no ``.app`` to swap, so install refuses
up front with a clear ``DevModeError`` ("git pull instead") rather than
downloading anything.

Sparkle (signed, delta updates) is the future path once code signing exists
-- see README's "Releases & auto-update" section. This is the unsigned
bridge that gets us a working update loop today.
"""

import hashlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx

from . import __version__
from . import jobs as jobs_module

logger = logging.getLogger(__name__)

REPO = "carlosedm10/magic-video-editor"
RELEASES_LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
_CHECK_TIMEOUT_S = 5.0

_INSTALL_LOCK_KEY = "update_install"


class DevModeError(RuntimeError):
    """Raised by start_install_job() when this process isn't running from a
    packaged .app bundle -- there is nothing to swap in place. The API layer
    (magic_video_editor/api/updater.py) turns this into a clear 400 response
    instead of a generic job failure."""


# --------------------------------------------------------------------------
# semver compare
# --------------------------------------------------------------------------


def parse_semver(v: str) -> tuple[int, int, int]:
    """"v1.2.3" / "1.2.3" / "1.2.3-rc1" -> (1, 2, 3). Raises ValueError on
    anything that doesn't start with at least a numeric major version --
    callers treat that as "can't compare, skip"."""
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    core = v.split("-", 1)[0].split("+", 1)[0]  # drop -rc1 / +build metadata
    parts = core.split(".")
    if len(parts) < 1 or not parts[0]:
        raise ValueError(f"not a semver: {v!r}")
    parts = parts + ["0"] * (3 - len(parts)) if len(parts) < 3 else parts[:3]
    return tuple(int(p) for p in parts)


def semver_gt(a: str, b: str) -> bool:
    """True if version string `a` is strictly greater than `b`. Unparsable
    input on either side is treated as "not greater" -- fail-closed, so a
    weird/malformed release tag never falsely offers an update."""
    try:
        return parse_semver(a) > parse_semver(b)
    except (ValueError, IndexError):
        return False


# --------------------------------------------------------------------------
# check-for-update
# --------------------------------------------------------------------------

_lock = threading.Lock()
_status: dict = {
    "checked": False,
    "available": False,
    "current_version": __version__,
    "latest_version": None,
    "release_url": None,
    "dmg_url": None,
    "sha256_url": None,
    "error": None,
}


def _find_asset_url(assets: list[dict], suffix: str) -> str | None:
    for a in assets:
        if a.get("name", "").endswith(suffix):
            return a.get("browser_download_url")
    return None


def _check_now() -> dict:
    res = httpx.get(
        RELEASES_LATEST_API,
        headers={"Accept": "application/vnd.github+json"},
        timeout=_CHECK_TIMEOUT_S,
        follow_redirects=True,
    )
    res.raise_for_status()
    data = res.json()
    tag = data.get("tag_name") or ""
    assets = data.get("assets") or []
    latest = tag[1:] if tag[:1] in ("v", "V") else tag
    return {
        "checked": True,
        "available": semver_gt(tag, __version__),
        "current_version": __version__,
        "latest_version": latest or None,
        "release_url": data.get("html_url"),
        "dmg_url": _find_asset_url(assets, ".dmg"),
        "sha256_url": _find_asset_url(assets, ".dmg.sha256"),
        "error": None,
    }


def check_for_update() -> dict:
    """Synchronous check, fail-silent: any error (offline, rate-limited, no
    releases yet) is logged at info level and recorded on the status instead
    of raised. Safe to call repeatedly (e.g. a manual "Check for Updates…").
    """
    global _status
    try:
        result = _check_now()
    except Exception as e:
        logger.info("update check failed (non-fatal): %s", e)
        with _lock:
            result = dict(_status)
        result["checked"] = True
        result["error"] = str(e)
    with _lock:
        _status = result
    return result


def start_check_async() -> None:
    """Fire-and-forget background thread -- call once from server startup so
    the check never delays boot."""
    threading.Thread(target=check_for_update, daemon=True, name="update-check").start()


def get_status() -> dict:
    with _lock:
        return dict(_status)


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------


def running_from_app_bundle() -> Path | None:
    """The ".../Magic Video Editor.app" bundle root if this process is
    actually a packaged (PyInstaller-frozen) build running from inside one,
    else None for a dev checkout (`uv run mve` / `uv run mve-server`)."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    for parent in (exe, *exe.parents):
        if parent.suffix == ".app":
            return parent
    return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(file_path: Path, sha256_sidecar_text: str) -> bool:
    """True if `file_path` hashes to the digest found in
    `sha256_sidecar_text` (the raw contents of a `.sha256` sidecar file,
    `shasum -a 256` format: "<hex>  <filename>", possibly with just the hex
    alone). Pure/testable -- no I/O beyond the file itself."""
    expected = sha256_sidecar_text.strip().split()[0].lower()
    return _sha256_of(file_path).lower() == expected


def _download(
    client: httpx.Client, url: str, dest: Path, log, frac_lo: float, frac_hi: float
) -> None:
    with client.stream("GET", url, timeout=None, follow_redirects=True) as res:
        res.raise_for_status()
        total = int(res.headers.get("content-length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in res.iter_bytes(1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    log.progress(frac_lo + (frac_hi - frac_lo) * (done / total))


def _helper_script_path() -> Path:
    """packaging/update_helper.sh, resolved the same "sibling of the package
    dir" way ollama_manager.py finds its vendored binary (works for both a
    PyInstaller onedir .app bundle and a bare dev checkout)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "packaging" / "update_helper.sh")
    candidates.append(Path(__file__).resolve().parent.parent / "packaging" / "update_helper.sh")
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def _run_install(log, dmg_url: str, sha256_url: str, bundle: Path) -> None:
    log("downloading update")
    tmp_dir = Path(tempfile.mkdtemp(prefix="mve-update-"))
    dmg_path = tmp_dir / "Magic Video Editor.dmg"
    sha_path = tmp_dir / "Magic Video Editor.dmg.sha256"
    with httpx.Client() as client:
        _download(client, sha256_url, sha_path, log, 0.0, 0.02)
        _download(client, dmg_url, dmg_path, log, 0.02, 0.85)

    log("verifying checksum")
    if not verify_sha256(dmg_path, sha_path.read_text()):
        raise RuntimeError("downloaded update failed sha256 verification -- aborting install")
    log.progress(0.9)

    helper = _helper_script_path()
    if not helper.exists():
        raise RuntimeError(f"update helper script missing: {helper}")

    log("handing off to the update helper and quitting")
    # start_new_session=True detaches the helper from this process's
    # session so it survives our own exit (it's the whole point: it waits
    # for our pid to die, then swaps the bundle). Mirrors the "app-first"
    # relaunch flow -- `open -n` inside the helper opens a fresh instance.
    subprocess.Popen(
        ["/bin/bash", str(helper), str(dmg_path), str(bundle), str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.progress(1.0)
    log("quitting now -- the app will relaunch once the update is installed")
    # Give the job's final log lines / HTTP response a brief moment to flush
    # before pulling the rug out from under the whole process.
    threading.Timer(0.5, lambda: os._exit(0)).start()


def start_install_job() -> str:
    """Kicks off the download+verify+swap+relaunch as a background job
    (magic_video_editor/jobs.py) and returns its id for the UI to poll.

    Raises DevModeError immediately (before any download) when this process
    isn't a packaged .app, and RuntimeError immediately when no update/asset
    is currently known -- both are checked synchronously so the caller gets
    a clear response right away, not a job that fails asynchronously."""
    bundle = running_from_app_bundle()
    if bundle is None:
        raise DevModeError(
            "dev mode: this is a source checkout, not a packaged app -- "
            "run `git pull` (and rebuild) instead of using auto-update."
        )
    status = get_status()
    if not status.get("available") or not status.get("dmg_url") or not status.get("sha256_url"):
        raise RuntimeError("no update available to install -- run a check first")
    return jobs_module.start(
        "update_install",
        _run_install,
        status["dmg_url"],
        status["sha256_url"],
        bundle,
        lock_key=_INSTALL_LOCK_KEY,
    )
