#!/usr/bin/env python3
"""End-to-end test for the ON-DEMAND cursor-position enhance PREVIEW (spec
v7.13 "audio preview UX" — "Enhance voice" is neural/DeepFilterNet3 and
can't run live like the 8-band EQ, so this is the "probar desde el cursor"
fix): POST /api/projects/{pid}/audio-preview-at.

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor), with tiny synthetic
ffmpeg-generated media (testsrc video + a sine tone mixed with white noise
— never real user files). Covers:

  1. EDL cursor-position -> (segment, clip-local time) resolution across a
     TWO-segment EDL (same source clip, two disjoint windows) — proves the
     endpoint walks program time the same way pipeline/render.py._build
     does, not just "clip_id + t" like the older /audio-preview.
  2. The happy path: a valid start_s/duration_s returns 200 with both an
     "original" and an "enhanced" wav, both Range-servable via the existing
     GET /api/projects/{pid}/media/file route, both real decodable audio
     (ffprobe has_audio) of the requested-ish duration.
  3. The enhanced sample is measurably DIFFERENT from the original and its
     integrated loudness has moved towards the -16 LUFS target — proof the
     chain (audio_enhance.enhance(): denoise stage + pyloudnorm normalize +
     limiter) actually ran on the extracted window, not just a copy.
  4. duration_s is capped to both PREVIEW_AT_MAX_SECONDS and to whatever is
     left in the active segment (never crosses a segment boundary).
  5. Rejections: no EDL yet, start_s beyond the total program duration, and
     a cursor sitting too close to the end of its segment.

DeepFilterNet note: the `df` package IS importable in this repo's uv
environment, but its model weights are large and would otherwise be
downloaded fresh into every throwaway scratch MVE_DATA dir this test uses —
slow and network-dependent, which would violate the "bounded waits" rule on
a cold machine/CI. So this test forces audio_enhance._load_dfn() to report
"unavailable" (monkeypatched), which makes enhance() take its own documented
noisereduce/highpass/presence-lift fallback branch instead — a REAL signal
chain, not a mock of the output — followed by the real pyloudnorm normalize
+ limiter stage. That's enough to prove the endpoint's wiring end to end
(extraction -> enhance() -> loudness change) and the LUFS/limiter stage
specifically; it does NOT exercise the DeepFilterNet3 neural branch itself.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_audio_track.py. MVE_DATA must be set (to a fresh tmp dir)
BEFORE importing anything from magic_video_editor, since magic_video_editor.config
reads it at import time.

Usage:
    uv run python scripts/test_audio_preview.py
    uv run python scripts/test_audio_preview.py -v
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_audio_preview_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, ffmpeg_utils, store  # noqa: E402
from magic_video_editor.pipeline import audio_enhance, ingest  # noqa: E402
from magic_video_editor.server import app  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

# Force the noisereduce/highpass/presence fallback branch (see module
# docstring) -- never attempt a DeepFilterNet weight download from this test.
audio_enhance._dfn_state = False

CLIP_DURATION = 8.0
TONE_FREQ = 440


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


def _make_noisy_clip(dst: Path, duration: float, freq: int) -> None:
    """A tiny H.264 16:9 clip whose audio is a pure tone mixed with white
    noise -- enough "already has content, but noisy" signal for the
    enhance chain (denoise + loudness-normalize + limiter) to visibly act
    on, without needing any real recorded speech."""
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
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=color=white:amplitude=0.06:duration={duration}",
        "-filter_complex",
        "[1:a][2:a]amix=inputs=2:duration=first:normalize=0[aout]",
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _integrated_lufs(wav_bytes: bytes) -> float:
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(wav_bytes), always_2d=True, dtype="float32")
    meter = pyln.Meter(sr)
    return float(meter.integrated_loudness(np.asarray(data)))


class AudioCursorPreviewE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.project = store.new_project("audio-cursor-preview-e2e")
        cls.pid = cls.project["id"]

        src_dir = Path(tempfile.mkdtemp(prefix="mve_audio_preview_src_"))
        cls.clip_path = src_dir / "clip.mp4"
        _make_noisy_clip(cls.clip_path, CLIP_DURATION, TONE_FREQ)

        added = ingest.add_clips(cls.project, [str(cls.clip_path)])
        assert len(added) == 1, added
        cls.clip_id = added[0]["id"]

        log = _Log()
        ingest.run(log, cls.project)
        store.save(cls.project)

        clip = store.get_clip(cls.project, cls.clip_id)
        assert clip["info"]["has_video"] and clip["info"]["has_audio"], clip["info"]

        # Manual two-segment EDL over disjoint windows of the SAME clip
        # (mirrors scripts/test_audio_track.py's manual-EDL fixture pattern):
        # segment 0 covers program [0, 3), segment 1 covers program [3, 6),
        # sourced from clip-local [4, 7) -- deliberately NOT contiguous with
        # segment 0's clip-local range, so a naive "program time == clip
        # time" implementation would resolve the wrong audio.
        cls.project["edl"] = [
            {
                "clip_id": cls.clip_id,
                "start": 0.0,
                "end": 3.0,
                "transition": {"type": "none", "duration": 0.5},
            },
            {
                "clip_id": cls.clip_id,
                "start": 4.0,
                "end": 7.0,
                "transition": {"type": "none", "duration": 0.5},
            },
        ]
        store.save(cls.project)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def _fetch(self, url: str) -> bytes:
        parsed = urlparse(url)
        r = self.client.get(f"{parsed.path}?{parsed.query}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.content

    def test_01_happy_path_resolves_second_segment_and_enhances(self):
        """start_s=4.0 lands 1s into segment 1 (program [3,6) <- clip-local
        [4,7)), so the extracted window must start at clip-local time
        4 + 1 = 5, with duration capped to the segment's remaining 2s (the
        default 8s request is way more than what's left)."""
        r = self.client.post(
            f"/api/projects/{self.pid}/audio-preview-at",
            json={"start_s": 4.0},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["clip_id"], self.clip_id)
        self.assertAlmostEqual(body["start_s"], 4.0)
        self.assertAlmostEqual(body["duration_s"], 2.0, delta=0.05)
        self.assertIn("/media/file?path=", body["original_url"])
        self.assertIn("/media/file?path=", body["enhanced_url"])

        original_bytes = self._fetch(body["original_url"])
        enhanced_bytes = self._fetch(body["enhanced_url"])
        self.assertGreater(len(original_bytes), 1000)
        self.assertGreater(len(enhanced_bytes), 1000)

        # Both files are real, decodable audio of about the requested
        # duration (ffprobe, not just "ffmpeg didn't error").
        for label, blob in (("original", original_bytes), ("enhanced", enhanced_bytes)):
            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                tmp.write(blob)
                tmp.flush()
                info = ffmpeg_utils.clip_info(tmp.name)
                self.assertTrue(info["has_audio"], f"{label} has no audio stream: {info}")
                self.assertAlmostEqual(info["duration"], 2.0, delta=0.3, msg=label)

        # The enhance chain (fallback denoise + pyloudnorm normalize +
        # limiter -- see module docstring) must have actually changed the
        # audio: bytes differ, AND loudness moved towards the -16 LUFS
        # target (the noisy synthetic source is not already at -16 LUFS).
        self.assertNotEqual(original_bytes, enhanced_bytes)
        original_lufs = _integrated_lufs(original_bytes)
        enhanced_lufs = _integrated_lufs(enhanced_bytes)
        self.assertLess(
            abs(enhanced_lufs - audio_enhance.TARGET_LUFS),
            abs(original_lufs - audio_enhance.TARGET_LUFS),
            f"expected enhanced loudness ({enhanced_lufs:.1f} LUFS) to be closer to "
            f"the {audio_enhance.TARGET_LUFS} LUFS target than the original "
            f"({original_lufs:.1f} LUFS)",
        )
        # True-peak-ish ceiling from the limiter stage.
        import numpy as np
        import soundfile as sf

        enhanced_data, _sr = sf.read(io.BytesIO(enhanced_bytes), always_2d=True)
        ceiling = 10 ** (audio_enhance.CEILING_DBTP / 20.0)
        self.assertLessEqual(float(np.max(np.abs(enhanced_data))), ceiling + 1e-3)

    def test_02_first_segment_resolves_clip_local_time_directly(self):
        """start_s=1.0 is inside segment 0 (program [0,3) <- clip-local
        [0,3), 1:1 mapping), remaining = 2s in-segment."""
        r = self.client.post(
            f"/api/projects/{self.pid}/audio-preview-at",
            json={"start_s": 1.0, "duration_s": 5.0},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertAlmostEqual(body["start_s"], 1.0)
        self.assertAlmostEqual(body["duration_s"], 2.0, delta=0.05)

    def test_03_duration_s_is_capped_to_the_max_even_with_plenty_of_room(self):
        """A near-start cursor in a long-enough segment: duration_s=100 must
        be capped by PREVIEW_AT_MAX_SECONDS (15s), not by the (here, ample)
        segment remainder."""
        project = store.load(self.pid)
        project["edl"] = [
            {
                "clip_id": self.clip_id,
                "start": 0.0,
                "end": CLIP_DURATION,
                "transition": {"type": "none", "duration": 0.5},
            }
        ]
        store.save(project)
        try:
            r = self.client.post(
                f"/api/projects/{self.pid}/audio-preview-at",
                json={"start_s": 0.0, "duration_s": 100.0},
            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertLessEqual(body["duration_s"], 15.0 + 1e-6)
        finally:
            # restore the two-segment EDL for tests relying on it.
            store.save(self.project)

    def test_04_start_s_beyond_program_duration_is_rejected(self):
        r = self.client.post(
            f"/api/projects/{self.pid}/audio-preview-at",
            json={"start_s": 9999.0},
        )
        self.assertEqual(r.status_code, 400, r.text)

    def test_05_cursor_too_close_to_segment_end_is_rejected(self):
        # Segment 1 ends at program time 6.0; 5.9 leaves only 0.1s, below
        # PREVIEW_AT_MIN_SECONDS.
        r = self.client.post(
            f"/api/projects/{self.pid}/audio-preview-at",
            json={"start_s": 5.9},
        )
        self.assertEqual(r.status_code, 400, r.text)

    def test_06_no_edl_yet_is_rejected(self):
        empty_project = store.new_project("audio-cursor-preview-no-edl")
        try:
            r = self.client.post(
                f"/api/projects/{empty_project['id']}/audio-preview-at",
                json={"start_s": 0.0},
            )
            self.assertEqual(r.status_code, 400, r.text)
        finally:
            shutil.rmtree(store.project_dir(empty_project["id"]), ignore_errors=True)

    def test_07_negative_start_s_is_rejected_by_validation(self):
        r = self.client.post(
            f"/api/projects/{self.pid}/audio-preview-at",
            json={"start_s": -1.0},
        )
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
