#!/usr/bin/env python3
"""Discover and run scripts/test_*.py as separate processes for CI and local `make test`.

Each test script sets MVE_DATA before importing magic_video_editor; they must not share
one interpreter. When COVERAGE=1 (CI), launches each script under coverage with a
per-script data file; combine/report/xml are run from the workflow after this exits.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_GLOB = "test_*.py"
TIMEOUT_SEC = 180


def discover_tests() -> list[Path]:
    scripts_dir = REPO_ROOT / "scripts"
    return sorted(scripts_dir.glob(TEST_GLOB))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Magic Video Editor unittest scripts.")
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated script basenames to skip (emergency use only).",
    )
    return parser.parse_args()


def build_command(script: Path) -> list[str]:
    stem = script.stem
    if os.environ.get("COVERAGE") == "1":
        data_file = REPO_ROOT / f".coverage.{stem}"
        return [
            "uv",
            "run",
            "--with",
            "coverage",
            "coverage",
            "run",
            "--source=magic_video_editor",
            "--parallel-mode",
            f"--data-file={data_file}",
            str(script),
            "-v",
        ]
    return [sys.executable, str(script), "-v"]


def run_script(script: Path) -> tuple[bool, str]:
    """Run one test script; return (passed, detail line for failures)."""
    env = os.environ.copy()
    env["MVE_DATA"] = tempfile.mkdtemp(prefix="mve-ci-")
    cmd = build_command(script)
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            timeout=TIMEOUT_SEC,
            capture_output=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {TIMEOUT_SEC}s (killed)"
    if result.returncode == 0:
        return True, ""
    return False, f"exit code {result.returncode}"


def main() -> int:
    args = parse_args()
    skip = {name.strip() for name in args.skip.split(",") if name.strip()}

    scripts = discover_tests()
    if not scripts:
        print("No scripts/test_*.py found", file=sys.stderr)
        return 1

    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []

    for script in scripts:
        name = script.name
        if name in skip:
            skipped.append(name)
            print(f"SKIP  {name} (--skip)")
            continue

        ok, detail = run_script(script)
        if ok:
            passed.append(name)
            print(f"PASS  {name}")
        else:
            failed.append((name, detail))
            suffix = f" ({detail})" if detail else ""
            print(f"FAIL  {name}{suffix}", file=sys.stderr)

    print()
    print(f"Summary: {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("Failed scripts:", file=sys.stderr)
        for name, detail in failed:
            line = f"  - {name}"
            if detail:
                line += f" ({detail})"
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
