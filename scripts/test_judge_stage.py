#!/usr/bin/env python3
"""Unit tests for the pre-render judge stage (pipeline/judge.py, spec point
6): a multi-run text-only judge comparing the EDITED transcript against the
ORDERED-BUT-UNCUT originals, run after `review` and before `render`.

Covers the stage-level (run()) behavior:
  - no sentences at all -> skip, no agent call
  - sentences but none kept -> skip, no agent call
  - ollama unavailable -> skip, no agent call
  - a majority "lost_content" finding becomes a suggestion, NEVER auto-applies
    (no sentence is ever flipped kept=false for this kind)
  - a majority "order_issue" finding becomes a suggestion, NEVER reorders
    clip_order/sentences
  - "kept_blooper" only auto-cuts when severity is >= config.
    JUDGE_AUTOCUT_SEVERITY in EVERY contributing run; below the bar it's a
    suggestion instead
  - auto-cut only ever flips the ids every contributing run agreed on (the
    INTERSECTION) -- an extra id one run lumps in alongside the true
    blooper is never cut, end-to-end through run()
  - project["edl"] is reset to None after an auto-cut (ordering.run's
    invalidation pattern), but left alone when nothing was auto-cut
  - a majority kept_blooper auto-cut also leaves a non-open, informational
    "auto_applied" suggestion naming the cut sentence text, and stamps
    project["judge_autocut_at"] (the stale-EDL-PUT mitigation)
  - run()'s final save reloads the project FRESH from store and re-applies
    only judge's own deltas onto it, so a concurrent HTTP write (rename,
    suggestion accept, ...) landing during judge's multi-minute run of
    sequential Ollama calls survives judge's own save intact

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_take_selection.py. MVE_DATA is set to a scratch tmp dir BEFORE
importing anything from magic_video_editor. `get_agent` is fully MOCKED so
this script makes no network call and spawns no ollama process.

Usage:
    uv run python scripts/test_judge_stage.py
    uv run python scripts/test_judge_stage.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

_SCRATCH = tempfile.mkdtemp(prefix="mve_judge_stage_test_")
os.environ["MVE_DATA"] = _SCRATCH  # MUST happen before any magic_video_editor import

from magic_video_editor import config  # noqa: E402
from magic_video_editor import store  # noqa: E402
from magic_video_editor.pipeline import judge  # noqa: E402

assert str(config.DATA_DIR) == _SCRATCH, (
    f"config.DATA_DIR ({config.DATA_DIR}) did not pick up MVE_DATA ({_SCRATCH}) -- "
    "a scratch-dir test must never touch the real data dir."
)


def _no_log(msg: str) -> None:
    pass


def _mk_sentence(idx: int, text: str, kept: bool = True, clip_id: str = "clip1") -> dict:
    return {
        "id": f"s{clip_id}-{idx}",
        "clip_id": clip_id,
        "start": float(idx * 2),
        "end": float(idx * 2 + 1.5),
        "text": text,
        "kept": kept,
    }


def _mk_project(sentences: list, clip_ids: list[str] | None = None) -> dict:
    project = store.new_project("judge-stage-test")
    clip_ids = clip_ids or sorted({s["clip_id"] for s in sentences})
    project["clips"] = [
        {"id": cid, "filename": f"{cid}.mp4", "role": "camera", "is_main": True} for cid in clip_ids
    ]
    project["sentences"] = sentences
    project["edl"] = [{"fake": "edl"}]
    # judge.run()'s final save reloads the project FRESH from store (the
    # reload-merge concurrency fix, see judge.py's module docstring) rather
    # than trusting this in-memory snapshot -- so the on-disk copy must
    # actually reflect what the test set up, exactly like a real caller
    # (store.load -> mutate -> stage.run -> ...) would have on disk already.
    store.save(project)
    return project


def _fake_finding(kind: str, sentence_ids: list, severity: int, message: str = "reason"):
    return types.SimpleNamespace(
        kind=kind, sentence_ids=sentence_ids, message=message, severity=severity
    )


def _fake_result(findings: list):
    return types.SimpleNamespace(findings=findings)


class _FakeAgent:
    """Returns `responses[call_index]` (clamped to the last entry) each
    run_sync call and records every prompt it was asked to run."""

    def __init__(self, responses: list):
        self.responses = responses
        self.calls: list[str] = []

    def run_sync(self, prompt: str):
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return types.SimpleNamespace(output=self.responses[idx])


class NoopEarlyOutTests(unittest.TestCase):
    def test_no_sentences_skips_without_calling_agent(self):
        project = store.new_project("judge-empty")
        with mock.patch("magic_video_editor.agents.agents.get_agent") as get_agent:
            judge.run(_no_log, project)
        get_agent.assert_not_called()
        self.assertEqual(project.get("suggestions", []), [])

    def test_no_kept_sentences_skips_without_calling_agent(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.", kept=False) for i in range(3)]
        project = _mk_project(sentences)
        with mock.patch("magic_video_editor.agents.agents.get_agent") as get_agent:
            judge.run(_no_log, project)
        get_agent.assert_not_called()

    def test_ollama_unavailable_skips_without_calling_agent(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(3)]
        project = _mk_project(sentences)
        with (
            mock.patch("magic_video_editor.pipeline.judge.llm.available", return_value=False),
            mock.patch("magic_video_editor.agents.agents.get_agent") as get_agent,
        ):
            judge.run(_no_log, project)
        get_agent.assert_not_called()


def _run_with_findings_every_run(project: dict, findings_per_run: list):
    """Runs judge.run() where every one of config.JUDGE_RUNS agent calls in
    the (single, since nothing auto-cuts) pass returns `findings_per_run`."""
    agent = _FakeAgent([_fake_result(findings_per_run)])
    with (
        mock.patch("magic_video_editor.pipeline.judge.llm.available", return_value=True),
        mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
    ):
        judge.run(_no_log, project)
    return agent


class LostContentNeverAutoAppliesTests(unittest.TestCase):
    def test_majority_lost_content_is_suggestion_only(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(4)]
        project = _mk_project(sentences)
        pre_edl = project["edl"]
        finding = _fake_finding("lost_content", [1], severity=5)
        _run_with_findings_every_run(project, [finding])

        self.assertTrue(all(s["kept"] for s in project["sentences"]), "no sentence flipped")
        # (value, not identity: judge.run()'s final save reloads a fresh
        # dict from store and syncs `project` to it -- see judge.py's
        # reload-merge concurrency fix -- so it's a distinct but
        # value-equal object when nothing changed.)
        self.assertEqual(project["edl"], pre_edl, "edl untouched when nothing was auto-cut")
        suggestions = project["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["kind"], "lost_content")
        self.assertEqual(suggestions[0]["status"], "open")
        self.assertEqual(suggestions[0]["source"], "judge")


class OrderIssueNeverReordersTests(unittest.TestCase):
    def test_majority_order_issue_is_suggestion_only(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(4)]
        project = _mk_project(sentences)
        original_order = list(project["clip_order"])
        original_ids = [s["id"] for s in project["sentences"]]
        finding = _fake_finding("order_issue", [2, 3], severity=5)
        _run_with_findings_every_run(project, [finding])

        self.assertEqual(project["clip_order"], original_order)
        self.assertEqual([s["id"] for s in project["sentences"]], original_ids)
        suggestions = project["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["kind"], "order_issue")
        self.assertEqual(suggestions[0]["proposed_action"], "reorder")


class KeptBlooperAutocutGateTests(unittest.TestCase):
    def test_below_bar_severity_becomes_suggestion_not_autocut(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(4)]
        project = _mk_project(sentences)
        pre_edl = project["edl"]
        below = config.JUDGE_AUTOCUT_SEVERITY - 1
        self.assertGreaterEqual(below, 1)
        finding = _fake_finding("kept_blooper", [2], severity=below)
        _run_with_findings_every_run(project, [finding])

        self.assertTrue(all(s["kept"] for s in project["sentences"]))
        # value, not identity -- see reload-merge note above
        self.assertEqual(project["edl"], pre_edl)
        suggestions = project["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["kind"], "kept_blooper")
        self.assertEqual(suggestions[0]["proposed_action"], "cut")

    def test_at_bar_severity_autocuts_and_resets_edl(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(4)]
        project = _mk_project(sentences)
        target_id = sentences[2]["id"]
        finding = _fake_finding("kept_blooper", [3], severity=config.JUDGE_AUTOCUT_SEVERITY)

        # Second pass (triggered by the auto-cut) must find nothing further,
        # so the loop stops after 2 passes instead of hitting MAX_JUDGE_ITERATIONS.
        empty = _fake_result([])
        agent = _FakeAgent(
            [_fake_result([finding])] * config.JUDGE_RUNS + [empty] * config.JUDGE_RUNS
        )
        with (
            mock.patch("magic_video_editor.pipeline.judge.llm.available", return_value=True),
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
        ):
            judge.run(_no_log, project)

        flipped = next(s for s in project["sentences"] if s["id"] == target_id)
        self.assertFalse(flipped["kept"], "the flagged kept sentence must be auto-cut")
        self.assertIsNone(project["edl"], "edl must be invalidated after an auto-cut")
        self.assertIn(
            "judge_autocut_at", project,
            "judge_autocut_at epoch stamp must be recorded (stale-EDL-PUT mitigation)",
        )
        # No OPEN suggestion for the auto-applied finding itself -- nothing
        # for a human to accept/dismiss -- but there IS a non-open,
        # informational record naming the cut text, so a human (or a stale
        # browser reinstating the old EDL) can see what was actually
        # removed.
        judge_suggestions = [s for s in project.get("suggestions", []) if s["source"] == "judge"]
        open_kinds = [s["kind"] for s in judge_suggestions if s["status"] == "open"]
        self.assertNotIn("kept_blooper", open_kinds)
        autocut_records = [s for s in judge_suggestions if s["status"] == "auto_applied"]
        self.assertEqual(len(autocut_records), 1)
        self.assertEqual(autocut_records[0]["kind"], "kept_blooper")
        self.assertIn("Sentence 2", autocut_records[0]["message"], "must name the cut text")


class AutocutIntersectionRunLevelTests(unittest.TestCase):
    """End-to-end (run()-level) version of the intersection-vs-union fix:
    an id only one contributing run mentioned must survive, even though the
    majority-consensus finding as a whole (kind + overlapping ids) is real
    and does auto-cut the id every contributing run agreed on."""

    def test_extra_id_in_one_run_is_not_cut(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(4)]
        project = _mk_project(sentences)
        # Sentence numbers are 1-indexed in clip_order (start-time) order,
        # so number 2 -> sentences[1], number 3 -> sentences[2].
        agreed_id = sentences[1]["id"]
        extra_only_id = sentences[2]["id"]
        at_bar = config.JUDGE_AUTOCUT_SEVERITY

        run0 = _fake_result([_fake_finding("kept_blooper", [2, 3], severity=at_bar)])
        run1 = _fake_result([_fake_finding("kept_blooper", [2], severity=at_bar)])
        run2 = _fake_result([])  # third run has no opinion; majority is still 2/3
        empty = _fake_result([])
        # Pass 1: the three JUDGE_RUNS calls above. Pass 2 (triggered by the
        # auto-cut): all empty, so the loop stops instead of hitting
        # MAX_JUDGE_ITERATIONS.
        agent = _FakeAgent([run0, run1, run2] + [empty] * config.JUDGE_RUNS)
        with (
            mock.patch("magic_video_editor.pipeline.judge.llm.available", return_value=True),
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
        ):
            judge.run(_no_log, project)

        by_id = {s["id"]: s for s in project["sentences"]}
        self.assertFalse(
            by_id[agreed_id]["kept"], "the id every contributing run agreed on must be cut"
        )
        self.assertTrue(
            by_id[extra_only_id]["kept"],
            "an id only ONE contributing run mentioned must never be auto-cut",
        )
        self.assertIsNone(project["edl"], "an autocut did happen (on the agreed id)")


class ReloadMergeConcurrencyTests(unittest.TestCase):
    """The critical fix: judge.run() must not silently revert a concurrent
    HTTP write that lands during its (potentially long) run of sequential
    Ollama calls. Simulated here by making the store.load() call inside
    judge's own end-of-run save (_save_judge_deltas) itself perform (once)
    an out-of-band write to the SAME project.json, mimicking another
    request's store.load-mutate-store.save cycle completing while judge was
    busy -- then judge's own reload-and-save must build on top of that,
    not stomp it."""

    def test_concurrent_write_survives_and_judges_own_deltas_still_apply(self):
        sentences = [_mk_sentence(i, f"Sentence {i}.") for i in range(4)]
        project = _mk_project(sentences)
        pid = project["id"]
        target_id = sentences[2]["id"]  # number 3 -> the one judge will flag

        real_load = store.load  # captured BEFORE patching, so our injector
        # can use it to read/write without recursing into the mock.

        concurrent_landed = {"n": 0}

        def load_with_concurrent_write(project_id):
            # Only inject once, and only for the project this test cares
            # about -- exactly one "other request" landing mid-judge.
            if project_id == pid and concurrent_landed["n"] == 0:
                concurrent_landed["n"] += 1
                concurrent = real_load(project_id)
                # Simulate e.g. a rename (api/projects.py) AND a suggestion
                # accept from review.py's findings (api/suggestions.py)
                # landing while judge was mid-flight.
                concurrent["name"] = "renamed-mid-judge"
                concurrent.setdefault("suggestions", []).append(
                    {
                        "id": "concurrent-sugg",
                        "kind": "lost_content",
                        "sentence_ids": ["irrelevant"],
                        "message": "a concurrent request's own finding",
                        "proposed_action": "restore",
                        "status": "accepted",
                        "source": "review",
                        "severity": 3,
                    }
                )
                store.save(concurrent)
            return real_load(project_id)

        finding = _fake_finding("kept_blooper", [3], severity=config.JUDGE_AUTOCUT_SEVERITY)
        empty = _fake_result([])
        agent = _FakeAgent(
            [_fake_result([finding])] * config.JUDGE_RUNS + [empty] * config.JUDGE_RUNS
        )
        with (
            mock.patch("magic_video_editor.pipeline.judge.llm.available", return_value=True),
            mock.patch("magic_video_editor.agents.agents.get_agent", return_value=agent),
            mock.patch("magic_video_editor.store.load", side_effect=load_with_concurrent_write),
        ):
            judge.run(_no_log, project)

        self.assertEqual(concurrent_landed["n"], 1, "the concurrent-write injector must have fired")

        # Judge's own deltas were applied...
        flipped = next(s for s in project["sentences"] if s["id"] == target_id)
        self.assertFalse(flipped["kept"], "judge's own auto-cut must still apply")
        self.assertIsNone(project["edl"], "judge's own edl invalidation must still apply")
        judge_auto_applied = [
            s
            for s in project["suggestions"]
            if s.get("source") == "judge" and s.get("status") == "auto_applied"
        ]
        self.assertEqual(len(judge_auto_applied), 1)

        # ...WITHOUT reverting the concurrent write.
        self.assertEqual(
            project["name"], "renamed-mid-judge", "the concurrent rename must survive judge's save"
        )
        concurrent_ids = [s["id"] for s in project["suggestions"]]
        self.assertIn(
            "concurrent-sugg", concurrent_ids, "the concurrent suggestion must survive judge's save"
        )

        # And on-disk state (re-read with the real, unpatched loader) must
        # match the in-memory `project` judge.run() left behind -- proof the
        # save actually persisted the merge, not just judge's in-memory copy.
        on_disk = real_load(pid)
        self.assertEqual(on_disk["name"], "renamed-mid-judge")
        self.assertFalse(next(s for s in on_disk["sentences"] if s["id"] == target_id)["kept"])
        self.assertIn("concurrent-sugg", [s["id"] for s in on_disk["suggestions"]])
        self.assertIn("judge_autocut_at", on_disk)


if __name__ == "__main__":
    unittest.main()
