"""FastAPI backend: the thin app shell — creates the app, mounts the feature
routers, and keeps UI serving, health, and Range-aware media streaming (so
<video> can seek) directly here. Project/clip/sentence/order endpoints live
in api/projects.py, stage-running endpoints in api/pipeline.py."""

import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from . import config, llm, store
from .api import audio, edl, filters, pipeline, projects, settings

app = FastAPI(title="CutRoom")
UI_DIR = Path(__file__).parent.parent / "ui"

app.include_router(projects.router)
app.include_router(pipeline.router)
app.include_router(settings.router)
app.include_router(audio.router)
app.include_router(filters.router)
app.include_router(edl.router)


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
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ollama": llm.available(),
        "model": config.OLLAMA_MODEL,
        "whisper": config.WHISPER_MODEL,
        "data_dir": str(config.DATA_DIR),
    }


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


@app.get("/api/projects/{pid}/media/file")
def media_file(pid: str, path: str, request: Request):
    pdir = store.project_dir(pid).resolve()
    target = Path(path).resolve()
    if not str(target).startswith(str(pdir)):
        raise HTTPException(403)
    return _stream(target, request)


def main():
    import uvicorn

    config.ensure_dirs()
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
