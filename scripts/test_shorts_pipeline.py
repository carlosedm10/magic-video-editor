#!/usr/bin/env python3
"""Tests for the "shorts are a separate, explicit step" rework (owner's
mental model: edit the main video to completion first; THEN, as a separate
explicit action, generate shorts FROM that finished cut). Covers three
independent changes:

  1. run-all no longer runs the reels stage: magic_video_editor.api.pipeline's
     STAGE_ORDER (the run-all sequence) excludes "reels" and ends at
     "render", while STAGES (the full runnable-stage registry validated by
     run_stage()/`_run_stage_kind`) still includes it, so "stage:reels"
     keeps working standalone. `_run_all_kind` never touches the reels
     stage, and a standalone "stage:reels" run still auto-enqueues
     "reel_previews" (magic_video_editor.queue's existing
     `_run_auto_enqueue_hooks` "stage:reels" branch, unchanged by this work).
  2. magic_video_editor.pipeline.reels._candidate_windows sources its sliding
     windows from project["edl"] (the FINAL approved cut) when present, so a
     moment the user trimmed out of the main video can never end up inside a
     reel window, even though its sentences are still flagged `kept` at the
     sentence-analysis level; falls back to sentence-level `kept` alone when
     there's no EDL yet.
  3. A reel's subtitles are fully decoupled from project["subtitles"]:
     `_compose_reel` only burns subtitles when the reel ITSELF has
     `subtitles_enabled` (new field, default False), never by inheriting the
     main project's subtitle config/sizing.

The LLM is mocked wherever a stage would otherwise call one (magic_video_editor.
pipeline.reels.get_agent), and every project lives under a scratch MVE_DATA
dir (MUST be set before importing magic_video_editor, same convention as
scripts/test_reel_dedup.py / test_reel_previews.py). Test 3 uses a real tiny
ffmpeg-generated clip (same pattern as test_reel_previews.py) since asserting
"the ass burn step was skipped" is most directly checked by whether the
per-segment .ass sidecar file was ever written to disk.

Usage:
    uv run python scripts/test_shorts_pipeline.py
    uv run python scripts/test_shorts_pipeline.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_shorts_pipeline_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from magic_video_editor import config, ffmpeg_utils, queue, store  # noqa: E402
from magic_video_editor.api import pipeline  # noqa: E402
from magic_video_editor.pipeline import ingest, reels  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


class _Log:
    """Minimal stand-in for jobs.JobLog (same shape used by
    test_reel_dedup.py/test_reel_previews.py): collects messages, no-op
    progress/stage, never cancels."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    def progress(self, _frac: float) -> None:
        pass

    def stage(self, *_a, **_k) -> None:
        pass


class _FakeAgent:
    """Same stand-in as test_reel_dedup.py: run_sync(prompt).output ==
    fn(prompt), no Ollama involved."""

    def __init__(self, fn):
        self._fn = fn

    def run_sync(self, prompt: str):
        return types.SimpleNamespace(output=self._fn(prompt))


def _mk_sentence(sid: str, clip_id: str, start: float, end: float, kept: bool = True) -> dict:
    return {
        "id": sid,
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "text": f"sentence {sid}",
        "kept": kept,
    }


# ---------------------------------------------------------------------------
# 1. run-all no longer includes reels
# ---------------------------------------------------------------------------


class RunAllExcludesReelsTests(unittest.TestCase):
    def test_stage_order_ends_at_render_without_reels(self):
        self.assertNotIn("reels", pipeline.STAGE_ORDER)
        self.assertEqual(pipeline.STAGE_ORDER[-1], "render")
        self.assertEqual(
            pipeline.STAGE_ORDER,
            ["ingest", "sync", "transcribe", "takes", "order", "paragraphs", "review", "render"],
        )

    def test_reels_stage_still_registered_for_standalone_use(self):
        # STAGES (validated by run_stage()/_run_stage_kind) must still list
        # "reels" even though STAGE_ORDER (run-all) no longer does -- that's
        # what keeps POST /projects/{pid}/run/reels ("stage:reels") working.
        self.assertIn("reels", pipeline.STAGES)
        self.assertIn("reels", pipeline.STAGE_LABELS)

    def test_run_all_kind_never_invokes_reels_and_stops_at_render(self):
        calls: list[str] = []

        def _fake_stage(name):
            def _fn(log, project):
                calls.append(name)
                log(f"ran {name}")

            return _fn

        fake_stages = {name: _fake_stage(name) for name in pipeline.STAGE_ORDER}
        fake_stages["reels"] = _fake_stage("reels")  # present in STAGES, must never be called

        project = store.new_project("run-all-excludes-reels")
        with (
            patch.dict(pipeline.STAGES, fake_stages, clear=False),
            patch.object(pipeline, "_preflight_stage", lambda _s: None),
        ):
            pipeline._run_all_kind(_Log(), project, {})

        self.assertEqual(
            calls, pipeline.STAGE_ORDER, "run-all must run every STAGE_ORDER stage, in order"
        )
        self.assertNotIn("reels", calls, "run-all must never invoke the reels stage")
        self.assertEqual(
            calls[-1], "render", "run-all's last step must be render (the finished main cut)"
        )


class StandaloneReelsStageTests(unittest.TestCase):
    """ "stage:reels" run through the real queue (registered runners), proving
    (a) it still produces suggestions, and (b) the existing reel_previews
    auto-enqueue hook still fires for a manual reels run -- unaffected by
    reels leaving run-all, since magic_video_editor.queue's hook keys off the
    queue item's own kind ("stage:reels"), never off run-all."""

    @classmethod
    def setUpClass(cls):
        cls.project = store.new_project("standalone-reels-stage")
        cls.project["clips"] = [{"id": "clipS", "role": "camera"}]
        cls.project["sentences"] = [_mk_sentence("s0", "clipS", 0.0, 20.0, kept=True)]
        store.save(cls.project)

    @classmethod
    def tearDownClass(cls):
        # Bounded drain of the real background worker thread queue.enqueue()
        # spins up, same pattern as test_reel_previews.py's tearDownClass.
        import time as _time

        deadline = _time.time() + 15
        while _time.time() < deadline:
            try:
                project = store.load(cls.project["id"])
            except FileNotFoundError:
                break
            if not any(i["status"] in ("pending", "running") for i in project.get("queue", [])):
                break
            _time.sleep(0.2)

    def _fake_reel_scorer(self, _prompt: str):
        return types.SimpleNamespace(hook=8.0, self_contained=8.0, payoff=8.0, title="A short")

    def _fake_get_agent(self, task: str):
        if task == "reel_scorer":
            return _FakeAgent(self._fake_reel_scorer)
        if task == "reel_composer":
            return _FakeAgent(lambda _p: types.SimpleNamespace(combine=False, why="", order="ab"))
        if task == "reel_dedup":
            return _FakeAgent(
                lambda _p: types.SimpleNamespace(
                    same_content=False, keep="a", confidence=1, reason=""
                )
            )
        raise AssertionError(f"unexpected agent task requested: {task!r}")

    def test_standalone_run_produces_suggestions_and_enqueues_previews(self):
        pid = self.project["id"]
        with (
            patch("magic_video_editor.pipeline.reels.llm.available", return_value=True),
            patch(
                "magic_video_editor.pipeline.reels._candidate_windows",
                return_value=[
                    {
                        "clip_id": "clipS",
                        "start": 0.0,
                        "end": 20.0,
                        "text": "hello world",
                        "duration": 20.0,
                    }
                ],
            ),
            patch("magic_video_editor.pipeline.reels.get_agent", side_effect=self._fake_get_agent),
            patch("magic_video_editor.pipeline.reels._copy_for_reel_safe", return_value=None),
        ):
            item = queue.enqueue(pid, "stage:reels", {"stage": "reels"})
            self._wait_for_item(pid, item["id"])

        finished = self._find_item(pid, item["id"])
        self.assertEqual(finished["status"], "done", finished.get("error"))

        project = store.load(pid)
        self.assertTrue(project.get("reels"), "standalone stage:reels must produce suggestions")

        queued_kinds = [i["kind"] for i in project.get("queue", [])]
        self.assertIn(
            "reel_previews",
            queued_kinds,
            "a manual stage:reels run must still auto-enqueue reel_previews",
        )

    @staticmethod
    def _find_item(pid: str, item_id: str) -> dict:
        project = store.load(pid)
        return next(i for i in project["queue"] if i["id"] == item_id)

    def _wait_for_item(self, pid: str, item_id: str, timeout: float = 20.0) -> None:
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            item = self._find_item(pid, item_id)
            if item["status"] not in ("pending", "running"):
                return
            _time.sleep(0.1)
        self.fail(f"stage:reels item {item_id} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# 2. reels source from the final EDL, not raw kept sentences
# ---------------------------------------------------------------------------


class CandidateWindowsSourceFromEdlTests(unittest.TestCase):
    """project["sentences"]["kept"] alone is NOT the source of truth once an
    EDL exists -- only content inside the EDL's own kept ranges may become a
    reel candidate, even when a sentence in the gap is still `kept: True`
    (e.g. the user trimmed it out of the timeline after take-selection ran)."""

    @classmethod
    def _project_with_continuous_kept_sentences(cls) -> dict:
        project = store.new_project("edl-constrained-candidates")
        project["clips"] = [{"id": "clipE", "role": "camera"}]
        # Continuous kept sentences, 2s each, covering 0..50s with no gap
        # ever exceeding the 4.0s "big content hole" threshold -- so without
        # any EDL constraint this is all ONE continuous candidate run.
        sentences = []
        t = 0.0
        i = 0
        while t < 50.0:
            sentences.append(_mk_sentence(f"s{i}", "clipE", t, t + 2.0, kept=True))
            t += 2.0
            i += 1
        project["sentences"] = sentences
        return project

    def test_no_edl_yet_falls_back_to_raw_kept_sentences(self):
        project = self._project_with_continuous_kept_sentences()
        self.assertNotIn("edl", project)  # sanity: fresh project has no EDL yet
        windows = reels._candidate_windows(project)
        self.assertTrue(windows)
        # Without EDL awareness a window may freely span across what will
        # later become the 20-30s cut -- i.e. some window covers both sides.
        spans_the_gap = any(w["start"] < 20.0 and w["end"] > 30.0 for w in windows)
        self.assertTrue(
            spans_the_gap, "fallback (no EDL) must behave exactly like before: sentence-kept-only"
        )

    def test_edl_excludes_a_time_range_no_window_ever_covers_it(self):
        project = self._project_with_continuous_kept_sentences()
        # The final approved cut kept 0-20s and 30-50s -- the user trimmed
        # 20-30s out of the main video entirely (even though those sentences
        # are still flagged kept=True at the sentence-analysis level).
        project["edl"] = [
            {"clip_id": "clipE", "start": 0.0, "end": 20.0, "text": "", "paragraph_break": False},
            {"clip_id": "clipE", "start": 30.0, "end": 50.0, "text": "", "paragraph_break": False},
        ]
        windows = reels._candidate_windows(project)
        self.assertTrue(windows, "candidates must still be found within the kept ranges")

        for w in windows:
            self.assertGreaterEqual(w["duration"], config.REEL_MIN_S - 0.01)
            in_first_range = w["start"] >= -0.01 and w["end"] <= 20.0 + 0.01
            in_second_range = w["start"] >= 30.0 - 0.01 and w["end"] <= 50.0 + 0.01
            self.assertTrue(
                in_first_range or in_second_range,
                f"window {w} escapes both EDL-kept ranges -- covers cut-out content",
            )
        # And, concretely, nothing ever bridges the removed 20-30s gap.
        self.assertFalse(any(w["start"] < 20.0 and w["end"] > 20.0 for w in windows))
        self.assertFalse(any(w["start"] < 30.0 and w["end"] > 30.0 for w in windows))

    def test_edl_present_but_clip_entirely_absent_yields_no_candidates_for_it(self):
        """An EDL that exists but never mentions a clip means every bit of
        that clip's kept-flagged content was cut from the final video --
        must never produce a candidate."""
        project = self._project_with_continuous_kept_sentences()
        project["edl"] = [{"clip_id": "some-other-clip", "start": 0.0, "end": 10.0}]
        windows = reels._candidate_windows(project)
        self.assertEqual(windows, [])


# ---------------------------------------------------------------------------
# 3. reel subtitles are decoupled from project["subtitles"]
# ---------------------------------------------------------------------------


def _make_synthetic_clip(dst: Path, duration: float = 3.0) -> None:
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
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


class ReelSubtitleDecouplingTests(unittest.TestCase):
    """render_reel/_compose_reel must never inherit project["subtitles"] --
    a reel burns subtitles ONLY when it explicitly opts in via
    reel["subtitles_enabled"] (new field, default False)."""

    @classmethod
    def setUpClass(cls):
        cls.project = store.new_project("reel-subtitle-decoupling")
        cls.pid = cls.project["id"]

        # The MAIN project has subtitles ON -- this must NOT leak into a
        # short by default (the exact bug this change fixes).
        cls.project["subtitles"] = {"enabled": True, "style": "bold"}

        src_dir = Path(tempfile.mkdtemp(prefix="mve_shorts_subs_src_"))
        cls.src_clip = src_dir / "clip.mp4"
        _make_synthetic_clip(cls.src_clip)

        added = ingest.add_clips(cls.project, [str(cls.src_clip)])
        cls.clip_id = added[0]["id"]
        ingest.run(_Log(), cls.project)
        store.save(cls.project)

        clip = store.get_clip(cls.project, cls.clip_id)
        assert clip["info"]["has_video"] and clip["info"]["codec_name"] == "h264", clip["info"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def _base_reel(self, reel_id: str) -> dict:
        reel = {
            "id": reel_id,
            "rank": 1,
            "clip_id": self.clip_id,
            "start": 0.2,
            "end": 2.5,
            "duration": 2.3,
            "title": "Test reel",
            "description": "",
            "hashtags": [],
            "score": 5.0,
            "hook": 1.0,
            "self_contained": 1.0,
            "payoff": 1.0,
            "text": "",
            "path": None,
            "status": "suggested",
            "in_override": None,
            "out_override": None,
            "crop_x": None,
            "cue_overrides": {},
            "subtitle_style": {},
            "transform": dict(reels.DEFAULT_TRANSFORM),
            "segments": [
                {
                    "clip_id": self.clip_id,
                    "start": 0.2,
                    "end": 2.5,
                    "in_override": None,
                    "out_override": None,
                }
            ],
            "transitions": [],
            "composed": False,
            "preview_ready": False,
            "preview_hash": None,
        }
        reels.ensure_segments(reel)
        return reel

    def _compose_and_count_ass_files(self, reel: dict) -> int:
        work = Path(tempfile.mkdtemp(prefix="mve_shorts_subs_work_"))
        try:
            reels._compose_reel(
                _Log(),
                self.project,
                reel,
                work,
                reels.PREVIEW_W,
                reels.PREVIEW_H,
                reels.PREVIEW_CRF,
                reels.PREVIEW_PRESET,
            )
            return len(list(work.glob("*.ass")))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_reel_without_explicit_subtitles_burns_none_despite_main_enabled(self):
        reel = self._base_reel("reel_no_subs")
        self.assertFalse(
            reel["subtitles_enabled"], "ensure_segments must default subtitles_enabled False"
        )
        n_ass = self._compose_and_count_ass_files(reel)
        self.assertEqual(
            n_ass, 0, "a reel with subtitles_enabled=False must skip the .ass burn step entirely"
        )

    def test_reel_with_its_own_subtitles_enabled_does_burn(self):
        reel = self._base_reel("reel_with_subs")
        reel["subtitles_enabled"] = True
        n_ass = self._compose_and_count_ass_files(reel)
        self.assertGreater(n_ass, 0, "a reel that explicitly enables subtitles must burn its own")

    def test_legacy_reel_missing_the_field_migrates_to_disabled(self):
        """A pre-existing reel persisted before this field existed must
        migrate to subtitles_enabled=False on ensure_segments, not silently
        inherit the main project's enabled=True."""
        reel = self._base_reel("reel_legacy")
        del reel["subtitles_enabled"]
        reels.ensure_segments(reel)
        self.assertFalse(reel["subtitles_enabled"])

    def test_effective_subtitle_cfg_ignores_project_subtitles_entirely(self):
        reel = self._base_reel("reel_cfg_check")
        reel["subtitle_style"] = {"style": "karaoke"}
        cfg = reels._effective_subtitle_cfg(reel)
        # project["subtitles"]["style"] is "bold" -- if this leaked through,
        # an UNSET field on subtitle_style (e.g. color) would come from the
        # main project's config instead of subtitles.DEFAULTS.
        self.assertEqual(cfg["style"], "karaoke")
        self.assertEqual(
            cfg["color"],
            "#FFFFFF",
            "unset fields must fall back to subtitles.DEFAULTS, not the project's config",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
