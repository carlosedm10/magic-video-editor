"""Reel Editor API (spec v5): per-reel overrides (in/out extension, crop_x,
cue text overrides, per-reel subtitle style, editable title/description) and
the copywriter "Regenerate copy" action.

NOTE for the integrator: POST /api/projects/{pid}/reels/{rid}/render already
exists in cutroom/api/pipeline.py (enqueues "reel_render:{rid}" through the
queue, which runs cutroom.pipeline.reels.render_reel — updated to honor every
override below) — it is intentionally NOT duplicated here to avoid two
handlers registered for the same route. This router only needs mounting for
the NEW endpoints:

    from .api import reels as reels_api
    app.include_router(reels_api.router)
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import store
from ..pipeline import reels, subtitles

router = APIRouter(prefix="/api", tags=["reels"])


class SubtitleStyleOverride(BaseModel):
    """Partial override of project["subtitles"] scoped to one reel — every
    field optional; unset fields fall back to the project's own subtitles
    config (merged in cutroom.pipeline.reels._effective_subtitle_cfg)."""

    style: Literal["clean", "bold", "karaoke"] | None = None
    font: str | None = None
    size: Literal["S", "M", "L"] | None = None
    color: str | None = None
    outline_color: str | None = None
    position: Literal["bottom", "center"] | None = None
    words_per_cue: int | None = None


class ReelPatch(BaseModel):
    """All fields optional — only what's provided (model_dump(exclude_unset))
    is applied, so a client can PATCH just one override at a time."""

    in_override: float | None = None
    out_override: float | None = None
    crop_x: float | None = Field(default=None, ge=0.0, le=1.0)
    cue_overrides: dict[str, str] | None = None
    subtitle_style: SubtitleStyleOverride | None = None
    title: str | None = None
    description: str | None = None


def _load_reel(pid: str, rid: str) -> tuple[dict, dict]:
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "project not found") from None
    reel = next((r for r in project.get("reels", []) if r["id"] == rid), None)
    if reel is None:
        raise HTTPException(404, f"reel {rid} not found")
    return project, reel


@router.patch("/projects/{pid}/reels/{rid}")
def reel_patch(pid: str, rid: str, body: ReelPatch):
    project, reel = _load_reel(pid, rid)
    clip = store.get_clip(project, reel["clip_id"])
    duration = (clip.get("info") or {}).get("duration")

    fields = body.model_dump(exclude_unset=True)

    if "in_override" in fields or "out_override" in fields:
        new_in = fields.get("in_override", reel.get("in_override"))
        new_out = fields.get("out_override", reel.get("out_override"))
        eff_in = reel["start"] if new_in is None else float(new_in)
        eff_out = reel["end"] if new_out is None else float(new_out)
        if eff_in < 0:
            raise HTTPException(400, "in_override must be >= 0")
        if duration is not None and eff_out > duration:
            raise HTTPException(400, f"out_override {eff_out} exceeds clip duration {duration}")
        if eff_out <= eff_in:
            raise HTTPException(400, "out_override must be greater than in_override")
        reel["in_override"] = fields.get("in_override", reel.get("in_override"))
        reel["out_override"] = fields.get("out_override", reel.get("out_override"))

    if "crop_x" in fields:
        reel["crop_x"] = fields["crop_x"]

    if "cue_overrides" in fields:
        merged = dict(reel.get("cue_overrides") or {})
        incoming = fields["cue_overrides"] or {}
        merged.update(incoming)
        reel["cue_overrides"] = merged

    if "subtitle_style" in fields:
        merged_style = dict(reel.get("subtitle_style") or {})
        incoming_style = {
            k: v for k, v in (fields["subtitle_style"] or {}).items() if v is not None
        }
        merged_style.update(incoming_style)
        reel["subtitle_style"] = merged_style

    if "title" in fields and fields["title"] is not None:
        reel["title"] = str(fields["title"])[:80]

    if "description" in fields and fields["description"] is not None:
        reel["description"] = str(fields["description"])

    # Any manual edit invalidates a previous render (spec status:
    # suggested|edited|rendered) — the stale rendered file stays at
    # reel["path"] until the reel is re-rendered.
    if fields:
        reel["status"] = "edited"

    store.save(project)
    return reel


@router.post("/projects/{pid}/reels/{rid}/regenerate-copy")
def reel_regenerate_copy(pid: str, rid: str):
    """Explicit copywriter re-run (spec: "Regenerate copy" button) — the one
    path allowed to overwrite a manually-edited title/description."""
    project, reel = _load_reel(pid, rid)
    try:
        reels.regenerate_copy(project, reel)
    except RuntimeError as e:
        raise HTTPException(424, str(e)) from None
    store.save(project)
    return reel


@router.get("/projects/{pid}/reels/{rid}/cues")
def reel_cues(pid: str, rid: str):
    """Cue list for the Reel Editor's Subs tab (index-keyed, matching what
    cue_overrides indices refer to), computed over the reel's EFFECTIVE
    in/out window and merged subtitle style — text already reflects any
    cue_overrides applied."""
    project, reel = _load_reel(pid, rid)
    clip = store.get_clip(project, reel["clip_id"])

    start = reel.get("in_override")
    start = reel["start"] if start is None else float(start)
    end = reel.get("out_override")
    end = reel["end"] if end is None else float(end)

    base = subtitles.normalize_config(project.get("subtitles"))
    cfg = subtitles.normalize_config({**base, **(reel.get("subtitle_style") or {})})

    cues = subtitles.cues_for_range(clip, start, end, cfg)
    overrides = reel.get("cue_overrides") or {}
    for cue in cues:
        override = overrides.get(cue["index"], overrides.get(str(cue["index"])))
        if override is not None:
            cue["text"] = str(override)
    return {"cues": cues}
