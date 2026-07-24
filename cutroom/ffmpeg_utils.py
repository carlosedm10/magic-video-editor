"""Thin ffmpeg/ffprobe wrappers. All pixel/sample work in the app goes through here."""

import functools
import json
import os
import subprocess
from pathlib import Path

import numpy as np

from . import config


class FFmpegError(RuntimeError):
    pass


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


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"{' '.join(cmd[:6])}... failed:\n{proc.stderr[-2000:]}")


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
    proc = subprocess.run(
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
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"decode failed for {path}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


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
        dst,
    ]
    run(cmd)


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
