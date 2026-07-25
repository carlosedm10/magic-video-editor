"""Thin ffmpeg/ffprobe wrappers. All pixel/sample work in the app goes through here.

Resource safety (spec: "Resource safety"): every ffmpeg child is spawned via a
tracked Popen (registry + terminate_all()), heavy encodes go through a
lazily-resized concurrency gate (settings.performance.max_parallel_ffmpeg) and
a per-process -threads cap (settings.performance.ffmpeg_threads), and a RAM
guard delays encodes while available memory is below
settings.performance.min_free_ram_gb."""

import functools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import psutil

from . import config, settings


class FFmpegError(RuntimeError):
    pass


class FFmpegCancelled(FFmpegError):
    """Raised by _wait_for_ram()/_EncodeGate.acquire() when the CURRENT
    job's cancel-check (see begin_job/_raise_if_cancelled) reports it has
    been cancelled, instead of completing the full RAM-guard/concurrency-
    gate wait (finding 7). Subclasses FFmpegError so existing
    `except FFmpegError` call sites keep working unchanged; jobs.py's
    _execute() additionally attributes ANY exception raised while
    job["cancel_requested"] is set to a "cancelled" job status regardless
    of type, so this doesn't need special-casing there either."""


# ---------- child-process registry + shutdown ----------

_procs: set[subprocess.Popen] = set()
# job_id -> the tracked Popens spawned while that job was "current" on
# their own thread (see begin_job/_spawn). Lets jobs.cancel(job_id)
# terminate only THAT job's children (finding 12+17) instead of every
# ffmpeg child process-wide -- which used to also kill unrelated
# concurrent jobs AND the untracked live color-preview-frame path
# (api/filters.py's preview_frame runs on a plain request thread, never
# inside a job, so it never gets a job_id here and can never be hit by
# terminate_job()).
_procs_by_job: dict[str, set[subprocess.Popen]] = {}
_procs_lock = threading.Lock()

# Per-THREAD "what job (if any) is currently executing on this thread"
# context, set by jobs.py's _execute() around running a job's fn (its own
# thread for start(), the caller's thread -- the sole queue worker -- for
# run_sync()) and cleared afterwards. A thread-local rather than a
# parameter threaded through run()/probe()/etc.: those are called from
# pipeline modules several layers away (and several packwerk-style
# ownership boundaries away, in this repo's terms) that shouldn't need to
# change just to opt a job into cancellation/per-job process tracking.
_job_context = threading.local()


def begin_job(job_id: str, cancel_check: Callable[[], bool]) -> None:
    """Mark `job_id` as the current job on THIS thread: _spawn() will file
    every Popen it creates on this thread under this job_id (see
    _procs_by_job), and _wait_for_ram()/_EncodeGate.acquire() will poll
    `cancel_check()` and raise FFmpegCancelled promptly if it returns True.
    Must be paired with end_job(job_id) in a finally block."""
    _job_context.job_id = job_id
    _job_context.cancel_check = cancel_check


def end_job(job_id: str) -> None:
    """Clear this thread's job context and drop `job_id`'s entry from the
    per-job process registry (its children, if any, have already been
    individually _unregister()'d as they finished/were killed -- this just
    stops the registry from accumulating one empty set per job ever run)."""
    _job_context.job_id = None
    _job_context.cancel_check = None
    with _procs_lock:
        _procs_by_job.pop(job_id, None)


def _raise_if_cancelled() -> None:
    check = getattr(_job_context, "cancel_check", None)
    if check is not None and check():
        raise FFmpegCancelled("cancelled")


def _spawn(cmd: list[str], **kwargs) -> subprocess.Popen:
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(cmd, **kwargs)
    job_id = getattr(_job_context, "job_id", None)
    with _procs_lock:
        _procs.add(proc)
        if job_id is not None:
            _procs_by_job.setdefault(job_id, set()).add(proc)
    return proc


def _unregister(proc: subprocess.Popen) -> None:
    with _procs_lock:
        _procs.discard(proc)
        for bucket in _procs_by_job.values():
            bucket.discard(proc)


def _terminate(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + 5
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.time()))
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def terminate_job(job_id: str) -> None:
    """SIGTERM (then SIGKILL after a 5s grace period) only the ffmpeg
    children tracked under `job_id` -- see begin_job/_spawn. Used by
    jobs.cancel(job_id) so cancelling one job can no longer kill a
    different, concurrently-running job's encode, nor the untracked live
    color-preview-frame path (finding 12+17). A no-op if `job_id` has no
    (or no longer any) tracked children."""
    with _procs_lock:
        procs = list(_procs_by_job.get(job_id, ()))
    _terminate(procs)


def terminate_all() -> None:
    """SIGTERM every tracked ffmpeg child process-wide, wait up to 5s, then
    SIGKILL any still alive. Real app shutdown ONLY (atexit/SIGTERM/
    SIGINT) -- job cancellation goes through terminate_job() instead so
    cancelling one job doesn't collaterally kill unrelated concurrent work
    (finding 12+17)."""
    with _procs_lock:
        procs = list(_procs)
    _terminate(procs)


# ---------- concurrency gate + RAM guard ----------


class _EncodeGate:
    """Caps concurrent heavy ffmpeg encodes. Sized from
    settings.performance.max_parallel_ffmpeg, re-read on every acquire so a
    settings change applies without a restart (a plain threading.Semaphore
    can't be resized once constructed)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active = 0

    def _limit(self) -> int:
        try:
            n = int(settings.load().get("performance", {}).get("max_parallel_ffmpeg", 2))
        except Exception:
            return 2
        return max(1, n)

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._limit():
                _raise_if_cancelled()
                self._cond.wait(timeout=1)
            _raise_if_cancelled()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()


_gate = _EncodeGate()


def _ffmpeg_threads() -> int:
    perf = settings.load().get("performance", {})
    n = perf.get("ffmpeg_threads")
    if n:
        try:
            return max(1, int(n))
        except Exception:
            pass
    return max(2, (os.cpu_count() or 4) // 2)


def ffmpeg_threads() -> int:
    """Public accessor for the configured -threads cap (settings.performance.
    ffmpeg_threads), for callers outside this module building their own
    ffmpeg command (e.g. render.py's fade/crossfade re-encodes)."""
    return _ffmpeg_threads()


# ---------- subprocess timeouts (finding 11) ----------
#
# Every ffmpeg/ffprobe subprocess call in this module is now bounded: one
# stalled or corrupt media file used to hang its `communicate()` forever,
# and since there is exactly one global queue worker thread, that wedged
# every project's queue, not just the one that hit the bad file. A timeout
# kills that one child and fails just that stage with a clear error
# instead. Three tiers (probe/light/heavy), each overridable via
# settings.performance the same way as max_parallel_ffmpeg/ffmpeg_threads/
# min_free_ram_gb -- re-read on every call, no restart needed.

_DEFAULT_PROBE_TIMEOUT_S = 30.0
_DEFAULT_LIGHT_TIMEOUT_S = 300.0  # non-"heavy" run() calls: wav extract, mux, concat
_DEFAULT_HEAVY_TIMEOUT_S = 1800.0  # heavy encodes: cut_segment, make_proxy


def _timeout_s(key: str, default: float) -> float:
    try:
        return float(settings.load().get("performance", {}).get(key, default))
    except Exception:
        return default


def _probe_timeout_s() -> float:
    return _timeout_s("ffprobe_timeout_s", _DEFAULT_PROBE_TIMEOUT_S)


def _light_timeout_s() -> float:
    return _timeout_s("ffmpeg_light_timeout_s", _DEFAULT_LIGHT_TIMEOUT_S)


def _encode_timeout_s() -> float:
    return _timeout_s("ffmpeg_timeout_s", _DEFAULT_HEAVY_TIMEOUT_S)


def _kill_after_timeout(proc: subprocess.Popen) -> None:
    """Best-effort: the child ignored/couldn't act on communicate()'s
    implicit SIGTERM-less timeout (communicate() itself does NOT kill the
    process on TimeoutExpired -- that's on us), so kill it directly and
    reap it with a short grace period."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def _wait_for_ram() -> None:
    """Block while available RAM is below the configured guard, up to 10
    minutes total, then proceed anyway -- graceful degradation over
    refusing to run.

    Cooperative (finding 7): polls the current job's cancel-check (see
    begin_job(), set by jobs.py's _execute()) every ~1s and raises
    FFmpegCancelled promptly instead of riding out the full wait -- before
    this, Cancel was a no-op for up to 10 minutes while the sole queue
    worker sat here, and the whole queue stalled behind it. Logs a
    "waiting for RAM" line every ~5s (not every poll, to avoid log spam)
    and a clear "timed out, proceeding anyway" line if the full max_wait
    elapses without the threshold being met."""
    min_gb = settings.load().get("performance", {}).get("min_free_ram_gb", 4)
    threshold = min_gb * 2**30
    waited = 0.0
    max_wait = 600.0
    last_log = -5.0
    while True:
        _raise_if_cancelled()
        try:
            available = psutil.virtual_memory().available
        except Exception:
            return
        if available >= threshold:
            return
        if waited >= max_wait:
            print(
                f"[ffmpeg_utils] low memory wait timed out after {max_wait:.0f}s "
                f"({available / 2**30:.1f}GB free < {min_gb}GB): proceeding anyway"
            )
            return
        if waited - last_log >= 5.0:
            print(
                f"[ffmpeg_utils] low memory ({available / 2**30:.1f}GB free < "
                f"{min_gb}GB): waiting for RAM..."
            )
            last_log = waited
        step = min(1.0, max_wait - waited)
        time.sleep(step)
        waited += step


@functools.cache
def _supports_ass(binary: str) -> bool:
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        return " ass " in out
    except Exception:
        return False


@functools.cache
def _bin_works(binary: str) -> bool:
    """True if `binary -version` actually runs. Used to skip a candidate
    that isn't installed/on PATH rather than blow up ffprobe_bin()."""
    try:
        return (
            subprocess.run([binary, "-version"], capture_output=True, timeout=10).returncode == 0
        )
    except Exception:
        return False


def _vendor_ffbin_roots() -> list[Path]:
    """Where the fixed-path bundled ffmpeg/ffprobe (packaging/mve.spec's
    explicit `binaries` entries at "vendor/ffbin/{ffmpeg,ffprobe}") might
    live inside a packaged .app. Computed from sys.executable the same way
    ollama_manager.py's _candidate_bundle_roots() locates its own vendored
    binary -- for a onedir macOS build, PyInstaller's `binaries` land under
    Contents/Frameworks (Contents/Resources is checked too, since some
    PyInstaller versions/datas mixes copy there instead)."""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "vendor" / "ffbin")
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        for parent in (exe, *exe.parents):
            if parent.suffix == ".app":
                roots.append(parent / "Contents" / "Frameworks" / "vendor" / "ffbin")
                roots.append(parent / "Contents" / "Resources" / "vendor" / "ffbin")
                break
    return roots


def _vendor_bin(name: str) -> str | None:
    """Path to `name` ("ffmpeg" or "ffprobe") under the fixed in-bundle
    vendor dir (see packaging/mve.spec), if this build actually vendored it
    and the file is present. This is the DETERMINISTIC bundle-mode
    candidate -- unlike collect_all()'s sweep of static_ffmpeg's package
    data (which, in the field, brought in ffmpeg but silently not ffprobe),
    this path is an explicit PyInstaller `binaries` entry, so its presence
    is a build-time guarantee, not a static-analysis best-effort."""
    for root in _vendor_ffbin_roots():
        candidate = root / name
        if candidate.exists():
            return str(candidate)
    return None


def _static_ffmpeg_exes() -> tuple[str | None, str | None]:
    """Lazily fetch (ffmpeg, ffprobe) from the `static-ffmpeg` package. On a
    machine that has never run this before, this downloads the platform's
    static build once (cached under the package's own install dir after
    that) -- see the report for how `make dist-app` should pre-warm this so
    the packaged .app never needs network access at runtime. Returns
    (None, None) if the package/build is unavailable."""
    try:
        import static_ffmpeg.run as static_ffmpeg_run

        ffmpeg_path, ffprobe_path = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
        return ffmpeg_path, ffprobe_path
    except Exception:
        return None, None


@functools.cache
def ffmpeg_bin() -> str:
    """Prefer an ffmpeg that can burn subtitles (libass). Homebrew's ffmpeg 8
    formula ships without libass, so fall back to the static build bundled
    with imageio-ffmpeg, and finally to the `static-ffmpeg` package's build
    (both bundled statics were verified to ship libass) when the system one
    can't. A clean machine with neither Homebrew ffmpeg nor a system ffprobe
    still resolves via static-ffmpeg -- what makes the packaged .app
    self-contained (field bug: packaged app shelling out to system
    ffmpeg/ffprobe that isn't there)."""
    candidates = [config.env_with_legacy_fallback("MVE_FFMPEG", "CUTROOM_FFMPEG"), "ffmpeg"]
    try:
        import imageio_ffmpeg

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    # Deterministic bundle path (packaging/mve.spec explicit `binaries`)
    # before the lazy static-ffmpeg fetch, which in a packaged app may have
    # no network access and nothing pre-fetched at runtime.
    vendor_ffmpeg = _vendor_bin("ffmpeg")
    if vendor_ffmpeg:
        candidates.append(vendor_ffmpeg)
    static_ffmpeg_exe, _ = _static_ffmpeg_exes()
    if static_ffmpeg_exe:
        candidates.append(static_ffmpeg_exe)
    candidates = [c for c in candidates if c]
    if not candidates:
        raise FFmpegError(
            "no ffmpeg binary found (system, imageio-ffmpeg, static-ffmpeg all unavailable)"
        )
    chosen = next((cand for cand in candidates if _supports_ass(cand)), candidates[0])
    print(f"[ffmpeg_utils] using ffmpeg binary: {chosen} (libass={_supports_ass(chosen)})")
    return chosen


@functools.cache
def ffprobe_bin() -> str:
    """Resolve ffprobe: env override -> system ffprobe -> the `static-ffmpeg`
    package's bundled ffprobe. imageio-ffmpeg ships ffmpeg only (no ffprobe),
    so it is NOT a candidate here -- static-ffmpeg is the only fallback that
    makes probe() work with no system ffmpeg/ffprobe install at all (the
    packaged-app field bug this function exists to fix)."""
    candidates = [
        config.env_with_legacy_fallback("MVE_FFPROBE", "CUTROOM_FFPROBE"),
        "ffprobe",
    ]
    # Deterministic bundle path (packaging/mve.spec explicit `binaries`) --
    # this is the fix for the field bug where the packaged .app reported
    # ffprobe missing (while ffmpeg worked) because collect_all()'s sweep of
    # static_ffmpeg's package data silently didn't carry ffprobe along.
    vendor_ffprobe = _vendor_bin("ffprobe")
    if vendor_ffprobe:
        candidates.append(vendor_ffprobe)
    _, static_ffprobe_exe = _static_ffmpeg_exes()
    if static_ffprobe_exe:
        candidates.append(static_ffprobe_exe)
    candidates = [c for c in candidates if c]
    if not candidates:
        raise FFmpegError(
            "no ffprobe binary found (system ffprobe and static-ffmpeg both unavailable)"
        )
    chosen = next((cand for cand in candidates if _bin_works(cand)), candidates[0])
    print(f"[ffmpeg_utils] using ffprobe binary: {chosen}")
    return chosen


def supports_subtitles() -> bool:
    return _supports_ass(ffmpeg_bin())


def binaries_status() -> dict:
    """Summary for the health endpoint (field bug: health/doctor checked
    `shutil.which("ffmpeg")`, which is always False for the packaged .app on
    a machine with no Homebrew ffmpeg even though the app bundles its own
    static ffmpeg+ffprobe and works fine). Resolves the same lazy/cached
    binaries the rest of this module actually runs, so "healthy" means "the
    binary this app will really use", not "something named ffmpeg happens to
    be on PATH". See report for the 3-line server.py patch that should call
    this instead of shutil.which("ffmpeg")."""
    try:
        ffmpeg_path = ffmpeg_bin()
    except Exception:
        ffmpeg_path = None
    try:
        ffprobe_path = ffprobe_bin()
    except Exception:
        ffprobe_path = None
    return {
        "ffmpeg": ffmpeg_path is not None,
        "ffprobe": ffprobe_path is not None,
        "ffmpeg_path": ffmpeg_path,
        "ffprobe_path": ffprobe_path,
        "ffmpeg_libass": _supports_ass(ffmpeg_path) if ffmpeg_path else False,
    }


_exported_shim_dir: str | None = None
_export_lock = threading.Lock()


def _resolves_on_path(name: str, resolved: str) -> bool:
    """True if plain `name` (as a bare command, via PATH lookup) already
    resolves to the same file as `resolved` (our ffmpeg_bin()/ffprobe_bin()
    pick). If so, third-party code shelling out to the bare command name
    gets the right binary for free and no shim is needed."""
    which = shutil.which(name)
    if not which:
        return False
    try:
        return Path(which).resolve() == Path(resolved).resolve()
    except OSError:
        return False


def export_binaries_to_path() -> str | None:
    """Make plain `ffmpeg`/`ffprobe` (bare command name, PATH lookup) resolve
    to the SAME binaries this module already picked via ffmpeg_bin()/
    ffprobe_bin(), for third-party code that shells out to the bare command
    name directly instead of calling into this module.

    Field bug (M2, packaged app, no system ffmpeg): mlx-whisper's internal
    audio.load_audio() runs `subprocess.run(["ffmpeg", ...])` straight from
    PATH -- our own ffmpeg_bin() resolution (vendor/static-ffmpeg fallback
    chain) never applied to it, so transcribe failed with "ffmpeg missing"
    even though ingest/probe (which go through this module) worked fine.

    If bare `ffmpeg`/`ffprobe` already resolve on PATH to the exact binaries
    this module would pick, this is a no-op (no shim needed). Otherwise it
    creates -- once, idempotently, safe to call from multiple entrypoints or
    repeatedly across runs -- a shim dir under DATA_DIR/bin-shims/ holding
    symlinks named exactly `ffmpeg` and `ffprobe` pointing at the resolved
    binaries, and prepends that dir to os.environ["PATH"] for this process.

    Cached after the first call in this process (module-level guard, not
    functools.cache, so tests can reset it via
    ffmpeg_utils._exported_shim_dir = None). Returns the shim dir path if
    PATH now includes it (freshly created or already prepended earlier in
    this process), else None if no shim was necessary.
    """
    global _exported_shim_dir
    with _export_lock:
        if _exported_shim_dir is not None:
            return _exported_shim_dir or None

        try:
            resolved_ffmpeg = ffmpeg_bin()
        except FFmpegError:
            resolved_ffmpeg = None
        try:
            resolved_ffprobe = ffprobe_bin()
        except FFmpegError:
            resolved_ffprobe = None

        needs: dict[str, str] = {}
        if resolved_ffmpeg and not _resolves_on_path("ffmpeg", resolved_ffmpeg):
            needs["ffmpeg"] = resolved_ffmpeg
        if resolved_ffprobe and not _resolves_on_path("ffprobe", resolved_ffprobe):
            needs["ffprobe"] = resolved_ffprobe

        if not needs:
            _exported_shim_dir = ""
            return None

        shim_dir = config.DATA_DIR / "bin-shims"
        shim_dir.mkdir(parents=True, exist_ok=True)
        for name, target in needs.items():
            link = shim_dir / name
            target_path = Path(target).resolve()
            try:
                if link.is_symlink() or link.exists():
                    if link.is_symlink() and Path(os.readlink(link)).resolve() == target_path:
                        continue
                    link.unlink()
                link.symlink_to(target_path)
            except OSError as e:
                print(f"[ffmpeg_utils] failed to create PATH shim {link} -> {target_path}: {e}")

        shim_dir_str = str(shim_dir)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if shim_dir_str not in path_entries:
            os.environ["PATH"] = os.pathsep.join([shim_dir_str, *path_entries])

        print(f"[ffmpeg_utils] exported ffmpeg/ffprobe PATH shims: {shim_dir_str}")
        _exported_shim_dir = shim_dir_str
        return shim_dir_str


def run(cmd: list[str], heavy: bool = False) -> None:
    """Run an ffmpeg command via a tracked Popen. `heavy=True` (real
    video encodes) gates on the RAM guard + the concurrency gate first, and
    is allowed a longer bounded timeout than lighter operations (wav
    extract/mux/concat) before its child is killed and the stage fails
    (finding 11) -- see _encode_timeout_s()/_light_timeout_s()."""
    if heavy:
        _wait_for_ram()
        _gate.acquire()
    timeout = _encode_timeout_s() if heavy else _light_timeout_s()
    try:
        proc = _spawn(cmd, text=True)
        try:
            try:
                _, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_after_timeout(proc)
                raise FFmpegError(
                    f"{' '.join(cmd[:6])}... timed out after {timeout:.0f}s (stage timed out)"
                ) from None
        finally:
            _unregister(proc)
        if proc.returncode != 0:
            raise FFmpegError(f"{' '.join(cmd[:6])}... failed:\n{(stderr or '')[-2000:]}")
    finally:
        if heavy:
            _gate.release()


def probe(path: str) -> dict:
    timeout = _probe_timeout_s()
    try:
        proc = subprocess.run(
            [
                ffprobe_bin(),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise FFmpegError(f"ffprobe timed out after {timeout:.0f}s for {path}") from None
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}: {proc.stderr[-500:]}")
    return json.loads(proc.stdout)


def clip_info(path: str) -> dict:
    data = probe(path)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    fps = 0.0
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        num, den = v["avg_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    return {
        "duration": float(data["format"].get("duration", 0.0)),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": round(fps, 3),
        "has_video": v is not None,
        "has_audio": a is not None,
        "size_bytes": int(data["format"].get("size", 0)),
        "codec_name": v.get("codec_name") if v else None,
        "pix_fmt": v.get("pix_fmt") if v else None,
    }


def extract_wav(src: str, dst: str, sr: int = config.ANALYSIS_SR) -> None:
    """Mono 16-bit wav for analysis (whisper, sync, loudness)."""
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            src,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sr),
            "-c:a",
            "pcm_s16le",
            dst,
        ]
    )


def load_wav_mono(path: str) -> np.ndarray:
    """Read a pcm_s16le wav as float32 in [-1, 1] (skips the header via ffmpeg pipe)."""
    proc = _spawn(
        [
            ffmpeg_bin(),
            "-v",
            "error",
            "-i",
            path,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(config.ANALYSIS_SR),
            "-",
        ]
    )
    timeout = _light_timeout_s()
    try:
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_after_timeout(proc)
            raise FFmpegError(f"ffmpeg decode timed out after {timeout:.0f}s for {path}") from None
    finally:
        _unregister(proc)
    if proc.returncode != 0:
        raise FFmpegError(f"decode failed for {path}")
    return np.frombuffer(stdout, dtype=np.int16).astype(np.float32) / 32768.0


def extract_frame(src: str, t: float, dst: str) -> None:
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            src,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            dst,
        ]
    )


def cut_segment(
    src: str,
    start: float,
    end: float,
    dst: str,
    width: int,
    height: int,
    fps: float,
    audio_src: str | None = None,
    audio_start: float | None = None,
    vf_extra: str = "",
    ass_path: str | None = None,
) -> None:
    """Frame-accurate cut with re-encode, normalized to a common format so
    segments can be concat-copied afterwards. Optionally swaps in audio from an
    external (already offset-corrected) source, and burns .ass subtitles
    (styles live inside the .ass file, so the filter arg needs no quoting)."""
    dur = max(0.05, end - start)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}"
    )
    if vf_extra:
        vf = f"{vf_extra},{vf}"
    if ass_path:
        vf += f",ass={ass_path}"

    cmd = [ffmpeg_bin(), "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if audio_src is not None:
        cmd += [
            "-ss",
            f"{audio_start:.3f}",
            "-t",
            f"{dur:.3f}",
            "-i",
            audio_src,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    cmd += [
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-crf",
        str(config.RENDER_CRF),
        "-preset",
        config.RENDER_PRESET,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-video_track_timescale",
        "90000",
        "-threads",
        str(_ffmpeg_threads()),
        dst,
    ]
    run(cmd, heavy=True)


def make_proxy(src: str, dst: str, fps: float) -> None:
    """H.264/yuv420p preview proxy for browser playback (Chrome can't decode
    HEVC 10-bit iPhone footage, so the <video> preview player needs a
    guaranteed-decodable stand-in; see docs/PLATFORM-SPEC.md streaming
    section). 720p-tall, fps capped at 30, faststart for immediate seeking."""
    capped_fps = min(fps, 30) if fps else 30
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            src,
            "-vf",
            f"scale=-2:720,fps={capped_fps}",
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-threads",
            str(_ffmpeg_threads()),
            dst,
        ],
        heavy=True,
    )


def mux_audio(video_path: str, audio_path: str, dst_path: str) -> None:
    """Remux video_path's video stream (copied, no re-encode) with
    audio_path's audio (re-encoded to AAC). Used by the voice-enhancement
    hook: extract -> enhance -> mux back onto the rendered file."""
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            dst_path,
        ]
    )


def concat_segments(segment_paths: list[str], dst: str, workdir: Path) -> None:
    """Concat identically-encoded segments without re-encoding."""
    lst = workdir / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in segment_paths))
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c",
            "copy",
            dst,
        ]
    )
