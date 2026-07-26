#!/usr/bin/env python3
"""Verification for the Ollama-robustness hardening pass (Package C):

  magic_video_editor/ollama_manager.py  -- findings #5 / #16
  magic_video_editor/api/ollama.py      -- finding #10
  magic_video_editor/agents/agents.py   -- finding #8

Root causes fixed:
  #5/#16  ensure_ollama()'s self-provisioning download used
          `client.stream(..., timeout=None)` -- a stalled transfer wedged
          the whole readiness pipeline forever. The GitHub release lookup
          was a single unauthenticated call with no retry/fallback, so a
          rate-limit or DNS blip permanently killed the self-provisioning
          path for the session (the ensure_ollama_async() latch never
          re-arms on its own).
  #10     _run_pull's httpx.stream POST /api/pull used timeout=None -- a
          stalled pull hung the job (and the Install button) forever.
  #8      agents.agents._model() built OllamaProvider with no explicit,
          bounded http client -- one hung generation could block the
          single global queue worker indefinitely.

Every test here runs entirely against local, in-process fakes:
  - "GitHub"/"ollama.com"/the pull endpoint are never contacted for real;
    httpx.get is mocked or pointed at a local stalling socket server
    bound to 127.0.0.1 (ephemeral port, never touches the real network).
  - A stalled transfer is simulated with a REAL local TCP listener that
    accepts the connection and then never writes a response, so the
    configured httpx timeout fires for real (proving the timeout is
    actually wired in, not just present as an unused kwarg) -- bounded to
    well under a second per test via patched-down timeout constants.
  - MVE_DATA is pointed at a scratch tempdir before any magic_video_editor
    import, per project convention (scripts/test_reel_previews.py).

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_ollama_manager.py / test_ollama_preflight.py.

Usage:
    uv run python scripts/test_ollama_robustness.py
    uv run python scripts/test_ollama_robustness.py -v
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_ollama_robustness_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

import httpx  # noqa: E402

from magic_video_editor import config  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

from magic_video_editor import ollama_manager  # noqa: E402
from magic_video_editor.agents import agents  # noqa: E402
from magic_video_editor.api import ollama as ollama_api  # noqa: E402

# Generous outer bound for any single blocking call in this file -- every
# timeout constant under test is patched down to a few hundred ms first,
# so a healthy fix finishes in well under a second; this is just the
# "something is very wrong" ceiling, comfortably inside the project's
# ~30s bounded-wait rule.
_MAX_WAIT_S = 5.0


class _FakeLog:
    """Minimal stand-in for jobs.JobLog -- collects messages, no-op
    progress, never cancels. Same shape used by scripts/test_reel_previews.py
    for direct (non-queue) calls into job bodies."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    def progress(self, frac: float) -> None:
        pass


class _StallingServer:
    """A real local TCP listener (127.0.0.1, ephemeral port) that accepts
    connections and then simply never writes a response. Lets a test
    exercise a REAL httpx timeout firing (connect succeeds instantly;
    read/write never gets a byte back) without any real network egress
    and without the test itself sleeping for the stall -- httpx raises as
    soon as its own (patched-down) timeout elapses."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._conns: list[socket.socket] = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
                self._conns.append(conn)  # accepted, held open, never answered
            except TimeoutError:
                continue
            except OSError:
                break

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(timeout=_MAX_WAIT_S)


# --------------------------------------------------------------------------
# #5/#16 -- self-provisioning download: bounded idle timeout
# --------------------------------------------------------------------------


class DownloadStallTests(unittest.TestCase):
    def setUp(self):
        self.server = _StallingServer()
        self.addCleanup(self.server.stop)

    def test_download_timeout_is_bounded_not_none(self):
        """The exact bug: timeout=None on the download stream. Assert the
        Timeout object actually built has finite read/write/connect values
        -- never None anywhere."""
        t = httpx.Timeout(
            connect=ollama_manager._DOWNLOAD_CONNECT_TIMEOUT_S,
            read=ollama_manager._DOWNLOAD_IDLE_TIMEOUT_S,
            write=ollama_manager._DOWNLOAD_IDLE_TIMEOUT_S,
            pool=ollama_manager._DOWNLOAD_CONNECT_TIMEOUT_S,
        )
        for value in (t.connect, t.read, t.write, t.pool):
            self.assertIsNotNone(value, "download timeout must be bounded, not None")

    def test_stalled_download_raises_promptly_instead_of_hanging(self):
        """Point the download straight at the stalling server (bypassing
        the GitHub lookup entirely) with the idle timeout patched down to
        a few hundred ms, and confirm the call raises for real, quickly --
        never a hang."""
        with (
            mock.patch.object(ollama_manager, "_DOWNLOAD_CONNECT_TIMEOUT_S", 0.5),
            mock.patch.object(ollama_manager, "_DOWNLOAD_IDLE_TIMEOUT_S", 0.3),
            mock.patch.object(
                ollama_manager,
                "_lookup_latest_release",
                return_value=("v0.0.0-test", f"{self.server.url}/ollama-darwin.tgz", None),
            ),
        ):
            log = _FakeLog()
            t0 = time.monotonic()
            with self.assertRaises(Exception) as ctx:
                ollama_manager._download_ollama_binary(log)
            elapsed = time.monotonic() - t0

        self.assertLess(
            elapsed, _MAX_WAIT_S, "stalled download did not fail within the bounded timeout"
        )
        # httpx.ReadTimeout (a ConnectTimeout/ReadError subclass of
        # httpx.TimeoutException) is what a real idle stall raises.
        self.assertIsInstance(ctx.exception, httpx.TimeoutException)


# --------------------------------------------------------------------------
# #16 -- GitHub release lookup: retry + pinned fallback, session stays
# recoverable
# --------------------------------------------------------------------------


class GithubLookupFallbackTests(unittest.TestCase):
    def test_failed_lookup_retries_then_falls_back_to_pinned_tag(self):
        calls = {"n": 0}

        def _always_fails(*args, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("simulated DNS/network failure")

        with (
            mock.patch.object(ollama_manager.httpx, "get", side_effect=_always_fails),
            mock.patch.object(ollama_manager, "_GITHUB_API_BACKOFF_S", 0.01),
        ):
            log = _FakeLog()
            t0 = time.monotonic()
            tag, asset_url, checksums_url = ollama_manager._lookup_latest_release(log)
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, _MAX_WAIT_S)
        self.assertEqual(calls["n"], ollama_manager._GITHUB_API_RETRIES, "expected retries")
        self.assertEqual(tag, ollama_manager._OLLAMA_FALLBACK_TAG)
        self.assertIn(ollama_manager._OLLAMA_FALLBACK_TAG, asset_url)
        self.assertIn(ollama_manager._DOWNLOAD_ASSET_NAME, asset_url)
        self.assertIn(ollama_manager._OLLAMA_FALLBACK_TAG, checksums_url)
        self.assertTrue(any("pinned" in line for line in log.lines))

    def test_lookup_succeeds_on_a_later_attempt_without_falling_back(self):
        responses = [httpx.ConnectError("first attempt fails")]

        def _flaky(*args, **kwargs):
            if responses:
                raise responses.pop()
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.json.return_value = {
                "tag_name": "v9.9.9",
                "assets": [
                    {"name": "ollama-darwin.tgz", "browser_download_url": "https://x/tgz"},
                    {"name": "sha256sum.txt", "browser_download_url": "https://x/sums"},
                ],
            }
            return resp

        with (
            mock.patch.object(ollama_manager.httpx, "get", side_effect=_flaky),
            mock.patch.object(ollama_manager, "_GITHUB_API_BACKOFF_S", 0.01),
        ):
            tag, asset_url, checksums_url = ollama_manager._lookup_latest_release(_FakeLog())

        self.assertEqual(tag, "v9.9.9")
        self.assertEqual(asset_url, "https://x/tgz")
        self.assertEqual(checksums_url, "https://x/sums")


# --------------------------------------------------------------------------
# #16 (c) -- ensure_ollama() never leaves a half-state; retry is not
# terminal
# --------------------------------------------------------------------------


class EnsureOllamaRecoveryTests(unittest.TestCase):
    def setUp(self):
        ollama_manager.terminate()
        # Force past the system-binary path regardless of whether this
        # machine happens to have a system `ollama` install (e.g. a
        # Homebrew-installed Ollama.app on /usr/local/bin/ollama) -- these
        # tests are specifically about the bundled/download/retry state
        # machine, and must never spawn a real system `ollama serve`.
        self._system_binary_patch = mock.patch.object(
            ollama_manager, "_system_binary_path", return_value=None
        )
        self._system_binary_patch.start()
        self.addCleanup(self._system_binary_patch.stop)

    def tearDown(self):
        ollama_manager.terminate()

    def test_total_failure_settles_on_unreachable_not_half_state(self):
        with (
            mock.patch.object(ollama_manager, "_reachable", return_value=False),
            mock.patch.object(ollama_manager, "bundled_binary_path", return_value=None),
            mock.patch.object(ollama_manager, "_download_and_spawn", return_value=None),
        ):
            mode = ollama_manager.ensure_ollama()
        self.assertEqual(mode, "unreachable")
        self.assertIsNone(ollama_manager._proc)

    def test_unexpected_exception_still_settles_on_unreachable(self):
        """Defensive net: even a surprise exception deep in the state
        machine must not leave _mode stuck at e.g. "downloading"."""
        with (
            mock.patch.object(ollama_manager, "_reachable", return_value=False),
            mock.patch.object(
                ollama_manager, "bundled_binary_path", side_effect=RuntimeError("boom")
            ),
        ):
            mode = ollama_manager.ensure_ollama()
        self.assertEqual(mode, "unreachable")

    def test_transient_failure_is_not_terminal_for_the_session(self):
        """The actual field bug: a failed attempt used to permanently
        latch ensure_ollama_async() -- nothing would ever probe again.
        retry_ensure_ollama() must get a real, fresh probe to run, and
        that probe must be able to succeed."""
        with (
            mock.patch.object(ollama_manager, "_reachable", return_value=False),
            mock.patch.object(ollama_manager, "bundled_binary_path", return_value=None),
            mock.patch.object(ollama_manager, "_download_and_spawn", return_value=None),
        ):
            ollama_manager.ensure_ollama_async()
            deadline = time.monotonic() + _MAX_WAIT_S
            while ollama_manager.current_mode() == "starting" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(ollama_manager.current_mode(), "unreachable")

            # A second ensure_ollama_async() call is normally a no-op --
            # confirm that WITHOUT retry, the stuck state really would
            # persist (documents the bug being fixed).
            ollama_manager.ensure_ollama_async()
            time.sleep(0.1)
            self.assertEqual(ollama_manager.current_mode(), "unreachable")

        # Now the transient condition clears (system Ollama becomes
        # reachable) and we retry -- this must NOT be a no-op.
        with mock.patch.object(ollama_manager, "_reachable", return_value=True):
            ollama_manager.retry_ensure_ollama()
            deadline = time.monotonic() + _MAX_WAIT_S
            while ollama_manager.current_mode() in ("starting", "unreachable"):
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            self.assertEqual(
                ollama_manager.current_mode(),
                "system",
                "retry_ensure_ollama() should let a cleared-up transient failure recover",
            )


# --------------------------------------------------------------------------
# #10 -- model pull stream: bounded timeout, error reaches the job
# --------------------------------------------------------------------------


class PullStallTests(unittest.TestCase):
    def setUp(self):
        self.server = _StallingServer()
        self.addCleanup(self.server.stop)
        self._orig_url = config.OLLAMA_URL
        config.OLLAMA_URL = self.server.url
        self.addCleanup(self._restore_url)

    def _restore_url(self):
        config.OLLAMA_URL = self._orig_url

    def test_pull_timeout_is_bounded_not_none(self):
        t = ollama_api._PULL_TIMEOUT
        for value in (t.connect, t.read, t.write, t.pool):
            self.assertIsNotNone(value, "pull timeout must be bounded, not None")

    def test_stalled_pull_raises_promptly_and_reaches_the_job(self):
        tiny_timeout = httpx.Timeout(connect=0.5, read=0.3, write=0.3, pool=0.5)
        with mock.patch.object(ollama_api, "_PULL_TIMEOUT", tiny_timeout):
            log = _FakeLog()
            t0 = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                ollama_api._run_pull(log, "qwen2.5:14b")
            elapsed = time.monotonic() - t0

        self.assertLess(
            elapsed, _MAX_WAIT_S, "stalled pull did not fail within the bounded timeout"
        )
        self.assertIn("pull failed", str(ctx.exception))

    def test_stalled_pull_surfaces_as_a_job_error_not_a_hang(self):
        """End-to-end through jobs.py, the way the /api/ollama/pull route
        actually runs it -- the Install button's job must land on
        status="error" with a message, never stay "running" forever."""
        from magic_video_editor import jobs as jobs_module

        tiny_timeout = httpx.Timeout(connect=0.5, read=0.3, write=0.3, pool=0.5)
        with mock.patch.object(ollama_api, "_PULL_TIMEOUT", tiny_timeout):
            t0 = time.monotonic()
            job = jobs_module.run_sync(
                "ollama_pull_test", ollama_api._run_pull, "qwen2.5:14b", lock_key=None
            )
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, _MAX_WAIT_S)
        self.assertEqual(job["status"], "error")
        self.assertIsNotNone(job["error"])


# --------------------------------------------------------------------------
# #8 -- per-agent-call timeout: bounded, overridable, sane under retries=2
# --------------------------------------------------------------------------


class AgentGenerationTimeoutTests(unittest.TestCase):
    def test_model_http_client_has_bounded_non_none_timeout(self):
        model = agents._model("qwen2.5:14b")
        real_client = model.client._client  # AsyncOpenAI -> underlying httpx.AsyncClient
        timeout = real_client.timeout
        for value in (timeout.connect, timeout.read, timeout.write, timeout.pool):
            self.assertIsNotNone(value, "agent generation timeout must be bounded, not None")
        self.assertEqual(timeout.read, agents._OLLAMA_GENERATE_TIMEOUT_S)
        self.assertEqual(timeout.connect, agents._OLLAMA_CONNECT_TIMEOUT_S)

    def test_timeout_is_overridable_and_retries_stay_sane(self):
        # Simulate an env override having been read at import time by
        # patching the module constants directly (what they're built
        # from) and confirming _model() actually uses them -- proves the
        # value isn't hardcoded deeper in the call.
        with (
            mock.patch.object(agents, "_OLLAMA_GENERATE_TIMEOUT_S", 42),
            mock.patch.object(agents, "_OLLAMA_CONNECT_TIMEOUT_S", 3),
        ):
            model = agents._model("llama3.2:3b")
            timeout = model.client._client.timeout
        self.assertEqual(timeout.read, 42)
        self.assertEqual(timeout.connect, 3)

        # retries=2 (3 attempts total) must not multiply the configured
        # timeout into an effectively unbounded wait: 3x the default
        # generate timeout should still be a bounded number of minutes,
        # not hours/None.
        worst_case_s = agents._OLLAMA_GENERATE_TIMEOUT_S * 3
        self.assertLess(
            worst_case_s,
            30 * 60,
            "3x the per-call timeout (retries=2) has grown unreasonably large",
        )

    def test_get_agent_builds_with_bounded_client(self):
        agent = agents.get_agent("take_judge")
        timeout = agent.model.client._client.timeout
        self.assertIsNotNone(timeout.read)
        self.assertIsNotNone(timeout.connect)


if __name__ == "__main__":
    unittest.main()
