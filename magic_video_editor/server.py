"""FastAPI backend: the thin app shell — creates the app, mounts the feature
routers, and keeps UI serving, health, and Range-aware media streaming (so
<video> can seek) directly here. Project/clip/sentence/order endpoints live
in api/projects.py, stage-running endpoints in api/pipeline.py."""

import atexit
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import __version__, config, ffmpeg_utils, llm, store
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
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ollama": llm.available(),
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
    return _stream(Path(store.get_clip(project, cid)["path"]), request)


@app.get("/api/projects/{pid}/media/preview/{cid}")
def media_preview(pid: str, cid: str, request: Request):
    """Browser-safe preview stream: the per-clip H.264 proxy when one exists
    (HEVC/10-bit/4K sources Chrome can't decode — see docs/PLATFORM-SPEC.md),
    else the original file (already browser-playable)."""
    project = store.load(pid)
    clip = store.get_clip(project, cid)
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
