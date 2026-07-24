"""Global configuration. Everything local, everything overridable via env."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CUTROOM_DATA", Path.home() / "CutRoom")).expanduser()
PROJECTS_DIR = DATA_DIR / "projects"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Env fallback only — cutroom/settings.py (per-task model settings) wins
# whenever it has an opinion; see cutroom/agents/agents.py:get_agent.
OLLAMA_MODEL = os.environ.get("CUTROOM_LLM", "qwen2.5:14b")

# mlx-community whisper repo (Apple Silicon). Downloaded on first use by mlx-whisper.
WHISPER_MODEL = os.environ.get("CUTROOM_WHISPER", "mlx-community/whisper-large-v3-turbo")

HOST = os.environ.get("CUTROOM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CUTROOM_PORT", "8765"))

# Analysis audio format
ANALYSIS_SR = 16000

# Sync detection
SYNC_ENV_RATE = 50  # Hz of the loudness envelope used for coarse alignment
SYNC_MIN_CORR = 0.55  # normalized correlation to consider two clips simultaneous

# Take/dedup detection
DUP_SIMILARITY = 82  # rapidfuzz token_sort_ratio threshold (0-100)
DUP_MIN_WORDS = 4

# Cross-clip semantic dedup (dedup_judge, v4)
CROSS_DEDUP_MIN_SIM = 55  # rapidfuzz token_set_ratio floor to consider a candidate pair
CROSS_DEDUP_MAX_SIM = 100
CROSS_DEDUP_MAX_PAIRS = 40  # cap on candidate pairs sent to the LLM per takes run
CROSS_DEDUP_AUTOCUT_CONFIDENCE = 4  # >= this + same_content -> auto-cut
CROSS_DEDUP_SUGGEST_CONFIDENCE = 2  # >= this (and < autocut) -> open suggestion

# Video topic summary (v4)
TOPIC_INPUT_CHARS = 3000  # truncate full transcript to ~this many chars for video_topic

# Rendering
RENDER_CRF = 18
RENDER_PRESET = "veryfast"
SEGMENT_PAD = 0.12  # seconds of padding around kept sentences
MERGE_GAP = 1.2  # merge adjacent kept sentences closer than this (same clip)

# Reels
REEL_MIN_S = 15.0
REEL_MAX_S = 60.0
REEL_SUGGESTIONS = 20
REEL_W, REEL_H = 1080, 1920


def ensure_dirs() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
