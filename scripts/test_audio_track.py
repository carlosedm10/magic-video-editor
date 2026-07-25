#!/usr/bin/env python3
"""End-to-end test for the MAIN AUDIO TRACK feature (spec vNext "Main audio
track" — music bed with auto-ducking).

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor) with tiny synthetic
ffmpeg-generated media (testsrc video + sine tones — never real user files).
Covers:

  1. Importing a music file (.wav) via POST /api/projects/{pid}/audio-assets
     lands it in project["audio_assets"] — NOT project["clips"] — with a
     probed duration.
  2. project["clips"] is untouched by that import (structural leak-proofing:
     build_edl/ordering/takes all filter role=="camera" over project
     ["clips"]; an audio_assets entry was never appended there at all).
  3. PUT/GET/DELETE /api/projects/{pid}/audio-track persist {asset_id,
     start_s, gain_db, ducking}.
  4. ONE real end-to-end final render (pipeline.render.run) with the audio
     track set actually produces a video with an audio stream, and the
     music's own frequency band is audibly present (higher band energy than
     a baseline render without an audio track) — proving the mix landed,
     not just that ffmpeg didn't crash.
  5. The music loops/positions correctly regardless of its own (shorter)
     duration: the render doesn't fail and comes out at the program's
     duration, not the (shorter) music file's.
  6. pipeline.render._apply_music_bed's filtergraph toggles sidechaincompress
     on/off with audio_track["ducking"] (fast unit check, ffmpeg_utils.run
     monkeypatched to just capture the command instead of encoding again).

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_reel_previews.py. MVE_DATA must be set (to a fresh tmp dir)
BEFORE importing anything from magic_video_editor, since magic_video_editor.config reads
it at import time.

Usage:
    uv run python scripts/test_audio_track.py
    uv run python scripts/test_audio_track.py -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_audio_track_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, ffmpeg_utils, store  # noqa: E402
from magic_video_editor.pipeline import ingest, ordering, render  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

PROGRAM_DURATION = 4.0
PROGRAM_FREQ = 1000
MUSIC_DURATION = 2.0  # shorter than the program -- exercises the loop/trim path
MUSIC_FREQ = 300


class _Log:
    """Minimal stand-in for jobs.JobLog: collects messages, no-op progress,
    never cancels."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    def progress(self, frac: float) -> None:
        pass

    def stage(self, *a, **k) -> None:
        pass


def _make_synthetic_clip(dst: Path, duration: float, freq: int) -> None:
    """A tiny H.264 16:9 clip (testsrc + a pure sine tone) -- cheap, decodable,
    plenty for exercising the render path end to end."""
    cmd = [
        ffmpeg_utils.ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=640x360:rate=24:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_synthetic_music(dst: Path, duration: float, freq: int) -> None:
    """A tiny mono wav sine tone -- the "music bed" import fixture."""
    cmd = [
        ffmpeg_utils.ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:duration={duration}",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _band_mean_volume(path: Path, freq: int) -> float:
    """Mean volume (dBFS) of `path`'s audio after a tight bandpass around
    `freq`, via ffmpeg's volumedetect (writes to stderr regardless of -v
    level). A higher (less negative) value means more energy in that band --
    used comparatively (with vs without the music track) rather than against
    an absolute threshold, so it doesn't depend on exact ducking params."""
    cmd = [
        ffmpeg_utils.ffmpeg_bin(),
        "-i",
        str(path),
        "-af",
        f"bandpass=f={freq}:width_type=h:w=100,volumedetect",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, f"no mean_volume in ffmpeg output:\n{proc.stderr[-2000:]}"
    return float(m.group(1))


class AudioTrackE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.project = store.new_project("audio-track-e2e")
        cls.pid = cls.project["id"]

        src_dir = Path(tempfile.mkdtemp(prefix="mve_audio_track_src_"))
        cls.clip_path = src_dir / "clip.mp4"
        _make_synthetic_clip(cls.clip_path, PROGRAM_DURATION, PROGRAM_FREQ)
        cls.music_path = src_dir / "music.wav"
        _make_synthetic_music(cls.music_path, MUSIC_DURATION, MUSIC_FREQ)

        added = ingest.add_clips(cls.project, [str(cls.clip_path)])
        assert len(added) == 1, added
        cls.clip_id = added[0]["id"]

        log = _Log()
        ingest.run(log, cls.project)
        store.save(cls.project)

        clip = store.get_clip(cls.project, cls.clip_id)
        assert clip["info"]["has_video"] and clip["info"]["has_audio"], clip["info"]

        # Manual EDL (mirrors scripts/test_reel_previews.py's manual fixture
        # reel): skips transcribe/takes/ordering entirely -- this test is
        # about the audio_track mix, not the AI cut.
        cls.project["edl"] = [
            {
                "clip_id": cls.clip_id,
                "start": 0.3,
                "end": PROGRAM_DURATION - 0.2,
                "transition": {"type": "none", "duration": 0.5},
            }
        ]
        store.save(cls.project)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def test_01_import_audio_asset_via_api(self):
        r = self.client.post(
            f"/api/projects/{self.pid}/audio-assets", json={"paths": [str(self.music_path)]}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["added"], 1)
        assets = body["audio_assets"]
        self.assertEqual(len(assets), 1)
        asset = assets[0]
        self.assertEqual(asset["filename"], "music.wav")
        self.assertAlmostEqual(asset["duration"], MUSIC_DURATION, delta=0.2)
        type(self).asset_id = asset["id"]

    def test_02_audio_asset_never_leaks_into_camera_clip_pipeline(self):
        project = store.load(self.pid)
        clip_ids = {c["id"] for c in project["clips"]}
        self.assertNotIn(self.asset_id, clip_ids, "audio asset id must never appear in project.clips")
        self.assertEqual(len(project["clips"]), 1, "importing music must not add a clip")

        # build_edl/ordering's own camera filter (pipeline/ordering.py) --
        # exercised directly, over the real project, to prove the pipeline
        # structurally can't see audio_assets (they were never appended to
        # project["clips"] in the first place, so this is nearly tautological
        # by construction, but asserts it against the ACTUAL filter used).
        camera_ids = [c["id"] for c in project["clips"] if c["role"] == "camera" and c["id"]]
        self.assertNotIn(self.asset_id, camera_ids)
        self.assertEqual(camera_ids, [self.clip_id])

        # ordering.invalidate_after_clipset_change (called by add_clips on a
        # clip-set change) must not have been triggered by the audio import
        # -- the EDL we hand-built in setUpClass must survive untouched.
        self.assertEqual(project["edl"][0]["clip_id"], self.clip_id)

    def test_03_audio_track_crud(self):
        r = self.client.get(f"/api/projects/{self.pid}/audio-track")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["audio_track"])

        # Unknown asset id is rejected.
        r = self.client.put(
            f"/api/projects/{self.pid}/audio-track", json={"asset_id": "nope", "start_s": 0}
        )
        self.assertEqual(r.status_code, 400)

        r = self.client.put(
            f"/api/projects/{self.pid}/audio-track",
            json={"asset_id": self.asset_id, "start_s": 0.5, "gain_db": 0.0, "ducking": True},
        )
        self.assertEqual(r.status_code, 200, r.text)
        track = r.json()["audio_track"]
        self.assertEqual(track["asset_id"], self.asset_id)
        self.assertEqual(track["start_s"], 0.5)
        self.assertEqual(track["gain_db"], 0.0)
        self.assertTrue(track["ducking"])

        r = self.client.delete(f"/api/projects/{self.pid}/audio-track")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(store.load(self.pid).get("audio_track"))

        # Re-set it for the render tests below.
        r = self.client.put(
            f"/api/projects/{self.pid}/audio-track",
            json={"asset_id": self.asset_id, "start_s": 0.5, "gain_db": 0.0, "ducking": True},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_04_baseline_render_without_audio_track(self):
        """Render once with audio_track cleared -- the "no music" baseline
        the real (with-music) render below is compared against."""
        project = store.load(self.pid)
        project["audio_track"] = None
        store.save(project)
        log = _Log()
        render.run(log, project)
        reloaded = store.load(self.pid)
        self.assertTrue(reloaded["renders"], "expected a recorded render")
        type(self).baseline_path = Path(reloaded["renders"][-1]["path"])
        self.assertTrue(self.baseline_path.exists())

        # Re-arm the audio track for the real test below.
        reloaded["audio_track"] = {
            "asset_id": self.asset_id,
            "start_s": 0.5,
            "gain_db": 0.0,
            "ducking": True,
        }
        store.save(reloaded)

    def test_05_real_render_with_music_bed_has_audio_and_music_present(self):
        """The one real, tiny, end-to-end encode: pipeline.render.run with
        project["audio_track"] set."""
        project = store.load(self.pid)
        self.assertIsNotNone(project.get("audio_track"))
        log = _Log()
        render.run(log, project)
        reloaded = store.load(self.pid)
        out_path = Path(reloaded["renders"][-1]["path"])
        self.assertTrue(out_path.exists())
        self.assertNotEqual(str(out_path), str(self.baseline_path))

        info = ffmpeg_utils.clip_info(str(out_path))
        self.assertTrue(info["has_audio"], "final render must have an audio stream")
        # Program was trimmed to (PROGRAM_DURATION - 0.2 - 0.3) seconds; the
        # music (looped/trimmed regardless of its own shorter duration) must
        # never change the video's length ("-shortest" + atrim to program
        # length, not the music file's).
        expected_duration = PROGRAM_DURATION - 0.2 - 0.3
        self.assertAlmostEqual(info["duration"], expected_duration, delta=0.5)

        # The music's own 300Hz band must carry noticeably more energy than
        # in the no-audio-track baseline -- proof the mix actually landed
        # (not just "ffmpeg didn't error").
        baseline_band = _band_mean_volume(self.baseline_path, MUSIC_FREQ)
        mixed_band = _band_mean_volume(out_path, MUSIC_FREQ)
        self.assertGreater(
            mixed_band,
            baseline_band + 3.0,
            f"expected the music's {MUSIC_FREQ}Hz band to be louder with the audio "
            f"track set (baseline={baseline_band:.1f}dB, mixed={mixed_band:.1f}dB)",
        )

    def test_06_ducking_toggles_sidechaincompress_in_the_filtergraph(self):
        """Fast unit check (no second real encode): ffmpeg_utils.run is
        monkeypatched to just capture the command render._apply_music_bed
        builds, instead of actually encoding again."""
        project = store.load(self.pid)
        captured: list[list[str]] = []
        real_run = ffmpeg_utils.run

        def fake_run(cmd, heavy=False):
            captured.append(cmd)

        ffmpeg_utils.run = fake_run
        try:
            # in_path just needs clip_info() (real ffprobe) to succeed --
            # the baseline render from test_04 is a handy already-on-disk file.
            in_path = self.baseline_path
            out_path = in_path.with_name("music_bed_test_out.mp4")

            project["audio_track"]["ducking"] = True
            ok = render._apply_music_bed(_Log(), project, in_path, out_path)
            self.assertTrue(ok)
            self.assertIn("sidechaincompress", captured[-1][captured[-1].index("-filter_complex") + 1])

            project["audio_track"]["ducking"] = False
            ok = render._apply_music_bed(_Log(), project, in_path, out_path)
            self.assertTrue(ok)
            self.assertNotIn(
                "sidechaincompress", captured[-1][captured[-1].index("-filter_complex") + 1]
            )
        finally:
            ffmpeg_utils.run = real_run

    def test_07_missing_asset_and_past_end_start_s_are_safe_no_ops(self):
        project = store.load(self.pid)
        project["audio_track"] = {"asset_id": "does-not-exist", "start_s": 0, "ducking": True}
        ok = render._apply_music_bed(_Log(), project, self.baseline_path, self.baseline_path.with_suffix(".x.mp4"))
        self.assertFalse(ok)

        project["audio_track"] = {"asset_id": self.asset_id, "start_s": 9999.0, "ducking": True}
        ok = render._apply_music_bed(_Log(), project, self.baseline_path, self.baseline_path.with_suffix(".y.mp4"))
        self.assertFalse(ok)

    def test_08_ordering_module_never_imports_audio_assets_concept(self):
        """Cheap structural guard: pipeline/ordering.py (untouched, off-limits
        for this task) has no notion of audio_assets at all -- it only ever
        filters project["clips"] by role=="camera"."""
        import inspect

        src = inspect.getsource(ordering)
        self.assertNotIn("audio_assets", src)
        self.assertNotIn("audio_track", src)
        self.assertIn('role"] == "camera"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
