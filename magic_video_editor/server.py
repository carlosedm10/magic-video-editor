"""FastAPI backend: the thin app shell — creates the app, mounts the feature
routers, and keeps UI serving, health, and Range-aware media streaming (so
<video> can seek) directly here. Project/clip/sentence/order endpoints live
in api/projects.py, stage-running endpoints in api/pipeline.py."""

import atexit
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from . import __version__, config, ffmpeg_utils, llm, ollama_manager, store, updater
from .api import (
    audio,
    edl,
    filters,
    ollama,
    overlays,
    pipeline,
    projects,
    reels,
    settings,
    subtitles,
    suggestions,
    thumbs,
)
from .api import safety as safety_api
from .api import transitions as transitions_api
from .api import updater as updater_api

app = FastAPI(title="Magic Video Editor")
UI_DIR = Path(__file__).parent.parent / "ui"

app.include_router(projects.router)
app.include_router(pipeline.router)
app.include_router(settings.router)
app.include_router(audio.router)
app.include_router(filters.router)
app.include_router(edl.router)
app.include_router(suggestions.router)
app.include_router(ollama.router)
app.include_router(subtitles.router)
app.include_router(thumbs.router)
app.include_router(reels.router)
app.include_router(overlays.router)
app.include_router(updater_api.router)
app.include_router(transitions_api.router)
app.include_router(safety_api.router)


# ---------- error handling ----------
#
# Belt-and-suspenders: most api/*.py endpoints already catch
# store.ProjectNotFound (as `except FileNotFoundError`, its parent class)
# themselves and raise a clean HTTPException(404). These handlers are the
# safety net for the ones that don't (e.g. api/pipeline.py's queue
# endpoints, api/projects.py's clip/sentence/order mutations) so a deleted
# -- or, pre-migration-fix, a merely-mis-pathed -- project id never surfaces
# as a raw 500 traceback. Registered for both the specific ProjectNotFound
# (clear "project not found" message) and plain FileNotFoundError (e.g. a
# media file referenced by a project that's gone missing on disk) so
# whichever one an endpoint's code path actually raises still comes back as
# JSON, not a stack trace.


@app.exception_handler(store.ProjectNotFound)
async def project_not_found_handler(_request: Request, exc: store.ProjectNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "project not found"})


@app.exception_handler(FileNotFoundError)
async def file_not_found_handler(_request: Request, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "not found"})


# ---------- UI ----------


@app.get("/", response_class=HTMLResponse)
def index():
    return (UI_DIR / "index.html").read_text()


@app.get("/ui/{path:path}")
def ui_asset(path: str):
    f = (UI_DIR / path).resolve()
    if not f.is_relative_to(UI_DIR.resolve()) or not f.exists() or f.is_dir():
        raise HTTPException(404)
    media = {"js": "text/javascript", "css": "text/css"}.get(f.suffix[1:], "text/plain")
    return Response(f.read_text(), media_type=media)


# ---------- health ----------


@app.get("/api/health")
def health():
    return {
        "name": "Magic Video Editor",
        "version": __version__,
        "by": "carlosedm10",
        "ffmpeg": ffmpeg_utils.binaries_status()["ffmpeg"],
        "ffprobe": ffmpeg_utils.binaries_status()["ffprobe"],
        "ollama": llm.available(),
        "ollama_mode": llm.mode(),
        "model": config.OLLAMA_MODEL,
        "whisper": config.WHISPER_MODEL,
        "data_dir": str(config.DATA_DIR),
    }


# ---------- folder open (Settings > General "Open folder", v4 section 5) ----------


class OpenFolderRequest(BaseModel):
    path: str


@app.post("/api/open-folder")
def open_folder(body: OpenFolderRequest):
    home = Path.home().resolve()
    target = Path(body.path).expanduser().resolve()
    if not (target == home or target.is_relative_to(home)):
        raise HTTPException(400, "path must be inside the user's home directory")
    target.mkdir(parents=True, exist_ok=True)
    if sys.platform != "darwin":
        raise HTTPException(400, "opening a folder is only supported on macOS")
    subprocess.run(["open", str(target)], check=True)
    return {"ok": True}


# ---------- media streaming (Range-aware for <video> seeking) ----------


def _stream(path: Path, request: Request):
    if not path.exists():
        raise HTTPException(404)
    size = path.stat().st_size
    range_header = request.headers.get("range")
    media = "video/mp4" if path.suffix != ".wav" else "audio/wav"
    if not range_header:
        return FileResponse(path, media_type=media)
    start_s, _, end_s = range_header.replace("bytes=", "").partition("-")
    start = int(start_s)
    end = int(end_s) if end_s else min(start + 4 * 1024 * 1024, size - 1)
    end = min(end, size - 1)

    def reader():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        reader(),
        status_code=206,
        media_type=media,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


@app.get("/api/projects/{pid}/media/clip/{cid}")
def media_clip(pid: str, cid: str, request: Request):
    project = store.load(pid)
    try:
        clip = store.get_clip(project, cid)
    except KeyError:
        raise HTTPException(404) from None
    return _stream(Path(clip["path"]), request)


@app.get("/api/projects/{pid}/media/preview/{cid}")
def media_preview(pid: str, cid: str, request: Request):
    """Browser-safe preview stream: the per-clip H.264 proxy when one exists
    (HEVC/10-bit/4K sources Chrome can't decode — see docs/PLATFORM-SPEC.md),
    else the original file (already browser-playable)."""
    project = store.load(pid)
    try:
        clip = store.get_clip(project, cid)
    except KeyError:
        raise HTTPException(404) from None
    path = clip.get("proxy") or clip["path"]
    return _stream(Path(path), request)


@app.get("/api/projects/{pid}/media/file")
def media_file(pid: str, path: str, request: Request):
    pdir = store.project_dir(pid).resolve()
    target = Path(path).resolve()
    if not str(target).startswith(str(pdir)):
        raise HTTPException(403)
    return _stream(target, request)


def main():
    import asyncio
    import signal

    import uvicorn

    config.ensure_dirs()

    # Field bug fix (M2): mlx-whisper shells out to bare `ffmpeg`/`ffprobe`
    # from PATH internally, bypassing our ffmpeg_bin()/ffprobe_bin()
    # resolution entirely -- make sure PATH already points at the right
    # binaries before any pipeline stage can run. See ffmpeg_utils.
    ffmpeg_utils.export_binaries_to_path()

    # v6 packaging Option B: prefer a system Ollama already reachable at
    # config.OLLAMA_URL; else spawn our bundled binary if one was vendored
    # (packaging/fetch_ollama.sh), or self-provision by downloading it if
    # not. Runs on a background thread (never blocks startup) -- GET
    # /api/health's ollama_mode reflects "starting"/"downloading" progress
    # until it settles on system/bundled/downloaded/unreachable.
    ollama_manager.ensure_ollama_async()

    # v6 auto-update: non-blocking GitHub Releases check (never delays boot,
    # fail-silent -- see magic_video_editor/updater.py).
    updater.start_check_async()

    # Resource safety: no ffmpeg child must survive the server (spec:
    # "Resource safety" -- orphaned ffmpeg used to outlive Ctrl-C by ~2min).
    # atexit is the fallback for any normal interpreter exit; the explicit
    # SIGTERM/SIGINT handlers below make sure the registry is torn down
    # *before* uvicorn's own graceful shutdown runs, and chain to uvicorn's
    # handle_exit so its shutdown still happens (never swallowed).
    atexit.register(ffmpeg_utils.terminate_all)

    cfg = uvicorn.Config(app, host=config.HOST, port=config.PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    # We install our own signal handlers below (chaining to server.handle_exit),
    # so prevent uvicorn's serve() from installing (and overriding) its own.
    server.install_signal_handlers = lambda: None

    async def run_server():
        loop = asyncio.get_event_loop()

        def _handle_exit(sig, _frame=None):
            ffmpeg_utils.terminate_all()
            server.handle_exit(sig, _frame)

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_exit, sig, None)

        await server.serve()

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
