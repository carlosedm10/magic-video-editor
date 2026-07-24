"""Global configuration. Everything local, everything overridable via env."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CUTROOM_DATA", Path.home() / "CutRoom")).expanduser()
PROJECTS_DIR = DATA_DIR / "projects"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("CUTROOM_LLM", "qwen2.5:7b-instruct")

# mlx-community whisper repo (Apple Silicon). Downloaded on first use by mlx-whisper.
WHISPER_MODEL = os.environ.get("CUTROOM_WHISPER", "mlx-community/whisper-large-v3-turbo")

HOST = os.environ.get("CUTROOM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTROOM_PORT", "8765"))

# Analysis audio format
ANALYSIS_SR = 16000

# Sync detection
SYNC_ENV_RATE = 50           # Hz of the loudness envelope used for coarse alignment
SYNC_MIN_CORR = 0.55         # normalized correlation to consider two clips simultaneous

# Take/dedup detection
DUP_SIMILARITY = 82          # rapidfuzz token_sort_ratio threshold (0-100)
DUP_MIN_WORDS = 4

# Rendering
RENDER_CRF = 18
RENDER_PRESET = "veryfast"
SEGMENT_PAD = 0.12           # seconds of padding around kept sentences
MERGE_GAP = 1.2              # merge adjacent kept sentences closer than this (same clip)

# Reels
REEL_MIN_S = 15.0
REEL_MAX_S = 60.0
REEL_SUGGESTIONS = 20
REEL_W, REEL_H = 1080, 1920


def ensure_dirs() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
