"""Reel Editor API (spec v5): per-reel overrides (in/out extension, crop_x,
cue text overrides, per-reel subtitle style, editable title/description) and
the copywriter "Regenerate copy" action.

NOTE for the integrator: POST /api/projects/{pid}/reels/{rid}/render already
exists in magic_video_editor/api/pipeline.py (enqueues "reel_render:{rid}" through the
queue, which runs magic_video_editor.pipeline.reels.render_reel — updated to honor every
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
    config (merged in magic_video_editor.pipeline.reels._effective_subtitle_cfg)."""

    style: Literal["clean", "bold", "karaoke"] | None = None
    font: str | None = None
    size: Literal["S", "M", "L"] | None = None
    color: str | None = None
    outline_color: str | None = None
    position: Literal["bottom", "center"] | None = None
    words_per_cue: int | None = None


class SegmentInput(BaseModel):
    """One entry of the multi-segment reel (spec v5.8b "the podcast case").
    A PATCH with `segments` REPLACES the whole list wholesale (add/remove/
    reorder/edit a segment all go through the same replace, rather than a
    per-index patch) — the caller is expected to send the full desired list
    each time, same pattern the Timeline EDL editor uses."""

    clip_id: str
    start: float
    end: float
    in_override: float | None = None
    out_override: float | None = None


class TransitionInput(BaseModel):
    type: Literal["none", "fade", "crossfade"] = "crossfade"
    duration: float = Field(default=0.4, gt=0.0, le=1.5)


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
    # Multi-segment reels (spec v5.8b) — see magic_video_editor/pipeline/reels.py's
    # module docstring for the segments/transitions shape.
    segments: list[SegmentInput] | None = None
    transitions: list[TransitionInput] | None = None


def _load_reel(pid: str, rid: str) -> tuple[dict, dict]:
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "project not found") from None
    reel = next((r for r in project.get("reels", []) if r["id"] == rid), None)
    if reel is None:
        raise HTTPException(404, f"reel {rid} not found")
    # Migrate a pre-v5.8b single-window reel to the segments/transitions
    # shape on first read (spec: "migrate single-window reels on read").
    reels.ensure_segments(reel)
    return project, reel


def _validate_window(
    clip_duration: float | None, eff_in: float, eff_out: float, where: str
) -> None:
    if eff_in < 0:
        raise HTTPException(400, f"{where}: in_override must be >= 0")
    if clip_duration is not None and eff_out > clip_duration:
        raise HTTPException(
            400, f"{where}: out_override {eff_out} exceeds clip duration {clip_duration}"
        )
    if eff_out <= eff_in:
        raise HTTPException(400, f"{where}: out_override must be greater than in_override")


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
        _validate_window(duration, eff_in, eff_out, "reel")
        reel["in_override"] = fields.get("in_override", reel.get("in_override"))
        reel["out_override"] = fields.get("out_override", reel.get("out_override"))
        # Keep segment 0 (the legacy fields' source of truth) in sync so a
        # plain top-level PATCH still works for single-segment reels.
        reel["segments"][0]["in_override"] = reel["in_override"]
        reel["segments"][0]["out_override"] = reel["out_override"]

    if "segments" in fields:
        incoming_segments = fields["segments"] or []
        if len(incoming_segments) < 1:
            raise HTTPException(400, "segments must have at least one entry")
        for idx, seg in enumerate(incoming_segments):
            try:
                seg_clip = store.get_clip(project, seg["clip_id"])
            except KeyError:
                raise HTTPException(
                    400, f"segments[{idx}]: unknown clip_id {seg['clip_id']!r}"
                ) from None
            seg_duration = (seg_clip.get("info") or {}).get("duration")
            eff_in = seg["start"] if seg.get("in_override") is None else float(seg["in_override"])
            eff_out = seg["end"] if seg.get("out_override") is None else float(seg["out_override"])
            _validate_window(seg_duration, eff_in, eff_out, f"segments[{idx}]")
        reel["segments"] = incoming_segments

    if "transitions" in fields:
        n_junctions = max(0, len(reel.get("segments") or [reel]) - 1)
        incoming_transitions = fields["transitions"] or []
        if len(incoming_transitions) != n_junctions:
            raise HTTPException(
                400,
                f"transitions must have exactly {n_junctions} entries for "
                f"{len(reel.get('segments') or [reel])} segments",
            )
        reel["transitions"] = incoming_transitions

    if "segments" in fields or "transitions" in fields:
        # Re-derive transitions defaults/length + legacy field mirror after
        # a segments/transitions replace (spec v5.8b).
        reels.ensure_segments(reel)

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
    """Cue list for the Reel Editor's Subs tab, computed over EVERY segment
    of the reel's EFFECTIVE window and merged subtitle style — concatenated
    in segment order with a GLOBAL "index" spanning the whole reel (index 0
    is the first cue of segment 0, continuing into segment 1, ...), matching
    what cue_overrides keys refer to (see magic_video_editor/pipeline/reels.py's module
    docstring). A `segment` field on each cue tells the client which segment
    it belongs to. Text already reflects any cue_overrides applied."""
    project, reel = _load_reel(pid, rid)

    base = subtitles.normalize_config(project.get("subtitles"))
    cfg = subtitles.normalize_config({**base, **(reel.get("subtitle_style") or {})})
    overrides = reel.get("cue_overrides") or {}

    all_cues: list[dict] = []
    global_idx = 0
    for seg_idx, seg in enumerate(reel["segments"]):
        clip = store.get_clip(project, seg["clip_id"])
        start = seg["start"] if seg.get("in_override") is None else float(seg["in_override"])
        end = seg["end"] if seg.get("out_override") is None else float(seg["out_override"])

        speakers = project.get("speakers")
        for cue in subtitles.cues_for_range(clip, start, end, cfg, speakers=speakers):
            override = overrides.get(global_idx, overrides.get(str(global_idx)))
            if override is not None:
                cue["text"] = str(override)
            cue["index"] = global_idx
            cue["segment"] = seg_idx
            all_cues.append(cue)
            global_idx += 1

    return {"cues": all_cues}
