"""Global configuration. Everything local, everything overridable via env."""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# macOS-correct default (v5.13 rename). The old ~/CutRoom default is migrated
# in-place at startup -- see migrate_data_dir() / _maybe_migrate_default_data_dir()
# below -- but ONLY when no MVE_DATA/CUTROOM_DATA override is set.
_OLD_DEFAULT_DATA_DIR = Path.home() / "CutRoom"
_NEW_DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "Magic Video Editor"

_warned_legacy_env: set[str] = set()


def env_with_legacy_fallback(
    new_name: str, old_name: str, default: str | None = None
) -> str | None:
    """Read `new_name`, falling back to the legacy `old_name` (CUTROOM_*) if
    set. Logs one warning per legacy var actually used. Returns `default` if
    neither is set. Public so other modules with their own env var (e.g.
    ffmpeg_utils.CUTROOM_FFMPEG/MVE_FFMPEG) can reuse the same fallback+warn
    behavior."""
    value = os.environ.get(new_name)
    if value is not None:
        return value
    legacy = os.environ.get(old_name)
    if legacy is not None:
        if old_name not in _warned_legacy_env:
            logger.warning(
                "%s is deprecated and will be removed in a future release; use %s instead.",
                old_name,
                new_name,
            )
            _warned_legacy_env.add(old_name)
        return legacy
    return default


def migrate_data_dir(old: Path, new: Path) -> bool:
    """Move `old` to `new` if `old` exists and `new` does not (same-volume
    rename, so it's fast and atomic). Pure function taking explicit paths --
    unit-test THIS with tmp dirs; never call it against real default paths in
    tests. Returns True if a migration happened."""
    old = Path(old)
    new = Path(new)
    if old.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
        logger.warning("Migrated data directory: %s -> %s", old, new)
        return True
    return False


_data_dir_override = env_with_legacy_fallback("MVE_DATA", "CUTROOM_DATA")
# Only auto-migrate the real ~/CutRoom -> new default when the user hasn't
# pinned a data dir via env -- an explicit override means they've opted out
# of the default location entirely, so we must never touch ~/CutRoom for them.
_AUTO_MIGRATE_DEFAULT = _data_dir_override is None

DATA_DIR = Path(_data_dir_override or _NEW_DEFAULT_DATA_DIR).expanduser()
PROJECTS_DIR = DATA_DIR / "projects"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Env fallback only -- magic_video_editor/settings.py (per-task model settings) wins
# whenever it has an opinion; see magic_video_editor/agents/agents.py:get_agent.
OLLAMA_MODEL = env_with_legacy_fallback("MVE_LLM", "CUTROOM_LLM", "qwen2.5:14b")

# mlx-community whisper repo (Apple Silicon). Downloaded on first use by mlx-whisper.
WHISPER_MODEL = env_with_legacy_fallback(
    "MVE_WHISPER", "CUTROOM_WHISPER", "mlx-community/whisper-large-v3-turbo"
)

HOST = env_with_legacy_fallback("MVE_HOST", "CUTROOM_HOST", "127.0.0.1")
PORT = int(env_with_legacy_fallback("MVE_PORT", "CUTROOM_PORT", "8765"))

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


def _maybe_migrate_default_data_dir() -> None:
    """Called from ensure_dirs() at real startup only (never at bare import)
    so that importing this module -- e.g. `make smoke` -- never touches disk.
    No-ops entirely when an env override is set."""
    if _AUTO_MIGRATE_DEFAULT:
        migrate_data_dir(_OLD_DEFAULT_DATA_DIR, _NEW_DEFAULT_DATA_DIR)


def ensure_dirs() -> None:
    _maybe_migrate_default_data_dir()
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
