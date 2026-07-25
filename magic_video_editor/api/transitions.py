"""Transitions catalog endpoint (spec v7.5): exposes ffmpeg's native xfade
named transitions (~58 of them, probed from the bundled binary) so the
Studio/Reel Editor FX inspector tab can browse and apply them to an EDL/reel
junction. No custom GLSL catalog needed (gl-transitions is the GLSL
catalog standard but needs a non-stock ffmpeg build — rejected per spec):
xfade ships in every stock ffmpeg build via `-filter_complex
xfade=transition=<name>`.

`GET /api/transitions` -> [{name, label_es, category, xfade_name}]. `name`
is what a client stores as EDL/reel transition.type (validated by
`render.valid_type_names()`, the single source of truth reused by
api/edl.py); `xfade_name` is the literal value passed to ffmpeg's
`xfade=transition=<xfade_name>` (currently always == name). `category` is
one of the 5 spec buckets (Fundidos/Barridos/Deslizamientos/Geométricas/
Píxel) for the FX browser's section grouping.

The actual catalog data (CATALOG_SPEC), the ffmpeg probe, and the caching
live in magic_video_editor/pipeline/render.py — the render pipeline is what
needs the xfade name list to build junction merges, so that's the source of
truth; this module is a thin re-export for the read-only API surface
(dependency direction stays the normal api -> pipeline way, api/edl.py
validates against the same `render.valid_type_names()` rather than against
this module).

Mount note for the integrator: this router needs mounting in server.py like
every other api/*.py router:

    from .api import transitions as transitions_api
    app.include_router(transitions_api.router)
"""

from fastapi import APIRouter

from ..pipeline import render

router = APIRouter(prefix="/api", tags=["transitions"])


@router.get("/transitions")
def list_transitions():
    return render.get_catalog()
