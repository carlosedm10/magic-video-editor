#!/usr/bin/env python3
"""Regression tests for roadmap #3 "manual editor without the pipeline":
the user must be able to import clips, build a cut on the timeline, and
export -- all WITHOUT running whisper/LLM/any pipeline stage.

Covers the fix in magic_video_editor/pipeline/ingest.py: `add_clips`
(the Files/Folder button + "add by path…" import path) used to create every
clip with `info: None` and never probe it -- only the "ingest" pipeline
STAGE (ingest.run()) populated clip.info.duration. Since
Editor.insertClip (ui/editor/state.js) early-returns when
`clip.info.duration` is missing/<=0, dragging a freshly Files/Folder-imported
clip onto the timeline silently no-op'd until at least one pipeline run --
"can't build the timeline without the pipeline". `_new_clip_record` now
probes at IMPORT time (mirrors register_uploaded_clips' sibling path and
add_audio_assets' existing probe-on-import), so a clip is immediately usable
by the manual editor with zero pipeline stages run.

Also covers:
  - PUT /api/projects/{pid}/edl accepting a manually-built EDL whose segment
    spans the clip's FULL duration [0, duration] with no sentences/clip_order
    ever having existed (empty-project start, spec #1/#2).
  - magic_video_editor.pipeline.render.run() producing a real output file from
    a purely manual project["edl"] -- no project["sentences"], no pipeline
    stage other than render itself.

Runs entirely against a SCRATCH MVE_DATA project dir (never the real
~/Library/Application Support/Magic Video Editor). Uses real (tiny, lavfi-
synthesized) ffmpeg clips and a real ffmpeg render -- this is the one seam
where a fake/placeholder file won't do, since the whole point is verifying
ffmpeg_utils.clip_info()'s probe and render.py's encode path. Skips itself
(rather than failing) if `ffmpeg`/`ffprobe` aren't on PATH.

No pytest in this project's dependency set -- stdlib unittest, same spirit
as scripts/test_edl_resilience.py.

Usage:
    uv run python scripts/test_manual_edit.py
    uv run python scripts/test_manual_edit.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_manual_edit_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from fastapi.testclient import TestClient  # noqa: E402

from magic_video_editor import config, ffmpeg_utils, store  # noqa: E402
from magic_video_editor.pipeline import ingest, render  # noqa: E402
from magic_video_editor.server import app  # noqa: E402


class _FakeLog:
    """render.py (and jobs.py's real JobLog) callers use `log(msg)` for lines
    and `log.progress(frac)` for progress -- a plain lambda has no
    `.progress` attribute, so this stands in for a real job's JobLog."""

    def __call__(self, msg: str) -> None:
        pass

    def progress(self, frac: float) -> None:
        pass

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

_HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")

_MEDIA_DIR = _SCRATCH / "src_media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _make_synthetic_clip(name: str, color: str, duration: float, freq: int) -> Path:
    """A tiny, real, decodable mp4 (color bars + a sine tone) via ffmpeg's
    lavfi virtual inputs -- no external fixture files needed."""
    path = _MEDIA_DIR / name
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration}:r=25",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
    )
    return path


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class AddClipsProbesAtImport(unittest.TestCase):
    """The core #3 blocker: import must not depend on the pipeline to
    populate clip.info.duration."""

    def test_add_clips_populates_info_duration_with_no_pipeline_run(self):
        clip_path = _make_synthetic_clip("a.mp4", "red", 4.0, 440)
        project = store.new_project("manual-edit-add-clips")

        added = ingest.add_clips(project, [str(clip_path)])

        self.assertEqual(len(added), 1)
        clip = added[0]
        self.assertIsNotNone(clip["info"], "add_clips must probe info at import time")
        self.assertAlmostEqual(clip["info"]["duration"], 4.0, delta=0.2)
        self.assertTrue(clip["info"]["has_video"])
        self.assertTrue(clip["info"]["has_audio"])
        # No pipeline stage of any kind has run -- "stages" must stay empty.
        reloaded = store.load(project["id"])
        self.assertEqual(reloaded.get("stages", {}), {})
        self.assertIsNotNone(reloaded["clips"][0]["info"])
        self.assertAlmostEqual(reloaded["clips"][0]["info"]["duration"], 4.0, delta=0.2)

    def test_register_uploaded_clips_also_probes_at_import(self):
        # Mirrors the browser-mode upload path (api/projects.py's
        # clips_upload -> ingest.register_uploaded_clips): files already
        # streamed onto disk inside the project's media/ dir.
        project = store.new_project("manual-edit-upload")
        pdir = store.project_dir(project["id"])
        media_dir = pdir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        dest = media_dir / "b.mp4"
        _make_synthetic_clip("b.mp4", "blue", 3.0, 220)
        shutil.copy2(_MEDIA_DIR / "b.mp4", dest)

        added = ingest.register_uploaded_clips(project, [(dest, "main")])

        self.assertEqual(len(added), 1)
        self.assertIsNotNone(added[0]["info"])
        self.assertAlmostEqual(added[0]["info"]["duration"], 3.0, delta=0.2)

    def test_unprobeable_file_does_not_break_the_batch(self):
        garbage = _MEDIA_DIR / "not_really_a_video.mp4"
        garbage.write_bytes(b"not a real video file")
        good = _make_synthetic_clip("c.mp4", "green", 2.0, 330)
        project = store.new_project("manual-edit-garbage")

        added = ingest.add_clips(project, [str(garbage), str(good)])

        self.assertEqual(len(added), 2, "a corrupt file must not drop the rest of the batch")
        by_name = {c["filename"]: c for c in added}
        self.assertIsNone(by_name["not_really_a_video.mp4"]["info"])
        self.assertIsNotNone(by_name["c.mp4"]["info"])


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class ManualEdlNoPipeline(unittest.TestCase):
    """Empty-project start (#1) + full-range trim (#2): build/save an EDL
    with zero sentences/clip_order ever existing."""

    def setUp(self):
        self.client = TestClient(app)
        clip_path = _make_synthetic_clip(f"clip_{self.id()}.mp4", "red", 5.0, 440)
        self.project = store.new_project(f"manual-edl-{self.id()}")
        added = ingest.add_clips(self.project, [str(clip_path)])
        self.clip = added[0]
        self.project = store.load(self.project["id"])

    def test_get_edl_on_brand_new_project_is_empty_not_error(self):
        resp = self.client.get(f"/api/projects/{self.project['id']}/edl")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["segments"], [])
        self.assertEqual(self.project.get("sentences"), [])
        self.assertEqual(self.project.get("clip_order"), [])

    def test_put_full_clip_segment_with_no_sentences_ever_existing(self):
        dur = self.clip["info"]["duration"]
        body = {
            "segments": [
                {"clip_id": self.clip["id"], "start": 0.0, "end": dur, "text": "",
                 "transition": {"type": "none", "duration": 0.5}},
            ]
        }
        resp = self.client.put(f"/api/projects/{self.project['id']}/edl", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        segs = resp.json()["segments"]
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertAlmostEqual(segs[0]["end"], dur, delta=0.01)

    def test_full_range_trim_is_not_clamped_to_any_sentence_bound(self):
        dur = self.clip["info"]["duration"]
        # A manual segment that starts life covering only the middle...
        body = {
            "segments": [
                {"clip_id": self.clip["id"], "start": 1.0, "end": dur - 1.0, "text": "",
                 "transition": {"type": "none", "duration": 0.5}},
            ]
        }
        resp = self.client.put(f"/api/projects/{self.project['id']}/edl", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        # ...must be trimmable all the way OUT to [0, duration] -- no
        # sentence/pipeline-derived bound exists to clamp it tighter.
        body2 = {
            "segments": [
                {"clip_id": self.clip["id"], "start": 0.0, "end": dur, "text": "",
                 "transition": {"type": "none", "duration": 0.5}},
            ]
        }
        resp2 = self.client.put(f"/api/projects/{self.project['id']}/edl", json=body2)
        self.assertEqual(resp2.status_code, 200, resp2.text)
        segs = resp2.json()["segments"]
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertAlmostEqual(segs[0]["end"], dur, delta=0.01)

    def test_end_beyond_clip_duration_is_still_rejected(self):
        dur = self.clip["info"]["duration"]
        body = {
            "segments": [
                {"clip_id": self.clip["id"], "start": 0.0, "end": dur + 5.0, "text": "",
                 "transition": {"type": "none", "duration": 0.5}},
            ]
        }
        resp = self.client.put(f"/api/projects/{self.project['id']}/edl", json=body)
        self.assertEqual(resp.status_code, 400)


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class ExportFromManualEdlOnly(unittest.TestCase):
    """render.run() must produce a real file from a purely manual EDL --
    no sentences, no clip_order, no other pipeline stage ever run (#3)."""

    def test_render_run_produces_a_playable_file_from_manual_edl_alone(self):
        clip_a = _make_synthetic_clip("export_a.mp4", "red", 3.0, 440)
        clip_b = _make_synthetic_clip("export_b.mp4", "blue", 2.0, 220)
        project = store.new_project("manual-export")
        added = ingest.add_clips(project, [str(clip_a), str(clip_b)])
        self.assertEqual(len(added), 2)
        project = store.load(project["id"])

        segments = [
            {"clip_id": added[0]["id"], "start": 0.5, "end": added[0]["info"]["duration"],
             "text": "", "transition": {"type": "none", "duration": 0.5}},
            {"clip_id": added[1]["id"], "start": 0.0, "end": 1.5,
             "text": "", "transition": {"type": "none", "duration": 0.5}},
        ]
        project["edl"] = segments
        # Confirm the precondition: this project never ran any pipeline stage.
        self.assertEqual(project.get("sentences"), [])
        self.assertEqual(project.get("stages", {}), {})
        store.save(project)

        export_dir = Path(tempfile.mkdtemp(prefix="mve_manual_export_out_"))
        try:
            with unittest.mock.patch.object(render, "_export_dir_for", return_value=export_dir):
                render.run(_FakeLog(), project)
        finally:
            reloaded = store.load(project["id"])

        self.assertEqual(len(reloaded.get("renders", [])), 1)
        out_path = Path(reloaded["renders"][0]["path"])
        self.assertTrue(out_path.exists(), "render.run() must write the final export file")
        info = ffmpeg_utils.clip_info(str(out_path))
        # ~0.5s clipped from clip A (3.0-0.5) + 1.5s from clip B = 4.0s.
        self.assertAlmostEqual(info["duration"], 4.0, delta=0.35)
        self.assertTrue(info["has_video"])
        shutil.rmtree(export_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
        shutil.rmtree(_MEDIA_DIR, ignore_errors=True)
