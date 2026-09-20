#!/usr/bin/env python3
"""Mutation suite: the evidence that the gates in scripts/check.sh can fail.

Each mutant in scripts/mutants_catalog.py plants one defect - in a policy
module, an adapter, the driver, a fixture - and must make check.sh fail with
the evidence it names. A mutant check.sh does not catch is a promise nothing
protects.

Every mutant runs in its own throwaway copy of the working tree (the files git
lists, tracked or new, never ignored ones), so the tree itself is never written
to and there is nothing to restore. A baseline copy must pass every gate first,
or no failure could be credited to a mutation, and controls prove that the
suite reports a survivor when there is one.

    python3 scripts/mutants.py [--jobs N] [--only ID-PREFIX]

Standard library only. CI runs every mutant on every push and pull request.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


from mutants_catalog import CATALOG, PY_FAILS, Edit, Mutant, mutant  # noqa: E402


# ----------------------------------------------------------------- the suite


def fail_lines(output: str) -> list[str]:
    """The only lines that count as evidence: a gate's verdict, a case the driver
    reports, or a tool's finding. A diff, an echoed expectation, or a traceback is not."""
    return [
        line for line in output.splitlines() if line.startswith(("  FAIL  ", "FAIL ["))
    ]


def judge(
    m: Mutant, status: int | None, output: str
) -> tuple[bool, list[str], list[str]]:
    """(killed, missing evidence, unwanted evidence). A timeout (status None)
    or a passing run is never a kill, whatever the output says."""
    fails = fail_lines(output)
    missing = [e for e in m.expect if not any(e in f for f in fails)]
    unwanted = [e for e in m.forbid if any(e in f for f in fails)]
    return (status not in (0, None) and not missing and not unwanted), missing, unwanted


def baseline_problems(status: int | None, output: str) -> list[str]:
    lines = [line for line in output.splitlines() if line.strip()]
    problems = []
    if status != 0:
        problems.append(f"check.sh exited {status}")
    problems += [f"a gate failed: {f.strip()}" for f in fail_lines(output)]
    if not lines or lines[-1] != "all gates passed":
        problems.append("check.sh did not end with 'all gates passed'")
    return problems


def catalog_problems(catalog: list[Mutant]) -> list[str]:
    problems, seen = [], set()
    for m in catalog:
        if m.id in seen:
            problems.append(f"{m.id}: the id repeats")
        seen.add(m.id)
        if not m.expect:
            problems.append(f"{m.id}: names no evidence, so any failure would count")
        if not m.edits:
            problems.append(f"{m.id}: changes nothing")
        for edit in m.edits:
            parts = Path(edit.path).parts
            if Path(edit.path).is_absolute() or ".." in parts or not parts:
                problems.append(f"{m.id}: {edit.path} is outside the tree")
    return problems


def planned_texts(m: Mutant, tree: Path) -> tuple[dict[str, str], list[str]]:
    """The full text of every file the mutant changes, or why it cannot apply:
    an anchor must occur exactly once and its replacement must change bytes."""
    texts: dict[str, str] = {}
    problems = []
    for edit in m.edits:
        target = tree / edit.path
        current = texts.get(edit.path)
        if current is None and target.is_file():
            current = target.read_text(encoding="utf-8")
        if edit.old is None:
            if current is not None:
                problems.append(
                    f"{m.id}: {edit.path} already exists, so it cannot be created"
                )
            texts[edit.path] = edit.new
            continue
        if current is None:
            problems.append(f"{m.id}: {edit.path} does not exist")
            continue
        count = current.count(edit.old)
        if count != 1:
            problems.append(
                f"{m.id}: {edit.path}: the anchor occurs {count} times, not once"
            )
            continue
        changed = current.replace(edit.old, edit.new, 1)
        if changed == current:
            problems.append(f"{m.id}: {edit.path}: the edit changes nothing")
        texts[edit.path] = changed
    return texts, problems


def snapshot(image: Path, shared_modules: Path) -> None:
    """Copy the working tree as git sees it, untracked-but-not-ignored files
    included, and give it a private copy of node_modules.

    Cargo's build directory is NOT shared between the copies, although
    sharing it would compile the dependencies once instead of once per
    planted defect, which is most of a run's time. A built binary has one
    name: two copies building `rust-adapter` into one directory overwrite
    each other, and a mutant is then judged on another mutant's binary.
    Measured, and it credited two defects to the wrong mutation."""
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    for rel in filter(None, listed.split("\0")):
        source = ROOT / rel
        if source.is_symlink():
            sys.exit(
                f"mutants: {rel} is a symlink; the suite copies regular files only"
            )
        if not source.is_file():  # deleted in the working tree
            continue
        (image / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, image / rel)
    if (ROOT / "node_modules").is_dir():
        shutil.copytree(ROOT / "node_modules", shared_modules, symlinks=True)
        (image / "node_modules").symlink_to(shared_modules)


_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


def run_check(tree: Path, timeout: float) -> tuple[int | None, str]:
    """check.sh in `tree`, stdout and stderr merged in order; status None on
    timeout, with the whole process group killed."""
    proc = subprocess.Popen(
        ["./scripts/check.sh"],
        cwd=tree,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with _running_lock:
        _running.add(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
        status: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        status = None
    finally:
        with _running_lock:
            _running.discard(proc)
    return status, out.decode("utf-8", errors="replace")


def run_mutant(
    m: Mutant, image: Path, work: Path, timeout: float
) -> tuple[int | None, str]:
    tree = Path(tempfile.mkdtemp(dir=work)) / "tree"
    shutil.copytree(image, tree, symlinks=True)
    try:
        texts, problems = planned_texts(m, tree)
        if problems:  # preflight already passed, so the copy itself is wrong
            return None, "\n".join(problems)
        for rel, text in texts.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text, encoding="utf-8")
        return run_check(tree, timeout)
    finally:
        shutil.rmtree(tree.parent, ignore_errors=True)


# ------------------------------------------------------------------ controls

# A comment changes no behavior: this must survive, or the suite credits
# failures it did not see.
SURVIVOR = mutant(
    "control: a comment changes nothing",
    "ts/checkpoint.ts",
    'import { createHash } from "node:crypto";\n',
    'import { createHash } from "node:crypto";\n// mutation suite: this line changes nothing\n',
    ["  FAIL  "],
)


def control_problems(image: Path) -> list[str]:
    """Checks on the suite's own judgment that need no check.sh run."""
    problems = []
    sample = Mutant(
        "sample",
        (Edit("x", "a", "b"),),
        (PY_FAILS, "FAIL [c] field=s.f"),
        ("(static)",),
    )
    caught = f"{PY_FAILS} 1\nFAIL [c] field=s.f\n"
    for status, output, want, why in [
        (1, caught, True, "the named evidence"),
        (1, "  FAIL  ruff reported issues\n", False, "an unrelated gate failing"),
        (
            2,
            f"{PY_FAILS} 2\nTraceback (most recent call last):\n",
            False,
            "a run that never reached a case",
        ),
        (0, caught, False, "a passing run that prints FAIL text"),
        (None, caught, False, "a timeout"),
        (1, caught + "  FAIL  x (static) y\n", False, "forbidden evidence"),
        (
            1,
            f"{PY_FAILS} 1\n  expected: FAIL [c] field=s.f\n",
            False,
            "an echoed expectation",
        ),
    ]:
        if judge(sample, status, output)[0] != want:
            problems.append(f"judge {'credits' if not want else 'rejects'} {why}")
    for status, output, want in [
        (0, "  PASS  x\nall gates passed\n", True),
        (0, "  FAIL  x\nall gates passed\n", False),
        (1, "all gates passed\n", False),
        (0, "  PASS  x\n", False),
    ]:
        if (not baseline_problems(status, output)) != want:
            problems.append(
                f"baseline check {'accepts' if not want else 'rejects'} {output.strip()!r} exit {status}"
            )
    if len(catalog_problems([sample, sample, Mutant("empty", (), ())])) < 3:
        problems.append("catalog check accepts a repeated id or an empty mutant")
    drifted = mutant(
        "drift", "scripts/check.sh", "an anchor that is nowhere\n", "x", ["x"]
    )
    if not planned_texts(drifted, image)[1]:
        problems.append("an absent anchor is not reported")
    return problems


# ---------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument(
        "--only", default="", help="run only mutants whose id starts with this"
    )
    args = parser.parse_args()

    problems = catalog_problems(CATALOG + [SURVIVOR])
    selected = [m for m in CATALOG if m.id.startswith(args.only)]
    if not selected:
        problems.append(f"no mutant id starts with {args.only!r}")
    if problems:
        print("mutants: the catalog is invalid:\n  " + "\n  ".join(problems))
        return 2

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    root = Path(tempfile.mkdtemp(prefix="haltrule-mutants-"))
    try:
        image = root / "image"
        image.mkdir()
        snapshot(image, root / "node_modules")

        # Every anchor is checked, selected or not: drift must fail loudly.
        problems = control_problems(image)
        for m in CATALOG + [SURVIVOR]:
            problems += planned_texts(m, image)[1]
        if problems:
            print("mutants: nothing ran:\n  " + "\n  ".join(problems))
            return 2

        work = root / "work"
        work.mkdir()
        started = time.monotonic()
        status, output = run_mutant(Mutant("baseline", (), ("-",)), image, work, 600)
        problems = baseline_problems(status, output)
        if problems:
            print(
                "mutants: the unmutated tree does not pass, so no failure can be credited to a mutation:"
            )
            print("  " + "\n  ".join(problems))
            return 1
        timeout = max(120.0, 10 * (time.monotonic() - started))
        print(
            f"baseline: all gates pass ({time.monotonic() - started:.0f}s); running {len(selected)} mutants on {args.jobs} jobs"
        )

        runs = selected + [SURVIVOR]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(
                pool.map(lambda m: run_mutant(m, image, work, timeout), runs)
            )
    except KeyboardInterrupt:
        print("mutants: interrupted; the working tree was never written to")
        return 130
    finally:
        with _running_lock:
            for proc in list(_running):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        shutil.rmtree(root, ignore_errors=True)

    killed = survived = timed_out = 0
    for m, (status, output) in zip(selected, results):
        ok, missing, unwanted = judge(m, status, output)
        if ok:
            killed += 1
            print(f"KILLED    {m.id}")
            continue
        if status is None:
            timed_out += 1
            print(f"TIMED OUT {m.id}")
        else:
            survived += 1
            print(f"SURVIVED  {m.id} (exit {status})")
            for label, items in (("missing", missing), ("unwanted", unwanted)):
                for item in items:
                    print(f"    {label}: {item!r}")
            for line in fail_lines(output)[:8]:
                print(f"    saw: {line.strip()}")
    survivor_status, survivor_output = results[-1]
    control_ok = (
        survivor_status == 0
        and not judge(SURVIVOR, survivor_status, survivor_output)[0]
    )
    if not control_ok:
        print(
            f"CONTROL   {SURVIVOR.id}: expected to survive, got exit {survivor_status}"
        )
        # A harmless edit failed the gates: some gate is not steady. Say which,
        # or the run cannot be told from a defect in the suite itself.
        for line in fail_lines(survivor_output)[:8]:
            print(f"    saw: {line.strip()}")
        for line in survivor_output.splitlines()[-12:]:
            print(f"    tail: {line.rstrip()[:240]}")

    partial = (
        f" (partial: {len(selected)} of {len(CATALOG)})"
        if len(selected) < len(CATALOG)
        else ""
    )
    print(
        f"\nkilled {killed}/{len(selected)}{partial} · survived {survived} · timed out {timed_out}"
        f" · survivor control {'held' if control_ok else 'FAILED'}"
    )
    return 0 if killed == len(selected) and control_ok else 1


if __name__ == "__main__":
    sys.exit(main())
