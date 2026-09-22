#!/usr/bin/env python3
"""The bridge a mutation tool needs for the Python shell program.

A mutation tool runs a test, and a test that starts 465 processes takes longer
than the tool is worth: this makes the same calls in this process. It holds no
expectation of its own - scripts/conform.py says which calls to make
(`cli-calls`) and judges what came back (`cli-judge`), as it judges the calls
made through processes in scripts/check.sh. Nothing but a mutation run needs it.

HALTRULE_ROOT says where the driver and the fixtures are, for a run that works
in a copy of the tree.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(os.environ.get("HALTRULE_ROOT", Path(__file__).resolve().parent.parent))
DRIVER = [sys.executable, str(ROOT / "scripts" / "conform.py")]

sys.path.insert(0, str(ROOT / "py"))
import cli  # noqa: E402  (the program under test, mutated where it lies)


def answer(entry: str, text: str) -> tuple[str, int]:
    """What the program writes and what it exits with, for one call. The
    arguments reach it as a caller's would, through the standard input."""
    written, said = io.StringIO(), io.StringIO()
    stdin = sys.stdin
    sys.stdin = types.SimpleNamespace(
        buffer=io.BytesIO(text.encode("utf-8", "surrogatepass")),
        isatty=lambda: False,
    )
    try:
        with redirect_stdout(written), redirect_stderr(said):
            code = cli.main([entry, "-"])
    finally:
        sys.stdin = stdin
    return written.getvalue(), code


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="haltrule-cli-bridge-") as held:
        calls = Path(held) / "calls.jsonl"
        subprocess.run(
            [*DRIVER, "cli-calls", str(calls)],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        written = Path(held) / "answers.jsonl"
        with written.open("w", encoding="utf-8") as lines:
            for line in calls.read_text(encoding="ascii").splitlines():
                call = json.loads(line)
                out, code = answer(call["entry"], call["input"])
                lines.write(
                    json.dumps({"id": call["id"], "out": out, "exit": code}) + "\n"
                )
        judged = subprocess.run(
            [*DRIVER, "cli-judge", "--every-input", str(written)], cwd=ROOT
        )
        if judged.returncode != 0:
            return judged.returncode
    # What the program promises beside answering a case: asked through itself as a process,
    # because that is where its arguments and its exits are.
    probes = subprocess.run(
        [*DRIVER, "cli-probes", sys.executable, str(ROOT / "py" / "cli.py")], cwd=ROOT
    )
    return probes.returncode


if __name__ == "__main__":
    sys.exit(main())
