#!/usr/bin/env python3
"""Completeness guard for the LLM task registry (2026-07-26 root-cause fix):
prevents the exact bug class where a new LLM task (clip_digest,
blooper_reviewer) gets wired into an agent + a pipeline stage but is
forgotten in one of the places that make it safe/configurable:

  1. agents/agents.py AGENT_SPECS       -- the task exists at all.
  2. api/pipeline.py LLM_TASKS_BY_STAGE -- the RAM/installed preflight guard
     (_preflight_stage) actually vets this task's model before the owning
     stage runs. This only applies to tasks invoked from within a
     STAGES-registered stage function (queue kind "stage:*"/"run-all");
     a handful of tasks are called from other queue kinds entirely
     (analyze_clip:* for clip_placement) or synchronously outside the queue
     (copywriter, from api/projects.py) and structurally never go through
     _preflight_stage -- those are the documented NOT_STAGE_DRIVEN set below,
     not a gap.
  3. api/settings.py TASKS + settings.py DEFAULTS['task_models'] -- users can
     set a per-task model override via PUT /api/settings.

Only (2) is asserted for every AGET_SPECS task (minus the documented
exemptions) since that's the guard whose absence caused the original bug (an
oversized model attempting to load, silently un-vetted, on a fail-open code
path). (3) is asserted specifically for clip_digest/blooper_reviewer -- the
two tasks this fix targets -- rather than every task, since several
longstanding stage-driven tasks (video_topic, context_check, take_sequencer,
paragraph_break, reel_composer) are deliberately not user-configurable today;
widening that is a separate, out-of-scope decision.

No pytest in this project's dependency set -- stdlib unittest, same spirit as
scripts/test_thinking_model_tiers.py. No ollama/network/filesystem I/O.

Usage:
    uv run python scripts/test_task_registry_complete.py
    uv run python scripts/test_task_registry_complete.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from magic_video_editor.agents.agents import AGENT_SPECS  # noqa: E402
from magic_video_editor.api.pipeline import LLM_TASKS_BY_STAGE  # noqa: E402
from magic_video_editor.api.settings import TASKS  # noqa: E402
from magic_video_editor.settings import DEFAULTS  # noqa: E402

# Tasks that are real agents but are never invoked from inside a
# STAGES-registered stage function, so LLM_TASKS_BY_STAGE structurally
# cannot cover them (_preflight_stage is only ever called from
# _run_stage_kind/_run_all_kind -- see api/pipeline.py):
#   - clip_placement: runs under the separate "analyze_clip:*" queue kind
#     (pipeline/placement.py, registered by api/suggestions.py's import),
#     triggered per-clip on ingest, never via "stage:*"/"run-all".
#   - copywriter: called synchronously from api/projects.py, outside the
#     queue entirely.
NOT_STAGE_DRIVEN = {"clip_placement", "copywriter"}

# The two tasks this fix specifically makes user-configurable (see
# api/settings.py TASKS and settings.py DEFAULTS['task_models'] for the
# fuller comment). Deliberately NOT "every AGENT_SPECS task" -- see module
# docstring.
NEWLY_CONFIGURABLE_TASKS = {"clip_digest", "blooper_reviewer"}


def _all_preflighted_tasks() -> set[str]:
    tasks: set[str] = set()
    for task_list in LLM_TASKS_BY_STAGE.values():
        tasks.update(task_list)
    return tasks


class TaskRegistryCompletenessTests(unittest.TestCase):
    def test_every_stage_driven_agent_task_is_preflighted(self):
        """Every AGENT_SPECS task that actually runs inside a pipeline stage
        must appear in some LLM_TASKS_BY_STAGE list, or _preflight_stage
        silently never vets its model -- exactly the clip_digest/
        blooper_reviewer bug this fix closes."""
        preflighted = _all_preflighted_tasks()
        stage_driven = set(AGENT_SPECS) - NOT_STAGE_DRIVEN
        missing = stage_driven - preflighted
        self.assertEqual(
            missing,
            set(),
            f"Task(s) {sorted(missing)} run inside a pipeline stage but are "
            "missing from api/pipeline.py's LLM_TASKS_BY_STAGE -- the RAM/"
            "installed preflight guard will never vet their model(s).",
        )

    def test_no_stale_or_unknown_tasks_in_llm_tasks_by_stage(self):
        """Guards the inverse direction: nothing in LLM_TASKS_BY_STAGE names
        a task that doesn't actually exist in AGENT_SPECS (e.g. a rename
        that forgot to update this table)."""
        preflighted = _all_preflighted_tasks()
        unknown = preflighted - set(AGENT_SPECS)
        self.assertEqual(
            unknown,
            set(),
            f"LLM_TASKS_BY_STAGE references unknown task(s) {sorted(unknown)} "
            "not present in AGENT_SPECS.",
        )

    def test_clip_digest_and_blooper_reviewer_are_preflighted(self):
        """Pins down exactly where each of the two new tasks must be
        preflighted, not just that they're covered somewhere."""
        self.assertIn("clip_digest", LLM_TASKS_BY_STAGE.get("order", []))
        self.assertIn("blooper_reviewer", LLM_TASKS_BY_STAGE.get("takes", []))

    def test_clip_digest_and_blooper_reviewer_are_user_configurable(self):
        """Users must be able to set a per-task model override for both new
        tasks via PUT /api/settings (api/settings.py TASKS) and both must
        have a settings.json default (settings.py DEFAULTS['task_models'])."""
        for task in NEWLY_CONFIGURABLE_TASKS:
            self.assertIn(task, AGENT_SPECS, f"{task} is not a real AGENT_SPECS task")
            self.assertIn(
                task, TASKS, f"{task} missing from api/settings.py TASKS"
            )
            self.assertIn(
                task,
                DEFAULTS["task_models"],
                f"{task} missing from settings.py DEFAULTS['task_models']",
            )
            self.assertIsNone(
                DEFAULTS["task_models"][task],
                f"{task}'s DEFAULTS['task_models'] entry should be None "
                "(falls back to default_model until a user overrides it)",
            )

    def test_every_settings_task_is_a_real_agent(self):
        """Inverse guard for api/settings.py TASKS: every name it exposes
        for override must correspond to a real AGENT_SPECS task."""
        unknown = set(TASKS) - set(AGENT_SPECS)
        self.assertEqual(
            unknown,
            set(),
            f"api/settings.py TASKS references unknown task(s) {sorted(unknown)}",
        )

    def test_every_task_models_default_is_a_real_agent(self):
        """Inverse guard for settings.py DEFAULTS['task_models']: every key
        must correspond to a real AGENT_SPECS task."""
        unknown = set(DEFAULTS["task_models"]) - set(AGENT_SPECS)
        self.assertEqual(
            unknown,
            set(),
            "settings.py DEFAULTS['task_models'] references unknown task(s) "
            f"{sorted(unknown)}",
        )


if __name__ == "__main__":
    unittest.main()
