"""Stage 3 — Transcribe: whisper with word-level timestamps.
Prefers mlx-whisper (Apple Silicon GPU); falls back to faster-whisper if present."""

from .. import config, store


def _transcribe_mlx(wav_path: str) -> dict:
    import mlx_whisper
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
                {"w": w["word"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3)}
                for w in s.get("words", [])
            ],
        }
        for s in result["segments"]
    ]
    return {"language": result.get("language"), "segments": segments}


def _transcribe_faster(wav_path: str) -> dict:
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="auto", compute_type="int8")
    segs, info = model.transcribe(wav_path, word_timestamps=True)
    segments = [
        {
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "text": s.text.strip(),
            "words": [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
                      for w in (s.words or [])],
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
            log(f"{clip['filename']}: {len(result['segments'])} segments, "
                f"{n_words} words, lang={result['language']}")
            store.save(project)
        log.progress((i + 1) / len(clips))
