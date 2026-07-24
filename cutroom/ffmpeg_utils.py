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
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import psutil

from . import config, settings


class FFmpegError(RuntimeError):
    pass


# ---------- child-process registry + shutdown ----------

_procs: set[subprocess.Popen] = set()
_procs_lock = threading.Lock()


def _spawn(cmd: list[str], **kwargs) -> subprocess.Popen:
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(cmd, **kwargs)
    with _procs_lock:
        _procs.add(proc)
    return proc


def _unregister(proc: subprocess.Popen) -> None:
    with _procs_lock:
        _procs.discard(proc)


def terminate_all() -> None:
    """SIGTERM every tracked ffmpeg child, wait up to 5s, then SIGKILL any
    still alive. Called on server shutdown (atexit/SIGTERM/SIGINT) and on
    job cancel."""
    with _procs_lock:
        procs = list(_procs)
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
                self._cond.wait(timeout=1)
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


def _wait_for_ram() -> None:
    """Block (log-waiting in 5s increments) while available RAM is below the
    configured guard, up to 10 minutes total, then proceed anyway --
    graceful degradation over refusing to run."""
    min_gb = settings.load().get("performance", {}).get("min_free_ram_gb", 4)
    threshold = min_gb * 2**30
    waited = 0.0
    max_wait = 600.0
    while True:
        try:
            available = psutil.virtual_memory().available
        except Exception:
            return
        if available >= threshold or waited >= max_wait:
            return
        print(
            f"[ffmpeg_utils] low memory ({available / 2**30:.1f}GB free < "
            f"{min_gb}GB): waiting 5s before encoding..."
        )
        time.sleep(5)
        waited += 5


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
def ffmpeg_bin() -> str:
    """Prefer an ffmpeg that can burn subtitles (libass). Homebrew's ffmpeg 8
    formula ships without libass, so fall back to the static build bundled
    with imageio-ffmpeg when the system one can't."""
    candidates = [os.environ.get("CUTROOM_FFMPEG"), "ffmpeg"]
    try:
        import imageio_ffmpeg

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    candidates = [c for c in candidates if c]
    for cand in candidates:
        if _supports_ass(cand):
            return cand
    return candidates[0]


def supports_subtitles() -> bool:
    return _supports_ass(ffmpeg_bin())


def run(cmd: list[str], heavy: bool = False) -> None:
    """Run an ffmpeg command via a tracked Popen. `heavy=True` (real
    video encodes) gates on the RAM guard + the concurrency gate first."""
    if heavy:
        _wait_for_ram()
        _gate.acquire()
    try:
        proc = _spawn(cmd, text=True)
        try:
            _, stderr = proc.communicate()
        finally:
            _unregister(proc)
        if proc.returncode != 0:
            raise FFmpegError(f"{' '.join(cmd[:6])}... failed:\n{(stderr or '')[-2000:]}")
    finally:
        if heavy:
            _gate.release()


def probe(path: str) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
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
    )
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
    try:
        stdout, _ = proc.communicate()
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
