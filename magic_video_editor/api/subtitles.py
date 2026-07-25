"""Subtitles API (spec v4 §6): persist project["subtitles"] config, expose
the word-timed cue list (for the frontend's Draft-mode DOM overlay, built by
magic_video_editor/pipeline/subtitles.cue_list off the persisted EDL), and the curated
font list for the Subtitles inspector tab's font dropdown."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store
from ..pipeline import subtitles

router = APIRouter(prefix="/api", tags=["subtitles"])


class SubtitlesUpdate(BaseModel):
    enabled: bool = False
    style: Literal["clean", "bold", "karaoke"] = "clean"
    font: str = subtitles.DEFAULTS["font"]
    size: Literal["S", "M", "L"] = "M"
    color: str = subtitles.DEFAULTS["color"]
    outline_color: str = subtitles.DEFAULTS["outline_color"]
    position: Literal["bottom", "center"] = "bottom"
    words_per_cue: int = subtitles.DEFAULTS["words_per_cue"]
    speaker_names: bool = subtitles.DEFAULTS["speaker_names"]
    # v7 §7.6: vertical-drag nudge (fraction of frame height) + project-level
    # cue text overrides ({cue_index: text}, GLOBAL index -- see
    # pipeline/subtitles.cue_list's docstring). Keys arrive as JSON object
    # keys (always strings); cue_list/segment_cue_overrides accept both.
    vpos: float = subtitles.DEFAULTS["vpos"]
    cue_overrides: dict[str, str] = {}


@router.get("/projects/{pid}/subtitles")
def subtitles_get(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    return subtitles.normalize_config(project.get("subtitles"))


@router.put("/projects/{pid}/subtitles")
def subtitles_put(pid: str, body: SubtitlesUpdate):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    cfg = subtitles.normalize_config(body.model_dump())
    project["subtitles"] = cfg
    store.save(project)
    return cfg


@router.get("/projects/{pid}/subtitles/cues")
def subtitles_cues(pid: str):
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None
    return {"cues": subtitles.cue_list(project)}


@router.get("/fonts")
def fonts_list():
    return {"fonts": subtitles.FONTS}
