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
kept as a fallback in case a given install rejects array input at runtime.

Field bug (2026-07-25): a fully-Spanish video suddenly produced fluent
ENGLISH subtitles that mean the same thing as the Spanish speech -- not
garbage. That's whisper's per-clip language auto-detection misfiring on one
clip's first window and locking to "en"; large multilingual whisper models
respond to an out-of-language guess by *translating* rather than
transcribing, so the output is a faithful English rendering of Spanish
audio. Fix: settings.transcription_language / project["language_override"]
let a language be pinned explicitly (skips auto-detect entirely), and even
in "auto" mode a lone clip whose detected language disagrees with the
majority of the project's other clips gets one automatic re-transcription
pinned to the majority language (see _majority_language_retry)."""

import logging
from collections import Counter

import numpy as np
import soundfile as sf

from .. import config, settings, store
from . import speakers

logger = logging.getLogger(__name__)


def _load_wav_array_or_path(wav_path: str) -> np.ndarray | str:
    """`_load_wav_array(wav_path)`, falling back to the raw path string on
    any failure so the whisper backend decodes it itself. That fallback was
    previously silent (bare `except Exception: audio = wav_path`), so a
    transcription-quality regression caused by array decoding failing
    open into this fallback -- and thus back onto ffmpeg-via-mlx-whisper --
    had no signal in the logs. Log once here so it's visible in the field."""
    try:
        return _load_wav_array(wav_path)
    except Exception as exc:
        logger.warning(
            "%s: ndarray audio decode failed, falling back to path-based decode: %s",
            wav_path,
            exc,
        )
        return wav_path


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


def _transcribe_mlx(wav_path: str, language: str | None = None) -> dict:
    import mlx_whisper

    audio = _load_wav_array_or_path(wav_path)

    try:
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=config.WHISPER_MODEL,
            word_timestamps=True,
            language=language,
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
            language=language,
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


def _transcribe_faster(wav_path: str, language: str | None = None) -> dict:
    from faster_whisper import WhisperModel

    model = WhisperModel("large-v3", device="auto", compute_type="int8")

    audio = _load_wav_array_or_path(wav_path)

    try:
        segs, info = model.transcribe(audio, word_timestamps=True, language=language)
        segs = list(segs)  # materialize before the array can go out of scope
    except Exception:
        if audio is wav_path:
            raise
        # array input rejected at runtime by this install -- fall back to
        # the path-based call.
        segs, info = model.transcribe(wav_path, word_timestamps=True, language=language)
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


#: ISO codes offered by Settings/project language pickers, see api/settings.py
#: and api/projects.py (kept in sync there; duplicated here as a plain tuple
#: to avoid a pipeline -> api import).
LANGUAGE_CODES = ("auto", "es", "en", "fr", "de", "it", "pt", "ca")


def _resolve_language(project: dict) -> str:
    """The language to pin transcription to for `project`: the project-level
    override if set to a real code, else the settings-level default, else
    "auto" (per-clip auto-detection, whisper's original behavior)."""
    override = project.get("language_override") or "auto"
    if override != "auto":
        return override
    return settings.load().get("transcription_language") or "auto"


def _majority_language_retry(log, project: dict, clips: list, backend) -> None:
    """Self-heal a lone misdetection in "auto" mode (the field bug this
    module's docstring describes): once every clip has an auto-detected
    language, find the majority language across the project's clips: any
    clip whose detected language disagrees gets ONE retry, pinned to the
    majority language, logged loudly both before and after. Fail-open per
    clip -- if a retry raises, log it and keep the original transcript
    rather than losing it."""
    langs = [c.get("language") for c in clips if c.get("language")]
    if len(langs) < 2:
        return
    counts = Counter(langs)
    majority_lang, majority_n = counts.most_common(1)[0]
    if majority_n == len(langs):
        return  # unanimous across all clips -- nothing to heal

    for clip in clips:
        lang = clip.get("language")
        if not lang or lang == majority_lang:
            continue
        log(
            f"LANGUAGE MISMATCH: {clip['filename']} auto-detected as '{lang}' but "
            f"{majority_n}/{len(langs)} of this project's clips are '{majority_lang}' "
            f"-- retrying transcription pinned to '{majority_lang}'"
        )
        try:
            result = backend(clip["wav"], language=majority_lang)
        except Exception as e:
            log(f"{clip['filename']}: majority-language retry failed ({e}), keeping original")
            continue
        clip["transcript"] = {"segments": result["segments"]}
        clip["language"] = majority_lang
        n_words = sum(len(s["words"]) for s in result["segments"])
        log(
            f"{clip['filename']}: re-transcribed pinned to '{majority_lang}' "
            f"({len(result['segments'])} segments, {n_words} words)"
        )
        store.save(project)


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

    pinned = _resolve_language(project)
    lang_arg = None if pinned == "auto" else pinned
    if lang_arg:
        log(f"Language pinned to '{pinned}' (project/settings override) -- auto-detect skipped")

    for i, clip in enumerate(clips):
        # A pinned language invalidates a transcript recorded under a
        # different (or auto-detected) language -- re-transcribe instead of
        # skipping, so flipping the override cleanly heals old projects
        # (previously ANY existing transcript was skipped unconditionally).
        stale_for_pin = lang_arg is not None and clip.get("language") != lang_arg
        if clip.get("transcript") and not stale_for_pin:
            log(f"{clip['filename']}: already transcribed, skipping")
        else:
            if stale_for_pin:
                log(
                    f"{clip['filename']}: re-transcribing -- language pinned to '{lang_arg}' "
                    f"(stored transcript was '{clip.get('language')}')"
                )
            else:
                log(f"Transcribing {clip['filename']} ({clip['info']['duration']:.0f}s)...")
            result = backend(clip["wav"], language=lang_arg)
            clip["transcript"] = {"segments": result["segments"]}
            clip["language"] = result["language"]
            n_words = sum(len(s["words"]) for s in result["segments"])
            log(
                f"{clip['filename']}: {len(result['segments'])} segments, "
                f"{n_words} words, lang={result['language']}"
            )
            store.save(project)
        log.progress((i + 1) / len(clips))

    if pinned == "auto":
        _majority_language_retry(log, project, clips, backend)

    # v5.8c speaker diarization: only when the project declares >1 speaker
    # (or "auto") -- speakers.run itself is the single source of truth for
    # the speaker_count=1/None skip, see its docstring.
    if project.get("speaker_count", 1) not in (1, "1", None):
        speakers.run(log, project)
