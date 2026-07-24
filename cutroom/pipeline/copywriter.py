"""SEO copywriter layer (v5 addendum "SEO copywriter + brand profile").

Two entry points:
- `copy_for_reel(project, reel)` -- title/description/hashtags for one reel
  (platform hint "shorts"), using the reel's own transcript text.
- `copy_for_video(project)` -- title/description/hashtags for the full main
  cut (platform hint "youtube"), using the full kept transcript in narrative
  (clip_order) order. Used by the project-level "Publish" block.

Both call the `copywriter` agent task (agents/agents.py, flat
CopywriterOutput schema) and fail open with a deterministic fallback so a
flaky/absent LLM never blocks the UI -- callers get *something* back, never
an exception.
"""

from .. import settings as settings_store
from ..agents.agents import get_agent

_TRANSCRIPT_CHARS = 4000


def _brand_profile() -> str:
    return (settings_store.load().get("brand_profile") or "").strip()


def _full_kept_transcript(project: dict) -> str:
    """Full kept transcript across all clips, in narrative (clip_order)
    order -- same ordering convention as pipeline/review.py."""
    order = project.get("clip_order") or [
        c["id"] for c in project.get("clips", []) if c.get("role") == "camera"
    ]
    parts = []
    for cid in order:
        kept = sorted(
            (
                s
                for s in project.get("sentences", [])
                if s["clip_id"] == cid and s.get("kept")
            ),
            key=lambda s: s["start"],
        )
        parts.extend(s["text"] for s in kept)
    return " ".join(parts)[:_TRANSCRIPT_CHARS]


def _prompt(transcript: str, topic: str, brand_profile: str, platform: str) -> str:
    return (
        f"Video topic: {topic or '(unknown)'}\n\n"
        f"Brand profile: {brand_profile or '(none provided)'}\n\n"
        f"Platform: {platform}\n\n"
        f"Transcript: {transcript or '(empty)'}"
    )


def _run(transcript: str, topic: str, platform: str, *, fallback_title: str) -> dict:
    result = {
        "title": fallback_title,
        "description": "",
        "hashtags": "",
    }
    if not transcript.strip():
        return result
    try:
        agent = get_agent("copywriter")
        prompt = _prompt(transcript, topic, _brand_profile(), platform)
        out = agent.run_sync(prompt).output
        return {
            "title": (out.title or fallback_title).strip()[:70] or fallback_title,
            "description": (out.description or "").strip(),
            "hashtags": (out.hashtags or "").strip(),
        }
    except Exception:
        # Fail open: keep whatever title already existed (reel_scorer's title,
        # or the project name), no description/hashtags. Never raise -- the
        # caller (an on-demand button / a reels-stage pass) must not break.
        return result


def copy_for_reel(project: dict, reel: dict) -> dict:
    """Generate {title, description, hashtags} for one reel. Deterministic
    fallback keeps the reel's existing title (from reel_scorer) untouched."""
    fallback_title = reel.get("title") or f"Reel {reel.get('rank', '')}".strip()
    return _run(
        reel.get("text", ""),
        project.get("topic", ""),
        "shorts",
        fallback_title=fallback_title,
    )


def copy_for_video(project: dict) -> dict:
    """Generate {title, description, hashtags} for the full main cut.
    Deterministic fallback keeps the project name as the title."""
    fallback_title = project.get("name") or "Untitled"
    return _run(
        _full_kept_transcript(project),
        project.get("topic", ""),
        "youtube",
        fallback_title=fallback_title,
    )
