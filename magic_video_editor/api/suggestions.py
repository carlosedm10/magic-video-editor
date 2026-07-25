"""Suggestions API: list / accept / dismiss the reviewer agent's findings
(project["suggestions"]), populated by magic_video_editor/pipeline/review.py. Accepting
a "cut" suggestion applies it (marks the referenced sentences kept=False and
invalidates the cached EDL so it's rebuilt); accepting "reorder"/"merge" only
flips the suggestion's status — per spec ("suggest, don't delete") the actual
reordering/merging stays a manual Studio edit.

Accepting a "placement" or "duplicate_clip" suggestion (spec v7.3, populated
by magic_video_editor/pipeline/placement.py for incrementally-added clips) instead
splices the clip into project["clip_order"] + project["edl"] via
placement.apply_placement; dismissing either just flips status, leaving the
clip out of clip_order/EDL (still in the media bin). The import below also
registers placement's "analyze_clip:*" queue runner as a side effect, the
same way api/thumbs.py registers the "thumbs" runner."""

from fastapi import APIRouter, HTTPException

from .. import store
from ..pipeline import placement

router = APIRouter(prefix="/api", tags=["suggestions"])


def _load(pid: str) -> dict:
    try:
        return store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404) from None


def _find(project: dict, sid: str) -> dict:
    for s in project.get("suggestions", []):
        if s["id"] == sid:
            return s
    raise HTTPException(404, f"suggestion {sid} not found")


@router.get("/projects/{pid}/suggestions")
def suggestions_list(pid: str):
    project = _load(pid)
    return {"suggestions": project.get("suggestions", [])}


@router.post("/projects/{pid}/suggestions/{sid}/accept")
def suggestions_accept(pid: str, sid: str):
    project = _load(pid)
    suggestion = _find(project, sid)
    if suggestion["kind"] in ("placement", "duplicate_clip"):
        placement.apply_placement(project, suggestion)
    elif suggestion["proposed_action"] == "cut":
        ids = set(suggestion["sentence_ids"])
        for s in project["sentences"]:
            if s["id"] in ids:
                s["kept"] = False
                s["reason"] = "suggestion accepted"
        project["edl"] = None
    suggestion["status"] = "accepted"
    store.save(project)
    return {"suggestion": suggestion}


@router.post("/projects/{pid}/suggestions/{sid}/dismiss")
def suggestions_dismiss(pid: str, sid: str):
    project = _load(pid)
    suggestion = _find(project, sid)
    suggestion["status"] = "dismissed"
    store.save(project)
    return {"suggestion": suggestion}
