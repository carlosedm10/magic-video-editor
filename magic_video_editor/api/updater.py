"""GET /api/update (status) + POST /api/update/check (manual re-check) +
POST /api/update/install (spec v6 "Auto-update via GitHub Releases").

Thin HTTP wrapper -- all the actual GitHub Releases / sha256 / helper-script
logic lives in magic_video_editor/updater.py; see that module's docstring."""

from fastapi import APIRouter, HTTPException

from .. import jobs as jobs_module
from .. import updater

router = APIRouter(prefix="/api/update", tags=["update"])


@router.get("")
def update_status():
    """Current known status -- populated by the non-blocking startup check
    (magic_video_editor.updater.start_check_async(), fired from server.py's
    main()). `checked: false` means that background check hasn't finished
    yet (or hasn't started, e.g. under `make smoke`'s bare imports); the UI
    is expected to poll this a few times shortly after boot."""
    return updater.get_status()


@router.post("/check")
def update_check():
    """Synchronous re-check -- e.g. a "Check for Updates…" menu action."""
    return updater.check_for_update()


@router.post("/install")
def update_install():
    try:
        job_id = updater.start_install_job()
    except updater.DevModeError as e:
        raise HTTPException(400, str(e)) from e
    except jobs_module.JobBusyError as e:
        return {"job_id": e.job_id, "already_running": True}
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"job_id": job_id}
