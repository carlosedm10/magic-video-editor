#!/usr/bin/env python3
"""Eval harness for the take-quality LLM passes (PLATFORM-SPEC v5.6 point 4).

Runs the transcript_cleaner and take_sequencer agents against a labeled
fixture (scripts/eval_fixture.json) built from real project sentences plus a
couple of synthetic cases, for a chosen Ollama --model, and prints
per-category precision/recall. This is how prompt-vs-model changes get
decided with data instead of vibes -- future prompt changes to
TRANSCRIPT_CLEANER_SYSTEM_PROMPT / TAKE_SEQUENCER_SYSTEM_PROMPT must keep
this green.

Usage:
    uv run python scripts/eval_takes.py --model qwen2.5:14b
    uv run python scripts/eval_takes.py --model qwen2.5:32b --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.output import NativeOutput, PromptedOutput  # noqa: E402

from magic_video_editor.agents import agents as agents_mod  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "eval_fixture.json"


def build_agent(task: str, model: str) -> Agent:
    """Build an Agent for `task` pinned to `model`, bypassing settings.py's
    per-task resolution -- same prompt/schema/output-mode construction as
    magic_video_editor.agents.agents.get_agent, just with an explicit model so the eval
    can compare models directly."""
    spec = agents_mod.AGENT_SPECS[task]
    if task in agents_mod._PROMPTED_OUTPUT_TASKS:
        output_type = PromptedOutput(spec["output_type"])
    else:
        output_type = NativeOutput(spec["output_type"])
    return Agent(
        model=agents_mod._model(model),
        system_prompt=spec["prompt"].strip(),
        output_type=output_type,
        model_settings=agents_mod._MODEL_SETTINGS,
        retries=2,
    )


def run_cleaner(agent: Agent, sentences: list[dict]) -> set[int]:
    numbered = "\n".join(f'{s["n"]}: "{s["text"]}"' for s in sentences)
    try:
        result = agent.run_sync(f"Numbered sentences from one clip, in order:\n{numbered}").output
        return {n for n in result.cut_ids if 1 <= n <= len(sentences)}
    except Exception as exc:
        print(f"  ! transcript_cleaner call failed: {exc}", file=sys.stderr)
        return set()


def run_sequencer(agent: Agent, sentences: list[dict]) -> set[int]:
    numbered = "\n".join(f'{s["n"]} ({s["gap"]:.1f}s): "{s["text"]}"' for s in sentences)
    try:
        prompt = (
            "Sliding window of consecutive sentences from one clip "
            f"(id, gap before, text):\n{numbered}"
        )
        result = agent.run_sync(prompt).output
        cut: set[int] = set()
        for run in result.cut_runs:
            a, b = run.start_id, run.end_id
            if a > b:
                a, b = b, a
            for n in range(a, b + 1):
                if 1 <= n <= len(sentences):
                    cut.add(n)
        return cut
    except Exception as exc:
        print(f"  ! take_sequencer call failed: {exc}", file=sys.stderr)
        return set()


def score(predicted: set[int], expected: set[int], universe: set[int]) -> tuple[int, int, int]:
    tp = len(predicted & expected)
    fp = len(predicted - expected)
    fn = len(expected - predicted)
    return tp, fp, fn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="Ollama model tag, e.g. qwen2.5:14b")
    ap.add_argument("--fixture", default=str(FIXTURE_PATH))
    ap.add_argument("-v", "--verbose", action="store_true", help="print each case's decision")
    args = ap.parse_args()

    fixture = json.loads(Path(args.fixture).read_text())
    cases = fixture["cases"]

    cleaner_agent = build_agent("transcript_cleaner", args.model)
    sequencer_agent = build_agent("take_sequencer", args.model)

    per_category: dict[str, list[tuple[int, int, int]]] = {}

    print(f"\n=== eval_takes.py -- model={args.model} -- {len(cases)} case(s) ===\n")

    for case in cases:
        sentences = case["sentences"]
        expected = set(case["expected_cut"])
        universe = {s["n"] for s in sentences}

        cleaner_cut = run_cleaner(cleaner_agent, sentences)
        sequencer_cut = run_sequencer(sequencer_agent, sentences)
        predicted = cleaner_cut | sequencer_cut

        tp, fp, fn = score(predicted, expected, universe)
        per_category.setdefault(case["category"], []).append((tp, fp, fn))

        if args.verbose:
            print(f"[{case['category']}] {case['id']}")
            for s in sentences:
                tag = "CUT-expected" if s["n"] in expected else "keep-expected"
                got = "CUT" if s["n"] in predicted else "keep"
                mark = "OK" if (s["n"] in predicted) == (s["n"] in expected) else "MISS"
                src = []
                if s["n"] in cleaner_cut:
                    src.append("cleaner")
                if s["n"] in sequencer_cut:
                    src.append("sequencer")
                src_s = f" via {'+'.join(src)}" if src else ""
                print(f"  #{s['n']} [{tag} -> got {got}{src_s}] {mark}: {s['text'][:80]!r}")
            print()

    # --- per-category + total table ---
    print(f"{'category':<32} {'TP':>4} {'FP':>4} {'FN':>4} {'precision':>10} {'recall':>8}")
    print("-" * 68)
    total_tp = total_fp = total_fn = 0
    for cat, rows in per_category.items():
        tp = sum(r[0] for r in rows)
        fp = sum(r[1] for r in rows)
        fn = sum(r[2] for r in rows)
        total_tp += tp
        total_fp += fp
        total_fn += fn
        precision = tp / (tp + fp) if (tp + fp) else float("nan")
        recall = tp / (tp + fn) if (tp + fn) else float("nan")
        print(f"{cat:<32} {tp:>4} {fp:>4} {fn:>4} {precision:>10.2f} {recall:>8.2f}")
    print("-" * 68)
    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else float("nan")
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else float("nan")
    print(
        f"{'TOTAL':<32} {total_tp:>4} {total_fp:>4} {total_fn:>4} "
        f"{precision:>10.2f} {recall:>8.2f}"
    )
    print()


if __name__ == "__main__":
    main()
