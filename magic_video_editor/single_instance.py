"""Single-instance guard: stop a second Magic Video Editor process from
binding config.HOST/config.PORT and touching the same config.DATA_DIR at the
same time (port-bind errors, duplicate ollama spawns, project.json races).

Two signals, cheapest-reliable first:

1. A lockfile under config.DATA_DIR (`mve.lock`) held via fcntl.flock. This
   is the primary check: it's a single local syscall, no network round
   trip, and it is inherently crash-safe on a real (local) filesystem --
   flock is owned by the process's open file descriptor, and the kernel
   releases it the instant that fd closes, including on SIGKILL/crash. So
   a "stale lock" left behind by a dead process isn't actually stale at the
   OS level: the next acquire() attempt just succeeds. We still stamp our
   pid into the file purely for human debugging (`cat mve.lock`).

   The one place flock can lie is a network volume (NFS/SMB), where
   locking is notoriously unreliable -- it can silently no-op (every
   process "acquires" the lock) or the flock() call itself can raise
   something other than "already locked". We treat that distinctly
   (`LockUnavailable`) so the caller can fall back to signal 2 instead of
   trusting a lock that may not mean anything here.

2. A health probe: GET http://{host}:{port}/api/health and check the
   response actually identifies itself as this app (`name == "Magic Video
   Editor"`), not just "something is listening on this port". Slightly
   more expensive (a real HTTP round trip) but doesn't depend on the
   filesystem at all -- the fallback of choice when the lock is unusable.

app.py (packaged window entry) and server.py (`mve-server` dev entry) both
call `acquire_singleton()` at startup and `release_singleton()` on their
existing shutdown hooks.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = "mve.lock"


class LockUnavailable(RuntimeError):
    """flock() itself failed in a way that isn't "someone else holds it" --
    e.g. DATA_DIR lives on a filesystem where flock is unsupported or
    unreliable. Callers should fall back to another detection signal
    (probe_existing_instance) rather than trust this lock either way."""


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check via signal 0 (no-op, just existence).
    Not currently required for correctness (see module docstring -- flock
    already self-cleans on process death) but kept as a cheap debugging aid
    and a second line of defense if the pid stamp is ever consulted
    directly."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours -- still alive
    except OSError:
        return False
    return True


class SingleInstanceLock:
    """Holds one flock'd file handle for the lifetime of this process.

    Two `SingleInstanceLock` objects pointed at the same path race for the
    same OS-level flock -- that's what makes "acquire twice" (whether from
    two real processes or two objects in a test within one process, each
    with its own fd) correctly block on the second attempt."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns True if this object now owns it,
        False if a live holder already has it. Raises LockUnavailable if
        flock can't be trusted on this filesystem."""
        if self._fh is not None:
            return True  # already held by this object

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False  # held by another live process
            raise LockUnavailable(f"flock unavailable on {self.lock_path}: {e}") from e

        # Lock acquired -- stamp our pid. Best-effort only: the flock is
        # what actually enforces exclusivity, this is just for `cat
        # mve.lock` debugging.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            pass
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


_singleton_guard = threading.Lock()
_instance: SingleInstanceLock | None = None


def acquire_singleton(lock_path: Path) -> SingleInstanceLock | None:
    """Acquire the ONE lock for this process. Returns the held lock, or
    None if another live process already holds it. Raises LockUnavailable
    if the lock mechanism itself can't be trusted here (caller should fall
    back to probe_existing_instance)."""
    global _instance
    with _singleton_guard:
        if _instance is not None:
            return _instance  # already acquired earlier in this process
        lock = SingleInstanceLock(lock_path)
        acquired = lock.acquire()  # may raise LockUnavailable -- let it propagate
        if not acquired:
            return None
        _instance = lock
        return lock


def release_singleton() -> None:
    """Release this process's lock, if held. Safe to call more than once
    (e.g. once from a window-close handler and again from atexit) and safe
    to call even if the lock was never acquired."""
    global _instance
    with _singleton_guard:
        if _instance is not None:
            _instance.release()
            _instance = None


def probe_existing_instance(host: str, port: int, timeout: float = 1.0) -> bool:
    """GET /api/health on host:port and check it identifies as THIS app.
    Used as the network-volume fallback when the lockfile can't be
    trusted, and as a defense-in-depth double-check even when it can be.
    False on any error (nothing listening, wrong app, timeout, ...) --
    never raises, since "can't reach it" just means "no other instance"."""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (OSError, urllib.error.URLError, ValueError):
        return False
    return data.get("name") == "Magic Video Editor"


def detect_existing_instance(data_dir: Path, host: str, port: int) -> bool:
    """True if another Magic Video Editor instance is already running
    against `data_dir`/`host`:`port`. Tries the lockfile first (cheap,
    crash-safe); only falls back to the health probe if the lock mechanism
    itself is unusable on this filesystem (see LockUnavailable)."""
    lock_path = Path(data_dir) / LOCK_FILENAME
    try:
        lock = acquire_singleton(lock_path)
    except LockUnavailable:
        logger.warning(
            "flock unavailable on %s (network volume?) -- falling back to health probe",
            lock_path,
        )
        return probe_existing_instance(host, port)
    return lock is None


def focus_existing_instance() -> bool:
    """Best-effort: bring an already-running Magic Video Editor window to
    the front on macOS. Returns True if we believe it worked, False
    otherwise (never raises) -- callers should still log a clear message
    either way so the user isn't left thinking nothing happened."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["open", "-a", "Magic Video Editor"],
            check=True,
            timeout=3,
            capture_output=True,
        )
        return True
    except Exception:
        return False
