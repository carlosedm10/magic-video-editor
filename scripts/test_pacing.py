#!/usr/bin/env python3
"""Contract test for the "ritmo" (cutting-rhythm) pacing feature (owner,
2026-07-26): a manual-vs-auto comparison found the auto cut too aggressive on
three PACING dimensions vs. a human editor -- head lead-in, micro-breaths
mid-paragraph, and tail. The owner's fix:

  1. Per-project `pacing` setting ("tight"/"natural"/"airy", Spanish UI
     "ceñido"/"natural"/"con aire", default "natural") drives three knobs
     (config.PACING_PRESETS): head_pad_s, merge_gap_s, tail_pad_s.
  2. pipeline/ordering.py::build_edl resolves the project's preset
     (resolve_pacing_preset) and uses merge_gap_s for the adjacent-merge
     decision + head_pad_s/tail_pad_s for the first/last segment's padding,
     instead of the old flat config.MERGE_GAP/SEGMENT_PAD -- but a project
     with NO pacing set at all falls back to those same bare globals
     unchanged (backward compatible with every pre-existing project and with
     scripts/test_intra_clip_order.py, which never sets project["pacing"]).
     A paragraph break still forces a split regardless of merge_gap_s.
  3. TAIL = "cortar en la ultima frase con sentido": after assembly, a
     conservative post-pass (_trim_trailing_low_content) drops trailing
     low-content sign-off/goodbye or hallucination-loop segments so the EDL
     always ends on the last sentence with real content -- independent of
     pacing.
  4. api/projects.py's ProjectUpdate exposes `pacing`, validated against
     config.PACING_PRESETS' keys (422 on anything else).

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_intra_clip_order.py / scripts/test_paragraph_cuts.py. MVE_DATA is
set to a scratch tmp dir BEFORE importing anything from magic_video_editor.
No ffmpeg/ollama involved -- build_edl is pure, and the API test uses
FastAPI's TestClient in-process.

Usage:
    uv run python scripts/test_pacing.py
    uv run python scripts/test_pacing.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = Path(tempfile.mkdtemp(prefix="mve_pacing_test_"))
os.environ["MVE_DATA"] = str(_SCRATCH)  # MUST happen before any magic_video_editor import

from magic_video_editor import config, store  # noqa: E402
from magic_video_editor.pipeline import ordering  # noqa: E402

assert str(config.DATA_DIR) == str(_SCRATCH), (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)

CLIP_ID = "clip-1"


def _camera_clip(cid: str, filename: str = "a.mp4", duration: float = 200.0) -> dict:
    return {
        "id": cid,
        "path": f"/fake/{filename}",
        "source_path": f"/fake/{filename}",
        "filename": filename,
        "role": "camera",
        "camera_group": "main",
        "is_main": False,
        "info": {"duration": duration, "has_audio": True, "has_video": True},
        "wav": None,
        "transcript": None,
        "language": None,
    }


def _sentence(sid: str, clip_id: str, start: float, end: float, text: str | None = None) -> dict:
    return {
        "id": sid,
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "text": text if text is not None else f"{sid} @ {start}-{end}",
        "kept": True,
        "reason": "",
    }


def _project(sentences: list[dict], pacing: str | None = None) -> dict:
    project = store.new_project("pacing-test")
    project["clips"] = [_camera_clip(CLIP_ID)]
    project["sentences"] = sentences
    project["clip_order"] = [CLIP_ID]
    if pacing is not None:
        project["pacing"] = pacing
    return project


class PacingPresetKnobs(unittest.TestCase):
    """config.PACING_PRESETS shape + resolve_pacing_preset resolution."""

    def test_presets_have_the_three_documented_knobs_in_increasing_order(self):
        for key in ("tight", "natural", "airy"):
            self.assertIn(key, config.PACING_PRESETS)
            preset = config.PACING_PRESETS[key]
            for knob in ("head_pad_s", "merge_gap_s", "tail_pad_s"):
                self.assertIn(knob, preset)
                self.assertGreater(preset[knob], 0)
        # tight < natural < airy for every knob (the whole point of the dial).
        for knob in ("head_pad_s", "merge_gap_s", "tail_pad_s"):
            self.assertLess(config.PACING_PRESETS["tight"][knob], config.PACING_PRESETS["natural"][knob])
            self.assertLess(config.PACING_PRESETS["natural"][knob], config.PACING_PRESETS["airy"][knob])

    def test_default_pacing_constant_is_natural(self):
        self.assertEqual(config.DEFAULT_PACING, "natural")

    def test_resolve_explicit_pacing_matches_preset(self):
        for key in ("tight", "natural", "airy"):
            project = _project([], pacing=key)
            self.assertEqual(ordering.resolve_pacing_preset(project), config.PACING_PRESETS[key])

    def test_resolve_unset_pacing_falls_back_to_bare_globals(self):
        """A project with no `pacing` key at all -- the state of every
        project that existed before this feature -- must resolve to exactly
        today's flat config.SEGMENT_PAD/MERGE_GAP, so build_edl's numeric
        output for such a project is completely unchanged (this is what keeps
        scripts/test_intra_clip_order.py, which never sets project["pacing"],
        green)."""
        project = _project([])
        self.assertNotIn("pacing", project)
        resolved = ordering.resolve_pacing_preset(project)
        self.assertEqual(resolved["head_pad_s"], config.SEGMENT_PAD)
        self.assertEqual(resolved["tail_pad_s"], config.SEGMENT_PAD)
        self.assertEqual(resolved["merge_gap_s"], config.MERGE_GAP)

    def test_resolve_invalid_pacing_also_falls_back_to_bare_globals(self):
        project = _project([], pacing="glacial")
        resolved = ordering.resolve_pacing_preset(project)
        self.assertEqual(resolved, ordering._globals_fallback_preset())


class MergeGapByPacing(unittest.TestCase):
    """The gap-of-~1.5s two-sentence case from the brief: natural/airy keep
    it ONE segment (no micro-cut of a breath), tight splits it in two."""

    def _two_sentences_1_5s_gap(self, pacing: str | None) -> list[dict]:
        sentences = [
            _sentence("s1", CLIP_ID, 10.0, 12.0),
            _sentence("s2", CLIP_ID, 13.5, 15.0),  # gap = 1.5s
        ]
        project = _project(sentences, pacing=pacing)
        return ordering.build_edl(project)

    def test_natural_merges_a_1_5s_gap_into_one_segment(self):
        segs = self._two_sentences_1_5s_gap("natural")
        self.assertEqual(len(segs), 1, f"expected one merged segment, got {segs}")

    def test_airy_merges_a_1_5s_gap_into_one_segment(self):
        segs = self._two_sentences_1_5s_gap("airy")
        self.assertEqual(len(segs), 1, f"expected one merged segment, got {segs}")

    def test_tight_splits_a_1_5s_gap_into_two_segments(self):
        segs = self._two_sentences_1_5s_gap("tight")
        self.assertEqual(len(segs), 2, f"expected two segments, got {segs}")

    def test_config_merge_gap_values_bracket_the_1_5s_gap_as_documented(self):
        # Sanity on the starting values themselves, per the brief: tight
        # (0.8) < 1.5s gap < natural/airy (>= 1.8).
        self.assertLess(config.PACING_PRESETS["tight"]["merge_gap_s"], 1.5)
        self.assertGreaterEqual(config.PACING_PRESETS["natural"]["merge_gap_s"], 1.8)
        self.assertGreaterEqual(config.PACING_PRESETS["airy"]["merge_gap_s"], 1.8)


class HeadPadByPacing(unittest.TestCase):
    """First segment's start should differ by preset (more lead-in for
    airy than tight), clamped >= 0."""

    def _first_segment_start(self, pacing: str, sentence_start: float = 5.0) -> float:
        sentences = [_sentence("s1", CLIP_ID, sentence_start, sentence_start + 1.0)]
        project = _project(sentences, pacing=pacing)
        segs = ordering.build_edl(project)
        self.assertEqual(len(segs), 1)
        return segs[0]["start"]

    def test_airy_starts_earlier_than_natural_which_starts_earlier_than_tight(self):
        tight_start = self._first_segment_start("tight")
        natural_start = self._first_segment_start("natural")
        airy_start = self._first_segment_start("airy")
        self.assertGreater(tight_start, natural_start)
        self.assertGreater(natural_start, airy_start)

    def test_head_pad_clamps_at_zero_near_clip_start(self):
        # sentence starts at 0.05s; even airy's 1.2s head pad must clamp to 0,
        # never go negative.
        start = self._first_segment_start("airy", sentence_start=0.05)
        self.assertEqual(start, 0.0)

    def test_tail_pad_clamps_at_clip_duration(self):
        duration = 200.0
        sentences = [_sentence("s1", CLIP_ID, duration - 0.3, duration - 0.05)]
        project = _project(sentences, pacing="airy")
        segs = ordering.build_edl(project)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["end"], duration)

    def test_interior_segment_padding_unaffected_by_pacing(self):
        """Only the overall first/last segment's padding uses head_pad_s/
        tail_pad_s -- everything else keeps the flat config.SEGMENT_PAD, for
        any pacing."""
        sentences = [
            _sentence("s1", CLIP_ID, 5.0, 6.0),
            _sentence("s2", CLIP_ID, 50.0, 51.0),  # far from s1 and s3 -- own segment
            _sentence("s3", CLIP_ID, 100.0, 101.0),
        ]
        project = _project(sentences, pacing="airy")
        segs = ordering.build_edl(project)
        self.assertEqual(len(segs), 3, f"expected 3 isolated segments, got {segs}")
        middle = segs[1]
        self.assertAlmostEqual(middle["start"], 50.0 - config.SEGMENT_PAD, places=6)
        self.assertAlmostEqual(middle["end"], 51.0 + config.SEGMENT_PAD, places=6)


class ParagraphBreakOverridesMergeGap(unittest.TestCase):
    """A paragraph break must still force a split even when the gap is well
    within merge_gap_s (natural/airy would otherwise merge it)."""

    def test_paragraph_break_forces_split_within_natural_merge_gap(self):
        sentences = [
            _sentence("s1", CLIP_ID, 10.0, 12.0),
            _sentence("s2", CLIP_ID, 13.0, 15.0),  # gap = 1.0s, well within natural's 1.8
        ]
        project = _project(sentences, pacing="natural")
        merged = ordering.build_edl(project)
        self.assertEqual(len(merged), 1, "sanity: without a break these merge under natural")

        split = ordering.build_edl(project, paragraph_break_after={"s1"})
        self.assertEqual(len(split), 2, "a paragraph break must force a split despite merge_gap_s")
        self.assertTrue(split[1]["paragraph_break"])


class TailTrimLastMeaningfulSentence(unittest.TestCase):
    """TAIL = cut on the last sentence with real content, dropping trailing
    low-content closers/hallucinations, regardless of pacing."""

    def _build(self, texts: list[str], pacing: str | None = None) -> list[dict]:
        sentences = []
        t = 0.0
        for i, text in enumerate(texts):
            sentences.append(_sentence(f"s{i}", CLIP_ID, t, t + 1.0, text=text))
            t += 20.0  # far apart -- never merges, one segment per sentence
        project = _project(sentences, pacing=pacing)
        return ordering.build_edl(project)

    def test_trailing_gracias_is_dropped(self):
        segs = self._build(["Este es el contenido real de la charla.", "Gracias."])
        self.assertEqual(len(segs), 1)
        self.assertIn("contenido real", segs[0]["text"])

    def test_trailing_nos_vemos_en_el_siguiente_is_dropped(self):
        segs = self._build(
            ["Y con eso cerramos el tema principal.", "Nos vemos en el siguiente."]
        )
        self.assertEqual(len(segs), 1)
        self.assertIn("cerramos el tema", segs[0]["text"])

    def test_trailing_hallucination_loop_is_dropped(self):
        segs = self._build(
            ["Este es el ultimo punto importante que queria compartir.", "gracias gracias gracias gracias gracias gracias"]
        )
        self.assertEqual(len(segs), 1)
        self.assertIn("ultimo punto importante", segs[0]["text"])

    def test_multiple_trailing_closers_all_dropped(self):
        segs = self._build(
            [
                "Ese fue el consejo mas importante del video.",
                "Muchas gracias por escuchar.",
                "Nos vemos en el proximo video.",
            ]
        )
        self.assertEqual(len(segs), 1)
        self.assertIn("consejo mas importante", segs[0]["text"])

    def test_meaningful_last_sentence_is_never_trimmed(self):
        segs = self._build(
            ["Primero hablamos del problema.", "Y esta fue la conclusion final del analisis."]
        )
        self.assertEqual(len(segs), 2)
        self.assertIn("conclusion final", segs[-1]["text"])

    def test_tail_trim_never_empties_the_whole_edl(self):
        """Even if EVERY sentence looks like a closer, at least one segment
        must survive -- never trim a project down to nothing."""
        segs = self._build(["Gracias.", "Nos vemos.", "Chau."])
        self.assertEqual(len(segs), 1)

    def test_tail_trim_disabled_toggle_keeps_everything(self):
        sentences = [
            _sentence("s0", CLIP_ID, 0.0, 1.0, text="Contenido real."),
            _sentence("s1", CLIP_ID, 20.0, 21.0, text="Gracias."),
        ]
        project = _project(sentences)
        original = config.TAIL_TRIM_ENABLED
        config.TAIL_TRIM_ENABLED = False
        try:
            segs = ordering.build_edl(project)
        finally:
            config.TAIL_TRIM_ENABLED = original
        self.assertEqual(len(segs), 2)

    def test_tail_trim_is_pacing_independent(self):
        for pacing in (None, "tight", "natural", "airy"):
            segs = self._build(
                ["Contenido real y sustancioso sobre el tema.", "Suscribete al canal."],
                pacing=pacing,
            )
            self.assertEqual(len(segs), 1, f"pacing={pacing!r} left the closer in: {segs}")


class ApiPacingField(unittest.TestCase):
    """api/projects.py's ProjectUpdate.pacing validation."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from magic_video_editor.server import app

        cls.client = TestClient(app)

    def _new_project(self) -> str:
        r = self.client.post("/api/projects", json={"name": "pacing-api-test"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_default_is_natural_when_unset(self):
        pid = self._new_project()
        r = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 200, r.text)
        # No pacing set yet on a fresh project -- the field is simply absent
        # (mirrors speaker_count/language_override's own "unset means
        # default" convention); config.DEFAULT_PACING documents what that
        # default resolves to.
        self.assertIsNone(r.json().get("pacing"))
        self.assertEqual(config.DEFAULT_PACING, "natural")

    def test_patch_valid_pacing_values(self):
        pid = self._new_project()
        for value in ("tight", "natural", "airy"):
            r = self.client.patch(f"/api/projects/{pid}", json={"pacing": value})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["pacing"], value)

    def test_patch_invalid_pacing_is_422(self):
        pid = self._new_project()
        r = self.client.patch(f"/api/projects/{pid}", json={"pacing": "glacial"})
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, exit=False)
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
