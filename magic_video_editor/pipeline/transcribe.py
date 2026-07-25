"""Stage 3 — Transcribe: whisper with word-level timestamps.
Prefers mlx-whisper (Apple Silicon GPU); falls back to faster-whisper if present.

Belt-and-suspenders for the M2 "ffmpeg missing" field bug: both whisper
backends' `transcribe()` accept either a path (str) or the raw waveform
(np.ndarray). Passing the path lets mlx-whisper's internal
audio.load_audio() shell out to a bare `ffmpeg` from PATH on its own --
bypassing our ffmpeg_bin() resolution and ffmpeg_utils.export_binaries_to_path()
shim entirely. Our analysis wavs (see ffmpeg_utils.extract_wav) are already
mono 16-bit PCM at config.ANALYSIS_SR (16kHz), i.e. exactly the format both
backends want, so we load them ourselves via soundfile and pass the ARRAY --
no ffmpeg subprocess of any kind on the hot path. The path-based call is
kept as a fallback in case a given install rejects array input at runtime."""

import numpy as np
import soundfile as sf

from .. import config, store
from . import speakers


def _load_wav_array(wav_path: str) -> np.ndarray:
    """Mono float32 ndarray at config.ANALYSIS_SR (16kHz) for whisper.
    ffmpeg_utils.extract_wav() already produces mono/16kHz/pcm_s16le, so no
    resampling or channel mixing is needed here -- just decode via
    soundfile (libsndfile), no ffmpeg subprocess involved."""
    data, sr = sf.read(wav_path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.float32)
    if sr != config.ANALYSIS_SR:
        raise RuntimeError(
            f"{wav_path}: expected {config.ANALYSIS_SR}Hz analysis wav, got {sr}Hz"
        )
    return np.ascontiguousarray(data, dtype=np.float32)


def _transcribe_mlx(wav_path: str) -> dict:
    import mlx_whisper

    try:
        audio = _load_wav_array(wav_path)
    except Exception:
        audio = wav_path  # fallback: let mlx-whisper load it itself

    try:
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=config.WHISPER_MODEL,
            word_timestamps=True,
        )
    except Exception:
        if audio is wav_path:
            raise
        # array input rejected at runtime by this install -- fall back to
        # the path-based call (mlx-whisper decodes it itself via ffmpeg).
        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=config.WHISPER_MODEL,
            word_timestamps=True,
        )
    segments = [
        {
            "start": round(s["start"], 3),
            "end": round(s["end"], 3),
            "text": s["text"].strip(),
            "words": [
                {
                    "w": w["word"].strip(),
                    "s": round(w["start"], 3),
                    "e": round(w["end"], 3),
                }
                for w in s.get("words", [])
            ],
        }
        for s in result["segments"]
    ]
    return {"language": result.get("language"), "segments": segments}


def _transcribe_faster(wav_path: str) -> dict:
    from faster_whisper import WhisperModel

    model = WhisperModel("large-v3", device="auto", compute_type="int8")

    try:
        audio = _load_wav_array(wav_path)
    except Exception:
        audio = wav_path

    try:
        segs, info = model.transcribe(audio, word_timestamps=True)
        segs = list(segs)  # materialize before the array can go out of scope
    except Exception:
        if audio is wav_path:
            raise
        # array input rejected at runtime by this install -- fall back to
        # the path-based call.
        segs, info = model.transcribe(wav_path, word_timestamps=True)
        segs = list(segs)
    segments = [
        {
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "text": s.text.strip(),
            "words": [
                {"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
                for w in (s.words or [])
            ],
        }
        for s in segs
    ]
    return {"language": info.language, "segments": segments}


def run(log, project: dict) -> None:
    clips = [c for c in project["clips"] if c.get("wav")]
    if not clips:
        raise RuntimeError("Run Ingest first — no analysis audio found.")

    try:
        import mlx_whisper  # noqa: F401

        backend = _transcribe_mlx
        log(f"Using mlx-whisper ({config.WHISPER_MODEL})")
    except ImportError:
        backend = _transcribe_faster
        log("mlx-whisper unavailable, using faster-whisper large-v3")

    for i, clip in enumerate(clips):
        if clip.get("transcript"):
            log(f"{clip['filename']}: already transcribed, skipping")
        else:
            log(f"Transcribing {clip['filename']} ({clip['info']['duration']:.0f}s)...")
            result = backend(clip["wav"])
            clip["transcript"] = {"segments": result["segments"]}
            clip["language"] = result["language"]
            n_words = sum(len(s["words"]) for s in result["segments"])
            log(
                f"{clip['filename']}: {len(result['segments'])} segments, "
                f"{n_words} words, lang={result['language']}"
            )
            store.save(project)
        log.progress((i + 1) / len(clips))

    # v5.8c speaker diarization: only when the project declares >1 speaker
    # (or "auto") -- speakers.run itself is the single source of truth for
    # the speaker_count=1/None skip, see its docstring.
    if project.get("speaker_count", 1) not in (1, "1", None):
        speakers.run(log, project)
