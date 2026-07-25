"""Per-project FIFO job queue (spec v4 section 2 -- "replace reject-with-409
for user actions").

State lives in project["queue"]: a list of
{id, kind, payload, status, created_at, progress, job_id, error}, status one
of pending|running|done|error|cancelled. A single lazily-started global
worker thread pops the next pending item, round-robin across projects, with
at most one running item per project at a time, and executes it via
KIND_RUNNERS -- a {kind: callable(log, project, payload)} registry other
modules populate at import time with register_runner(). Kinds may also be
registered as a wildcard prefix ("reel_render:*") to handle a family of
dynamic kinds like "reel_render:<id>"; an unregistered kind errors just that
one item (see _run_item) without touching the worker thread.

Execution itself is delegated to jobs.run_sync(), which gives each item the
same JobLog/stage/cancel machinery as the older one-thread-per-job jobs.start()
path, just run synchronously on this module's single worker thread."""

import threading
import time
import uuid
from collections.abc import Callable

from . import jobs, store

# kind (or "prefix:*") -> callable(log, project, payload) -> None
KIND_RUNNERS: dict[str, Callable] = {}

_worker_lock = threading.Lock()
_worker_started = False
_wake = threading.Event()
_rr_lock = threading.Lock()
_rr_index = 0

# Guards every load-mutate-save critical section on project["queue"].
# store.save() only makes the write itself atomic (tmp + replace); without
# this, two threads racing a read-modify-write (e.g. the worker claiming an
# item concurrently with an API request cancelling/enqueuing another one on
# the same project) can lose each other's edit, last-save-wins. Global
# rather than per-project: queue mutations are cheap and infrequent, so the
# extra serialization across unrelated projects isn't worth per-pid lock
# bookkeeping. Never held across a runner's actual execution (see _run_item).
# RLock, not Lock: _mark_item(finished=True) -> _run_auto_enqueue_hooks ->
# enqueue() re-enters this same lock from the same (worker) thread.
_state_lock = threading.RLock()


def register_runner(kind: str, fn: Callable) -> None:
    """Register a runner for an exact kind ("run-all") or a wildcard prefix
    ("reel_render:*", matching any kind starting with "reel_render:")."""
    KIND_RUNNERS[kind] = fn


def _resolve_runner(kind: str) -> Callable | None:
    runner = KIND_RUNNERS.get(kind)
    if runner is not None:
        return runner
    if ":" in kind:
        return KIND_RUNNERS.get(kind.split(":", 1)[0] + ":*")
    return None


def _new_item(kind: str, payload: dict) -> dict:
    return {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "payload": payload,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "progress": 0.0,
        "job_id": None,
        "error": None,
    }


def enqueue(pid: str, kind: str, payload: dict | None = None, dedupe: bool = True) -> dict:
    """Append a queue item for project `pid` and make sure the worker is
    running. If `dedupe`, skip (returning the existing item) when an
    identical *pending* item (same kind + payload) is already queued."""
    payload = payload or {}
    with _state_lock:
        project = store.load(pid)
        q = project.setdefault("queue", [])
        if dedupe:
            for item in q:
                if (
                    item["status"] == "pending"
                    and item["kind"] == kind
                    and item["payload"] == payload
                ):
                    return item
        item = _new_item(kind, payload)
        q.append(item)
        store.save(project, preserve_queue=False)
    _ensure_worker()
    return item


def list_queue(pid: str) -> list[dict]:
    project = store.load(pid)
    return project.setdefault("queue", [])


def cancel_item(pid: str, item_id: str) -> bool:
    """pending -> removed outright. running -> cooperative cancel via the
    underlying job (jobs.cancel sets cancel_requested + terminates that
    job's OWN ffmpeg children; the runner notices on its next log()/
    progress() call and _run_item marks the item cancelled/error once it
    returns). Returns False if the item doesn't exist or is already
    finished.

    Looks the item up (and, for pending, removes it) while holding
    _state_lock, but drops the lock BEFORE calling jobs.cancel() -- that
    call blocks for up to ~5s waiting for the job's ffmpeg children to
    actually terminate (ffmpeg_utils.terminate_job), and _state_lock is a
    GLOBAL lock shared by every project's queue reads/writes (enqueue/
    list_queue/reorder/_mark_item/_run_item all take it); holding it
    across that wait would stall the whole app's queue traffic just to
    cancel one project's one item (finding 13)."""
    job_id: str | None = None
    with _state_lock:
        project = store.load(pid)
        q = project.setdefault("queue", [])
        item = next((i for i in q if i["id"] == item_id), None)
        if item is None:
            return False
        if item["status"] == "pending":
            q.remove(item)
            store.save(project, preserve_queue=False)
            return True
        if item["status"] != "running":
            return False
        job_id = item.get("job_id")
    if job_id:
        jobs.cancel(job_id)
    return True


def cancel_running_and_wait(pid: str, timeout: float = 10.0) -> None:
    """Best-effort pre-delete hook (finding 1d): if `pid` currently has a
    RUNNING queue item, request cooperative cancellation (jobs.cancel) and
    poll (bounded, `timeout` seconds max, 100ms granularity -- never an
    unbounded wait) until the worker has actually stopped touching it
    (status flips away from "running") before the caller -- api/projects.py's
    project_delete -- rmtree's the project directory out from under it.

    Chosen over refusing the delete with a 409: the worker-internals guards
    added alongside this (every store.load(pid) in queue.py's worker path
    now survives store.ProjectNotFound / a vanished dir) already make an
    in-flight rmtree SAFE -- the worker just no-ops its bookkeeping instead
    of dying -- so this function only needs to make delete-while-running
    CLEAN (the cancelled job settles into a normal terminal status, ffmpeg
    children are torn down promptly) rather than merely non-fatal. A 409
    would just push the "wait and retry" burden onto every caller (the
    frontend) for a case this module can already resolve itself in at most
    a few seconds; if the runner ignores cancellation and the timeout
    elapses anyway, this still returns -- the delete proceeds, and the
    self-healing guards above are exactly what makes that safe.

    A no-op (returns immediately) if the project is already gone or has no
    running item."""
    try:
        project = store.load(pid)
    except store.ProjectNotFound:
        return
    q = project.get("queue", [])
    running_item = next((i for i in q if i["status"] == "running"), None)
    if running_item is None:
        return
    job_id = running_item.get("job_id")
    if job_id:
        jobs.cancel(job_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        try:
            project = store.load(pid)
        except store.ProjectNotFound:
            return
        q = project.get("queue", [])
        if not any(i["status"] == "running" for i in q):
            return
        time.sleep(0.1)


def reorder(pid: str, ids: list[str]) -> list[dict]:
    """Reorder the PENDING subset of the queue to match `ids` (running/done/
    error/cancelled items are left in place). Pending ids not listed keep
    their relative order and are appended after the listed ones."""
    with _state_lock:
        project = store.load(pid)
        q = project.setdefault("queue", [])
        pending_by_id = {i["id"]: i for i in q if i["status"] == "pending"}
        others = [i for i in q if i["status"] != "pending"]
        ordered = [pending_by_id[i] for i in ids if i in pending_by_id]
        leftover = [i for i in q if i["status"] == "pending" and i["id"] not in ids]
        project["queue"] = others + ordered + leftover
        store.save(project, preserve_queue=False)
        return project["queue"]


def _reconcile_stale_running() -> None:
    """Run exactly once, the first time the worker starts in this process:
    any queue item left "running" from a PREVIOUS process is unrecoverable
    (jobs.py's job registry is purely in-memory, so that job_id can never
    resolve again after a restart) and would otherwise wedge that project's
    queue forever -- _next_item() refuses to start a new item for a project
    that already has one "running". Found live: killing the server mid-job
    left project c7642fc7755e stuck on a phantom "running" stage:reels item
    across a fresh boot. Mark any such items as errored-by-restart so the
    queue can make progress again."""
    for pid in _all_project_ids():
        with _state_lock:
            try:
                project = store.load(pid)
            except Exception:
                continue
            q = project.get("queue", [])
            changed = False
            for item in q:
                if item["status"] == "running":
                    item["status"] = "error"
                    item["error"] = "interrupted by server restart"
                    changed = True
            if changed:
                store.save(project, preserve_queue=False)


_worker_thread: threading.Thread | None = None


def _ensure_worker() -> None:
    """Start the global worker thread if it has never run, OR respawn it if
    it has died/gone missing since (thread is None or not is_alive()) --
    relying solely on the _worker_started one-shot latch meant an unforeseen
    death of the worker (e.g. an exception escaping _worker_loop entirely)
    would never self-heal: every enqueue() afterwards would see
    _worker_started already True and skip spawning a replacement, silently
    wedging every project's queue forever. _reconcile_stale_running() still
    only runs on the very first start of this process (a respawn is NOT a
    process restart, so there's nothing stale to reconcile)."""
    global _worker_started, _worker_thread
    with _worker_lock:
        if not _worker_started or _worker_thread is None or not _worker_thread.is_alive():
            first_start = not _worker_started
            _worker_started = True
            if first_start:
                _reconcile_stale_running()
            _worker_thread = threading.Thread(
                target=_worker_loop, daemon=True, name="queue-worker"
            )
            _worker_thread.start()
    _wake.set()


def _all_project_ids() -> list[str]:
    return [p["id"] for p in store.list_projects()]


def _next_item() -> tuple[str, str] | None:
    """(project_id, item_id) of the next pending item to run, round-robin
    across projects that don't already have one running."""
    global _rr_index
    with _rr_lock:
        pids = _all_project_ids()
        if not pids:
            return None
        n = len(pids)
        start = _rr_index % n
        for k in range(n):
            pid = pids[(start + k) % n]
            try:
                project = store.load(pid)
            except Exception:
                continue
            q = project.get("queue", [])
            if any(i["status"] == "running" for i in q):
                continue
            pending = next((i for i in q if i["status"] == "pending"), None)
            if pending is not None:
                _rr_index = (pids.index(pid) + 1) % n
                return pid, pending["id"]
        return None


def _worker_loop() -> None:
    while True:
        try:
            picked = _next_item()
        except Exception:
            picked = None
        if picked is None:
            _wake.clear()
            _wake.wait(timeout=2.0)
            continue
        pid, item_id = picked
        try:
            _run_item(pid, item_id)
        except Exception as e:
            # A bug in _run_item's own bookkeeping (not in the queued
            # runner -- that's already caught by jobs.run_sync) must not
            # take the whole worker thread down. _mark_item is itself
            # guarded against the project having been deleted out from
            # under it (store.ProjectNotFound / a vanished dir), but this
            # except-handler is ALSO wrapped so that NOTHING it does can
            # escape and kill this daemon thread -- that exact escape (this
            # handler's own store.load() raising ProjectNotFound a SECOND
            # time, for a project deleted mid-job) used to kill the sole
            # worker thread forever, wedging every future job in every
            # project (finding 1). _ensure_worker() would also now respawn
            # a dead worker, but this handler existing means it never has
            # to.
            try:
                _mark_item(pid, item_id, status="error", error=f"queue internal error: {e}")
            except Exception as mark_err:
                print(
                    f"[queue] worker bookkeeping failed for {pid}/{item_id} "
                    f"(item left as-is): {mark_err}"
                )


def _run_item(pid: str, item_id: str) -> None:
    with _state_lock:
        try:
            project = store.load(pid)
        except store.ProjectNotFound:
            # Project was deleted between being picked by _next_item and
            # actually starting here -- nothing to run, nothing to mark.
            return
        q = project.get("queue", [])
        item = next((i for i in q if i["id"] == item_id), None)
        if item is None or item["status"] != "pending":
            return
        item["status"] = "running"
        try:
            store.save(project, preserve_queue=False)
        except FileNotFoundError:
            # Project dir vanished (deleted) between load() and save()
            # above -- same "nothing to run" outcome as ProjectNotFound.
            return
        kind = item["kind"]
        raw_payload = item["payload"]

    # Runners registered under a wildcard prefix (e.g. "reel_render:*") often
    # need the full kind to recover the dynamic suffix (the reel id, the
    # stage name...) when the caller didn't also duplicate it into payload
    # (e.g. a bare {"kind": "stage:ingest"} via the generic POST .../queue
    # endpoint) -- make it available without forcing every runner to take a
    # `kind` argument.
    payload = dict(raw_payload)
    payload.setdefault("_kind", kind)
    runner = _resolve_runner(kind)

    def task(log, project=project, payload=payload):
        if runner is None:
            raise RuntimeError(f"no runner registered for queue kind '{kind}'")
        runner(log, project, payload)

    def on_start(job_id: str) -> None:
        # Update BOTH the persisted copy (_mark_item, via its own fresh
        # store.load()) AND this exact in-memory `project` object -- the
        # same one `task`/the runner above holds and will call
        # store.save(project) against, likely many times, over the life of
        # a long stage. Persisting job_id only through _mark_item's fresh
        # load-mutate-save was silently losing it: the runner's very next
        # internal store.save(project) would overwrite project.json with
        # its own (older, job_id-less) in-memory "queue" list, reverting
        # job_id back to null for the rest of the run. Reproduced live: a
        # real run-all against project c7642fc7755e stayed job_id=null in
        # /api/projects/{pid}/queue for its entire multi-minute execution
        # even though the takes stage's Ollama calls were genuinely running.
        with _state_lock:
            for q_item in project.get("queue", []):
                if q_item["id"] == item_id:
                    q_item["job_id"] = job_id
                    break
        _mark_item(pid, item_id, job_id=job_id)

    job = jobs.run_sync(f"queue:{kind}:{pid}", task, lock_key=None, on_start=on_start)

    _mark_item(
        pid,
        item_id,
        status=job["status"],
        progress=job.get("progress", 0.0),
        error=job.get("error"),
        job_id=job["id"],
        finished=True,
    )


def _mark_item(
    pid: str,
    item_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    error: str | None = None,
    job_id: str | None = None,
    finished: bool = False,
) -> None:
    with _state_lock:
        try:
            project = store.load(pid)
        except store.ProjectNotFound:
            # Project was deleted while this item was running (or in the
            # window between it finishing and this bookkeeping call) --
            # nothing to persist. Critically must NOT raise: this is the
            # exact call that, unguarded, escaped a SECOND time from inside
            # _worker_loop's except-handler and killed the sole worker
            # thread forever (finding 1's root cause).
            return
        q = project.setdefault("queue", [])
        item = next((i for i in q if i["id"] == item_id), None)
        if item is None:
            return
        if status is not None:
            item["status"] = status
        if progress is not None:
            item["progress"] = progress
        if error is not None:
            item["error"] = error
        if job_id is not None:
            item["job_id"] = job_id
        try:
            store.save(project, preserve_queue=False)
        except FileNotFoundError:
            # Same "project vanished mid-flight" case, just caught at the
            # save() step instead of load() -- still a no-op, still must
            # not raise.
            return
        if finished:
            # Runs while still holding _state_lock: _run_auto_enqueue_hooks ->
            # enqueue() re-enters this same RLock from this same thread, and
            # must observe the item/project state just saved above.
            _run_auto_enqueue_hooks(pid, project, item)


def _run_auto_enqueue_hooks(pid: str, project: dict, item: dict) -> None:
    """Auto-enqueue rules (spec "Auto-enqueue rules"): completing run-all
    enqueues thumbs + reel_render for the top 5 reels. thumbs/reel_render
    kinds are fine to enqueue even before their runners exist -- an
    unregistered kind just errors that one item (see _resolve_runner).

    Reel previews (spec v7.14): right after the reels pipeline stage
    completes -- whether as the last step of a run-all or as a standalone
    "stage:reels" re-run -- auto-enqueue "reel_previews" so every reel
    suggestion gets its cheap low-res 9:16 preview render without the user
    having to ask for it, same spirit as thumbs/proxies auto-enqueuing after
    ingest. Dedupe on kind means a "reel_previews" already pending (e.g.
    queued a moment earlier by a reel edit, see api/reels.py) just gets
    reused rather than duplicated."""
    if item["kind"] == "run-all" and item["status"] == "done":
        enqueue(pid, "thumbs", {}, dedupe=True)
        top5 = sorted(project.get("reels", []), key=lambda r: r.get("rank", 999))[:5]
        for reel in top5:
            enqueue(pid, f"reel_render:{reel['id']}", {"reel_id": reel["id"]}, dedupe=True)
        if project.get("reels"):
            enqueue(pid, "reel_previews", {}, dedupe=True)
    elif item["kind"] == "stage:reels" and item["status"] == "done" and project.get("reels"):
        enqueue(pid, "reel_previews", {}, dedupe=True)
