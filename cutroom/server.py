"""FastAPI backend: serves the UI, the project API, media streaming (with Range
support so <video> can seek), and launches pipeline stages as background jobs."""

import os
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import config, jobs, llm, store
from .pipeline import ingest, ordering, render, reels, sync, takes, transcribe

app = FastAPI(title="CutRoom")
UI_DIR = Path(__file__).parent.parent / "ui"

STAGES = {
    "ingest": ingest.run,
    "sync": sync.run,
    "transcribe": transcribe.run,
    "takes": takes.run,
    "order": ordering.run,
    "render": render.run,
    "reels": reels.suggest,
}
STAGE_ORDER = list(STAGES.keys())


# ---------- UI ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return (UI_DIR / "index.html").read_text()


@app.get("/ui/{name}")
def ui_asset(name: str):
    f = UI_DIR / name
    if not f.exists() or ".." in name:
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


# ---------- projects ----------

class NewProject(BaseModel):
    name: str


class AddClips(BaseModel):
    paths: list[str]


class ClipUpdate(BaseModel):
    role: str | None = None
    is_main: bool | None = None


class SentenceUpdate(BaseModel):
    kept: bool


class OrderUpdate(BaseModel):
    clip_order: list[str]


@app.get("/api/projects")
def projects_list():
    return store.list_projects()


@app.post("/api/projects")
def projects_create(body: NewProject):
    return store.new_project(body.name.strip() or "Untitled")


@app.get("/api/projects/{pid}")
def project_get(pid: str):
    try:
        p = store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404)
    p["edl_preview"] = ordering.build_edl(p) if p.get("sentences") else []
    return p


@app.delete("/api/projects/{pid}")
def project_delete(pid: str):
    store.delete_project(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/clips")
def clips_add(pid: str, body: AddClips):
    project = store.load(pid)
    added = ingest.add_clips(project, body.paths)
    return {"added": len(added), "clips": project["clips"]}


@app.post("/api/projects/{pid}/clips/{cid}")
def clip_update(pid: str, cid: str, body: ClipUpdate):
    project = store.load(pid)
    clip = store.get_clip(project, cid)
    if body.role in ("camera", "audio"):
        clip["role"] = body.role
    if body.is_main is not None:
        for c in project["clips"]:
            c["is_main"] = False
        clip["is_main"] = body.is_main
    store.save(project)
    return clip


@app.delete("/api/projects/{pid}/clips/{cid}")
def clip_remove(pid: str, cid: str):
    project = store.load(pid)
    project["clips"] = [c for c in project["clips"] if c["id"] != cid]
    project["sentences"] = [s for s in project.get("sentences", []) if s["clip_id"] != cid]
    store.save(project)
    return {"ok": True}


@app.post("/api/projects/{pid}/sentences/{sid}")
def sentence_update(pid: str, sid: str, body: SentenceUpdate):
    project = store.load(pid)
    for s in project["sentences"]:
        if s["id"] == sid:
            s["kept"] = body.kept
            s["reason"] = "" if body.kept else "excluded manually"
            store.save(project)
            return s
    raise HTTPException(404)


@app.post("/api/projects/{pid}/order")
def order_update(pid: str, body: OrderUpdate):
    project = store.load(pid)
    project["clip_order"] = body.clip_order
    project["order_notes"] = "manual order"
    store.save(project)
    return {"ok": True}


# ---------- pipeline ----------

@app.post("/api/projects/{pid}/run/{stage}")
def run_stage(pid: str, stage: str):
    if stage not in STAGES:
        raise HTTPException(400, f"unknown stage {stage}")
    project = store.load(pid)
    fn = STAGES[stage]

    def task(log, project=project, stage=stage):
        try:
            fn(log, project)
            store.mark_stage(project, stage, "done")
        except Exception as e:
            store.mark_stage(project, stage, "error", str(e)[:300])
            raise

    return {"job": jobs.start(f"{stage}:{pid}", task)}


@app.post("/api/projects/{pid}/reels/{rid}/render")
def reel_render(pid: str, rid: str):
    project = store.load(pid)
    return {"job": jobs.start(f"reel:{rid}",
                              lambda log: reels.render_reel(log, project, rid))}


@app.get("/api/jobs/{jid}")
def job_get(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    return job


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
        reader(), status_code=206, media_type=media,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        })


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
