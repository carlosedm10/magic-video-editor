"""Reel social safe zones + face safety API (spec v7.7).

NOT mounted anywhere yet — this is a NEW router alongside the existing
api/reels.py (which owns reel PATCH/render and is NOT this task's file).

Mount note for the integrator (magic_video_editor/server.py, alongside the
other `app.include_router(...)` calls):

    from .api import safety as safety_api
    app.include_router(safety_api.router)

GET /api/safezones -> the PLATFORMS spec (platform key, label, zones as
fractions of 1080x1920) — the Reel Editor UI builds its CSS/SVG safe-zone
mockup overlay directly from this, one call, cached client-side (the spec
never changes at runtime).

GET /api/projects/{pid}/reels/{rid}/safety?platform=tiktok|reels|shorts ->
the deterministic geometry result from pipeline/safezones.analyze(). Now also
returns `debug_samples` (per-sample face box + zone, for transparency) and
each `intervals[]` entry carries its own `face_box`/`zone_rect` so the UI can
draw exactly what triggered the warning (spec v7.7 transparency follow-up,
2026-07-25 false-positive fix).

GET /api/projects/{pid}/reels/{rid}/safety/face-at?t=<seconds>&platform=... ->
on-demand single-frame face box lookup at an arbitrary reel-timeline instant
(pipeline/safezones.face_box_at_time()) -- powers the Reel Editor's "Ver
zonas" LIVE face-box tracking as the playhead moves, independent of
analyze()'s ~9 discrete samples. `platform` is optional; when omitted (or
unrecognized) `zone` is always None (still useful just to draw the box).
"""

from typing import Literal

from fastapi import APIRouter, HTTPException

from .. import store
from ..pipeline import reels, safezones

router = APIRouter(prefix="/api", tags=["safety"])

Platform = Literal["tiktok", "reels", "shorts"]


@router.get("/safezones")
def get_safezones() -> dict:
    return {key: {"key": key, **spec} for key, spec in safezones.PLATFORMS.items()}


@router.get("/projects/{pid}/reels/{rid}/safety")
def get_reel_safety(pid: str, rid: str, platform: Platform) -> dict:
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "project not found") from None
    reel = next((r for r in project.get("reels", []) if r["id"] == rid), None)
    if reel is None:
        raise HTTPException(404, f"reel {rid} not found")
    reels.ensure_segments(reel)
    try:
        return safezones.analyze(project, reel, platform)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/projects/{pid}/reels/{rid}/safety/face-at")
def get_reel_face_at(pid: str, rid: str, t: float, platform: Platform | None = None) -> dict:
    try:
        project = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "project not found") from None
    reel = next((r for r in project.get("reels", []) if r["id"] == rid), None)
    if reel is None:
        raise HTTPException(404, f"reel {rid} not found")
    reels.ensure_segments(reel)
    return safezones.face_box_at_time(project, reel, t, platform=platform)
