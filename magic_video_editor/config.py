"""Global configuration. Everything local, everything overridable via env."""

import json
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


def rewrite_project_json_paths(projects_dir: Path, old_prefix: str, new_prefix: str) -> int:
    """Walk `projects_dir`/*/project.json and rewrite every absolute path
    string carrying `old_prefix` to `new_prefix`. Text-level string replace
    (load as text, str.replace, validate, write back) rather than a
    structural walk -- project.json has paths scattered all over the schema
    (clip["path"], source_path, wav, proxy, thumbs.*, preview.path,
    renders[].path, reels[].path, ...) and new ones get added over time;
    a plain prefix replace on the raw text catches all of them without
    hand-enumerating the schema. Validates the rewritten text parses as
    JSON *before* writing anything -- a file that fails to validate is
    skipped (and logged) rather than risking a corrupt project.json.
    Each file is written atomically (tmp + os.replace). Returns the number
    of files actually rewritten."""
    if not projects_dir.exists():
        return 0
    rewritten = 0
    for project_json in sorted(projects_dir.glob("*/project.json")):
        try:
            text = project_json.read_text()
        except OSError as e:
            logger.error("Could not read %s for path migration: %s", project_json, e)
            continue
        if old_prefix not in text:
            continue
        new_text = text.replace(old_prefix, new_prefix)
        try:
            json.loads(new_text)
        except json.JSONDecodeError as e:
            logger.error(
                "Skipped path rewrite for %s: result failed to parse as JSON (%s)",
                project_json,
                e,
            )
            continue
        tmp = project_json.with_suffix(".tmp")
        tmp.write_text(new_text)
        tmp.replace(project_json)
        rewritten += 1
    return rewritten


def migrate_data_dir(old: Path, new: Path) -> bool:
    """Move `old` to `new` if `old` exists and `new` does not (same-volume
    rename, so it's fast and atomic), then rewrite any absolute paths
    stored INSIDE each projects/*/project.json that still point at `old`
    (see rewrite_project_json_paths) -- otherwise every clip/wav/proxy/
    render/reel path baked into project.json keeps pointing at the
    now-gone `old` location and every read 500s with "No such file or
    directory". Pure function taking explicit paths -- unit-test THIS with
    tmp dirs; never call it against real default paths in tests. Returns
    True if a migration happened."""
    old = Path(old)
    new = Path(new)
    if old.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
        logger.warning("Migrated data directory: %s -> %s", old, new)
        n = rewrite_project_json_paths(new / "projects", str(old), str(new))
        if n:
            logger.warning(
                "Rewrote absolute paths in %d project.json file(s) after migration (%s -> %s)",
                n,
                old,
                new,
            )
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

# Cross-clip dedup candidate PRE-FILTER (perf fix, v4 write-up never
# implemented this): before the O(kept^2) fuzzy token_set_ratio loop, bucket
# kept sentences by RARE keywords (words that appear in only a handful of
# sentences project-wide) and only fuzzy-compare cross-clip pairs that share
# one -- a long recording no longer does hundreds of thousands of
# comparisons. CROSS_DEDUP_MAX_PAIRS below still caps what's sent to the LLM.
CROSS_DEDUP_KEYWORD_MIN_LEN = 5  # chars; short words are rarely a useful bucket key
CROSS_DEDUP_KEYWORD_MAX_DF = 6  # a word used in more than this many sentences is too common

# Context check (out-of-context / meta-aside pass, v4 section 1 point 2).
# Chunked+capped the same way as CLEANER_CHUNK_SIZE/SEQUENCER_WINDOW_SIZE
# (was one LLM call PER SENTENCE -- O(sentences); now O(sentences/chunk)).
CONTEXT_CHECK_CHUNK_SIZE = 15
CONTEXT_CHECK_CHUNK_OVERLAP = 3
CONTEXT_CHECK_MAX_SENTENCES = 300  # hard cap on sentences fed to context_check per takes run
# Confidence gate ("suggest, don't delete"): only high-confidence verdicts
# auto-cut; lower-confidence ones become project["suggestions"] instead of a
# silent cut. Same shape/naming as CROSS_DEDUP_AUTOCUT_CONFIDENCE/SUGGEST_CONFIDENCE.
CONTEXT_CHECK_AUTOCUT_CONFIDENCE = 4
CONTEXT_CHECK_SUGGEST_CONFIDENCE = 2

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

# Main audio track / music bed (vNext "MUSIC BED WITH AUTO-DUCKING"):
# project["audio_track"] mixes an imported audio_assets entry under the
# program (clips') audio on final render + reels. See
# pipeline/render.py:_apply_music_bed for the filtergraph these feed.
# sidechaincompress threshold (linear 0..1) -- program audio above this ducks the music
MUSIC_DUCK_THRESHOLD = 0.05
MUSIC_DUCK_RATIO = 8.0  # sidechaincompress ratio applied to the music while ducked
MUSIC_DUCK_ATTACK_MS = 20.0  # sidechaincompress attack (ms) -- how fast the duck kicks in
# sidechaincompress release (ms) -- how fast the music recovers in gaps
MUSIC_DUCK_RELEASE_MS = 400.0
# default project["audio_track"]["gain_db"] when first placed on the timeline
MUSIC_GAIN_DEFAULT_DB = -6.0
# alimiter ceiling (linear, ~-0.4 dBFS) applied after mixing music + program audio
MUSIC_MIX_LIMIT = 0.95


def _maybe_migrate_default_data_dir() -> None:
    """Called from ensure_dirs() at real startup only (never at bare import)
    so that importing this module -- e.g. `make smoke` -- never touches disk.
    No-ops entirely when an env override is set."""
    if _AUTO_MIGRATE_DEFAULT:
        migrate_data_dir(_OLD_DEFAULT_DATA_DIR, _NEW_DEFAULT_DATA_DIR)


def ensure_dirs() -> None:
    _maybe_migrate_default_data_dir()
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
