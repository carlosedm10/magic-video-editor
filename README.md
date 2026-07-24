# magic-video-editor (CutRoom)

Local-first AI video editor for macOS. You drop in raw footage; it transcribes,
finds the best takes, orders the story, renders a main cut, and suggests ~20
Reels/TikTok-ready vertical clips — **everything runs on your machine**:

- **Whisper** (mlx-whisper, Apple Silicon GPU) — transcription with word timestamps
- **Ollama** (any local model, default `qwen2.5:7b-instruct`) — all editorial *decisions*
- **ffmpeg** — all cutting/cropping/rendering. No editing app involved.
- **scipy** — multi-cam / external-audio sync via audio cross-correlation
- **OpenCV** — face detection for speaker-centered 9:16 crops

The LLM only ever emits JSON decisions (which take, what order, which clip) —
it never touches pixels. Every decision is inspectable and overridable in the UI.

## Pipeline

1. **Ingest** — probe clips, extract analysis audio. Originals are never modified.
2. **Sync** — cross-correlate audio to detect simultaneous recordings
   (multi-cam groups + external audio recorder alignment). Pure DSP.
3. **Transcribe** — whisper large-v3-turbo, word-level timestamps.
4. **Takes** — sentence split, repeated-take clustering (rapidfuzz), scoring
   (disfluencies, restarts, completeness, pace, loudness stability) + local-LLM
   tiebreak. Best take kept, rest cut — all toggleable in the UI.
5. **Order** — LLM reads per-clip transcripts and orders clips so the narrative
   flows (clips don't need to be recorded in order). Drag to override.
6. **Render** — EDL → frame-accurate per-segment re-encode → lossless concat.
   Synced external audio replaces camera scratch audio automatically.
7. **Reels** — candidate windows (15–60s of continuous kept speech) scored by
   the LLM on hook/self-containment/payoff; top 20 suggested. Rendering a reel
   crops to 1080×1920 centered on the detected face and burns word-timed subtitles.

## Requirements

- macOS (Apple Silicon recommended), ffmpeg (`brew install ffmpeg`)
- [Ollama](https://ollama.com) running with a model pulled
  (`ollama pull qwen2.5:7b-instruct` — or `qwen2.5:14b` if you have ≥32GB RAM)
- [uv](https://docs.astral.sh/uv/)

## Run

```bash
uv sync
uv run cutroom          # native app window (pywebview)
uv run cutroom-server   # or: plain backend, open http://127.0.0.1:8765
```

First transcription downloads the whisper model (~1.6 GB) from Hugging Face.

Projects and renders live in `~/CutRoom/projects/`.

## Configuration (env vars)

| Var | Default | |
|---|---|---|
| `CUTROOM_LLM` | `qwen2.5:7b-instruct` | any Ollama model |
| `CUTROOM_WHISPER` | `mlx-community/whisper-large-v3-turbo` | any mlx whisper repo |
| `CUTROOM_DATA` | `~/CutRoom` | projects dir |
| `OLLAMA_URL` | `http://localhost:11434` | |

## Honest v1 limitations

- Take scoring is heuristic (transcript + loudness); DNSMOS/NISQA neural speech
  quality scoring is a planned upgrade.
- Narrative ordering is clip-level, not sentence-level (robust > clever with 7B models).
- Multi-cam groups keep the main camera for the whole cut (no automatic angle
  switching yet).
- Face crop is fixed per reel (median face position), not tracking.
