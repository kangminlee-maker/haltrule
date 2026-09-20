"""The off-the-shelf mutation tools, held to one list.

StrykerJS mutates the TypeScript modules and cosmic-ray the Python ones; each runs the shared driver as its
only test (stryker.config.json, cosmic-ray.toml). Whatever survives must be, entry for entry, what
scripts/survivors_accepted.json lists with a reason: a survivor that is not listed is a case the fixtures
lack or code that changes nothing, and a listed one that no longer survives is a stale entry. Both fail.

The tools run on a copy of the tree: cosmic-ray mutates files where they lie.

  survivors.py ts | py       run the tool on a copy, compare
  survivors.py self-test     the comparison itself can fail

Standard library only; the tools are found in node_modules/.bin and, for cosmic-ray, .venv/bin or the PATH.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mutants import ROOT, snapshot

ACCEPTED = ROOT / "scripts" / "survivors_accepted.json"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def stryker_survivors(report: dict) -> tuple[list[str], int]:
    """(survivors, mutants run) from a mutation-testing-elements report."""
    found, total = [], 0
    for name, data in sorted(report["files"].items()):
        lines = data["source"].split("\n")
        for mutant in data["mutants"]:
            if mutant["status"] in ("Ignored", "CompileError", "RuntimeError"):
                continue
            total += 1
            if mutant["status"] not in ("Survived", "NoCoverage"):
                continue  # Killed, or Timeout: the run noticed
            start, end = mutant["location"]["start"], mutant["location"]["end"]
            span = lines[start["line"] - 1 : end["line"]]
            span[-1] = span[-1][: end["column"] - 1]
            span[0] = span[0][start["column"] - 1 :]
            original, replacement = (
                _one_line("\n".join(span)),
                _one_line(mutant.get("replacement", "")),
            )
            found.append(
                f"{name} | {mutant['mutatorName']} | {original} -> {replacement}"
            )
    return found, total


def cosmic_ray_survivors(dump: str) -> tuple[list[str], int]:
    """(survivors, mutants run) from `cosmic-ray dump`: one [work item, result] per line."""
    found, total = [], 0
    for line in filter(None, dump.splitlines()):
        item, result = json.loads(line)
        if result is None:
            raise SystemExit("survivors: cosmic-ray left a mutant unrun")
        total += 1
        if result["test_outcome"] != "survived":
            continue  # killed, or incompetent: the mutant did not even load
        mutation = item["mutations"][0]
        changed = [row for row in result["diff"].splitlines() if row[:1] in "-+"]
        before = [row[1:] for row in changed if row[0] == "-" and row[:3] != "---"]
        after = [row[1:] for row in changed if row[0] == "+" and row[:3] != "+++"]
        found.append(
            f"{mutation['module_path']} | {mutation['operator_name'].removeprefix('core/')}"
            f" | {_one_line(' '.join(before))} -> {_one_line(' '.join(after))}"
        )
    return found, total


def compare(found: list[str], total: int, accepted: list[str]) -> list[str]:
    """Problems, as FAIL lines: both lists are multisets and must be equal."""
    if total == 0:
        return ["FAIL [no mutants] the tool ran nothing, so nothing was shown"]
    have, want = collections.Counter(found), collections.Counter(accepted)
    return [
        f"FAIL [new survivor] {entry}" for entry in sorted((have - want).elements())
    ] + [f"FAIL [stale entry] {entry}" for entry in sorted((want - have).elements())]


def read_accepted(prefix: str) -> list[str]:
    entries = json.loads(ACCEPTED.read_text(encoding="utf-8"))
    for entry in entries:
        if sorted(entry) != ["survivor", "why"] or not entry["why"].strip():
            raise SystemExit(
                f"survivors: every accepted entry is a survivor and why: {entry}"
            )
    return [e["survivor"] for e in entries if e["survivor"].startswith(prefix)]


def _run(command: list[str], tree: Path, env: dict[str, str]) -> str:
    done = subprocess.run(command, cwd=tree, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(
            f"survivors: {' '.join(command)} exited {done.returncode}\n{done.stdout[-2000:]}{done.stderr[-2000:]}"
        )
    return done.stdout


def run_tool(language: str) -> tuple[list[str], int]:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [str(ROOT / ".venv" / "bin"), str(ROOT / "node_modules" / ".bin"), env["PATH"]]
    )
    with tempfile.TemporaryDirectory(prefix="haltrule-survivors-") as tmp:
        tree = Path(tmp) / "tree"
        snapshot(tree, Path(tmp) / "node_modules")
        if language == "ts":
            _run(["stryker", "run", "stryker.config.json"], tree, env)
            report = json.loads((tree / "reports" / "stryker.json").read_text("utf-8"))
            return stryker_survivors(report)
        _run(["cosmic-ray", "init", "cosmic-ray.toml", "session.sqlite"], tree, env)
        _run(["cosmic-ray", "baseline", "cosmic-ray.toml"], tree, env)
        _run(["cosmic-ray", "exec", "cosmic-ray.toml", "session.sqlite"], tree, env)
        return cosmic_ray_survivors(
            _run(["cosmic-ray", "dump", "session.sqlite"], tree, env)
        )


def self_test() -> list[str]:
    """The comparison must fail for a new survivor, a stale entry, a repeat, and an empty run."""
    report = {
        "files": {
            "ts/a.ts": {
                "source": "const a = 1;\nif (a >\n  0) run();\n",
                "mutants": [
                    {
                        "status": "Killed",
                        "mutatorName": "X",
                        "replacement": "2",
                        "location": {
                            "start": {"line": 1, "column": 11},
                            "end": {"line": 1, "column": 12},
                        },
                    },
                    {
                        "status": "Survived",
                        "mutatorName": "EqualityOperator",
                        "replacement": "a >= 0",
                        "location": {
                            "start": {"line": 2, "column": 5},
                            "end": {"line": 3, "column": 4},
                        },
                    },
                    {
                        "status": "Ignored",
                        "mutatorName": "StringLiteral",
                        "replacement": '""',
                        "location": {
                            "start": {"line": 1, "column": 1},
                            "end": {"line": 1, "column": 2},
                        },
                    },
                ],
            }
        }
    }
    ts_entry = "ts/a.ts | EqualityOperator | a > 0 -> a >= 0"
    item = {
        "mutations": [
            {"module_path": "py/a.py", "operator_name": "core/NumberReplacer"}
        ]
    }
    diff = "--- mutation diff ---\n--- a/py/a.py\n+++ b/py/a.py\n@@ -1 +1 @@\n-    x = 1\n+    x = 2\n"
    dump = "\n".join(
        json.dumps([item, {"test_outcome": outcome, "diff": diff}])
        for outcome in ("killed", "survived", "incompetent")
    )
    py_entry = "py/a.py | NumberReplacer | x = 1 -> x = 2"
    problems = []

    def expect(what: str, got, wanted) -> None:
        if got != wanted:
            problems.append(f"FAIL [self-test] {what}: got {got!r}, wanted {wanted!r}")

    expect("a stryker report is read", stryker_survivors(report), ([ts_entry], 2))
    expect("a cosmic-ray dump is read", cosmic_ray_survivors(dump), ([py_entry], 3))
    expect("the listed survivors pass", compare([ts_entry], 2, [ts_entry]), [])
    expect(
        "a new survivor fails",
        compare([ts_entry, py_entry], 5, [ts_entry]),
        [f"FAIL [new survivor] {py_entry}"],
    )
    expect(
        "a stale entry fails",
        compare([], 5, [ts_entry]),
        [f"FAIL [stale entry] {ts_entry}"],
    )
    expect(
        "a second survivor that looks the same fails",
        compare([ts_entry, ts_entry], 5, [ts_entry]),
        [f"FAIL [new survivor] {ts_entry}"],
    )
    expect(
        "a run of nothing fails",
        compare([], 0, [])[:1],
        ["FAIL [no mutants] the tool ran nothing, so nothing was shown"],
    )
    return problems


def main(argv: list[str]) -> int:
    if argv == ["self-test"]:
        problems = self_test()
    elif argv in (["ts"], ["py"]):
        found, total = run_tool(argv[0])
        problems = compare(found, total, read_accepted(argv[0] + "/"))
        print(
            f"{total} mutants, {len(found)} survived, {len(found) - sum(p.startswith('FAIL [new') for p in problems)} of them listed"
        )
    else:
        print(__doc__)
        return 2
    for problem in problems:
        print(problem)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
