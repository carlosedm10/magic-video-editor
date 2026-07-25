#!/usr/bin/env python3
"""Unit tests for the v6 auto-update feature (magic_video_editor/updater.py +
magic_video_editor/api/updater.py).

No pytest in this project's dependency set (see pyproject.toml) -- stdlib
unittest + FastAPI's TestClient (already a transitive dep via starlette),
same "no build step" spirit as scripts/eval_takes.py. Everything here is a
pure/mocked unit test: no real network call, no real GitHub release, no real
file swap.

Usage:
    uv run python scripts/test_updater.py
    uv run python scripts/test_updater.py -v
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import updater  # noqa: E402
from magic_video_editor.server import app  # noqa: E402


class SemverCompareTests(unittest.TestCase):
    def test_parse_basic(self):
        self.assertEqual(updater.parse_semver("1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_semver("V0.4.0"), (0, 4, 0))

    def test_parse_short_and_suffixed(self):
        self.assertEqual(updater.parse_semver("1.2"), (1, 2, 0))
        self.assertEqual(updater.parse_semver("2"), (2, 0, 0))
        self.assertEqual(updater.parse_semver("1.2.3-rc1"), (1, 2, 3))
        self.assertEqual(updater.parse_semver("1.2.3+build5"), (1, 2, 3))

    def test_parse_garbage_raises(self):
        with self.assertRaises(ValueError):
            updater.parse_semver("not-a-version")
        with self.assertRaises(ValueError):
            updater.parse_semver("")

    def test_gt_newer_patch(self):
        self.assertTrue(updater.semver_gt("0.4.1", "0.4.0"))
        self.assertFalse(updater.semver_gt("0.4.0", "0.4.1"))

    def test_gt_newer_minor_major(self):
        self.assertTrue(updater.semver_gt("0.5.0", "0.4.9"))
        self.assertTrue(updater.semver_gt("1.0.0", "0.9.9"))

    def test_gt_equal_is_false(self):
        self.assertFalse(updater.semver_gt("0.4.0", "0.4.0"))
        self.assertFalse(updater.semver_gt("v0.4.0", "0.4.0"))

    def test_gt_v_prefix_mixed(self):
        self.assertTrue(updater.semver_gt("v1.2.3", "1.2.2"))

    def test_gt_unparsable_is_fail_closed(self):
        # A weird/garbage release tag must never falsely offer an update.
        self.assertFalse(updater.semver_gt("garbage", "0.4.0"))
        self.assertFalse(updater.semver_gt("0.4.0", "garbage"))
        self.assertFalse(updater.semver_gt("garbage", "garbage"))


class Sha256VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file_path = Path(self.tmp.name) / "Magic Video Editor.dmg"
        self.file_path.write_bytes(b"fake dmg bytes for the sha256 fixture" * 100)
        self.digest = hashlib.sha256(self.file_path.read_bytes()).hexdigest()

    def test_verify_ok_shasum_format(self):
        # Real `shasum -a 256` sidecar format: "<hex>  <filename>\n"
        sidecar = f"{self.digest}  Magic Video Editor.dmg\n"
        self.assertTrue(updater.verify_sha256(self.file_path, sidecar))

    def test_verify_ok_hex_only(self):
        self.assertTrue(updater.verify_sha256(self.file_path, self.digest))

    def test_verify_ok_case_insensitive(self):
        self.assertTrue(updater.verify_sha256(self.file_path, self.digest.upper()))

    def test_verify_rejects_wrong_hash(self):
        wrong = "0" * 64
        self.assertFalse(updater.verify_sha256(self.file_path, wrong))

    def test_verify_rejects_tampered_file(self):
        sidecar = self.digest
        self.file_path.write_bytes(b"tampered contents")
        self.assertFalse(updater.verify_sha256(self.file_path, sidecar))


def _fake_release_response(tag: str, assets: list[dict] | None = None, html_url: str = "https://x"):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json.return_value = {
        "tag_name": tag,
        "html_url": html_url,
        "assets": assets or [],
    }
    return resp


class UpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Isolate module-level state between tests.
        self._orig_status = updater.get_status()
        self.addCleanup(lambda: updater._status.update(self._orig_status))

    def test_status_endpoint_returns_current_state(self):
        res = self.client.get("/api/update")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("checked", body)
        self.assertIn("current_version", body)

    @mock.patch("magic_video_editor.updater.httpx.get")
    def test_check_reports_update_available(self, mock_get):
        assets = [
            {"name": "Magic Video Editor.dmg", "browser_download_url": "https://dl/app.dmg"},
            {"name": "Magic Video Editor.dmg.sha256", "browser_download_url": "https://dl/app.dmg.sha256"},
        ]
        mock_get.return_value = _fake_release_response("v99.0.0", assets)

        res = self.client.post("/api/update/check")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["latest_version"], "99.0.0")
        self.assertEqual(body["dmg_url"], "https://dl/app.dmg")
        self.assertEqual(body["sha256_url"], "https://dl/app.dmg.sha256")
        self.assertIsNone(body["error"])

    @mock.patch("magic_video_editor.updater.httpx.get")
    def test_check_reports_up_to_date(self, mock_get):
        from magic_video_editor import __version__

        mock_get.return_value = _fake_release_response(f"v{__version__}")

        res = self.client.post("/api/update/check")
        body = res.json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["error"])

    @mock.patch("magic_video_editor.updater.httpx.get")
    def test_check_is_fail_silent_on_network_error(self, mock_get):
        mock_get.side_effect = RuntimeError("network exploded")

        res = self.client.post("/api/update/check")
        self.assertEqual(res.status_code, 200)  # never surfaces as an HTTP error
        body = res.json()
        self.assertTrue(body["checked"])
        self.assertFalse(body["available"])
        self.assertIn("network exploded", body["error"])

    @mock.patch("magic_video_editor.updater.running_from_app_bundle", return_value=None)
    def test_install_refuses_in_dev_mode(self, _mock_bundle):
        # Even with an update "available", dev mode must refuse up front,
        # before any download attempt.
        updater._status.update(
            {
                "checked": True,
                "available": True,
                "latest_version": "99.0.0",
                "dmg_url": "https://dl/app.dmg",
                "sha256_url": "https://dl/app.dmg.sha256",
                "error": None,
            }
        )
        res = self.client.post("/api/update/install")
        self.assertEqual(res.status_code, 400)
        self.assertIn("dev mode", res.json()["detail"])
        self.assertIn("git pull", res.json()["detail"])

    @mock.patch("magic_video_editor.updater.running_from_app_bundle")
    def test_install_refuses_when_no_update_available(self, mock_bundle):
        mock_bundle.return_value = Path("/Applications/Magic Video Editor.app")
        updater._status.update(
            {
                "checked": True,
                "available": False,
                "dmg_url": None,
                "sha256_url": None,
                "error": None,
            }
        )
        res = self.client.post("/api/update/install")
        self.assertEqual(res.status_code, 409)

    @mock.patch("magic_video_editor.jobs.start", return_value="job123")
    @mock.patch("magic_video_editor.updater.running_from_app_bundle")
    def test_install_starts_job_when_bundled_and_available(self, mock_bundle, mock_start):
        mock_bundle.return_value = Path("/Applications/Magic Video Editor.app")
        updater._status.update(
            {
                "checked": True,
                "available": True,
                "latest_version": "99.0.0",
                "dmg_url": "https://dl/app.dmg",
                "sha256_url": "https://dl/app.dmg.sha256",
                "error": None,
            }
        )
        res = self.client.post("/api/update/install")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["job_id"], "job123")
        mock_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
