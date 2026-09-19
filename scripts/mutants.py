#!/usr/bin/env python3
"""Mutation suite: the evidence that the gates in scripts/check.sh can fail.

Each mutant breaks one promise - in an implementation, a runner, a fixture, or
check.sh itself - and must make check.sh fail with the evidence it names: FAIL
lines saying which gate caught it and, when a runner did, which case and field.
A mutant that check.sh does not catch is a promise nothing protects.

Every mutant runs in its own throwaway copy of the working tree (the files git
lists, tracked or new, never ignored ones), so the tree itself is never written
to and there is nothing to restore. A baseline copy must pass every gate first,
or no failure could be credited to a mutation, and controls prove that the
suite reports a survivor when there is one.

    python3 scripts/mutants.py [--jobs N] [--only ID-PREFIX] [--shard I/N]

Standard library only. CI runs every mutant on every push and pull request.
"""

from __future__ import annotations

import argparse
import re
import concurrent.futures
import dataclasses
import json
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


@dataclasses.dataclass(frozen=True)
class Edit:
    path: str
    old: str | None  # None creates `path`, which must not exist yet
    new: str


@dataclasses.dataclass(frozen=True)
class Mutant:
    id: str
    edits: tuple[Edit, ...]
    expect: tuple[str, ...]  # each must appear in some FAIL line
    forbid: tuple[str, ...] = ()  # none may appear in any FAIL line


def mutant(id: str, path: str, old: str, new: str, expect, forbid=()) -> Mutant:
    return Mutant(id, (Edit(path, old, new),), tuple(expect), tuple(forbid))


def _copied_breaker_fixture() -> str:
    """breaker/v0.json with its ids kept and its first case made wrong."""
    doc = json.loads((ROOT / "fixtures/breaker/v0.json").read_text(encoding="utf-8"))
    doc["classify"][0]["message"], doc["classify"][0]["expect"] = (
        "plain success",
        "WRONG",
    )
    return json.dumps(doc) + "\n"


TS_RUNNER_FAILS = "  FAIL  typescript runner exited"
# The global object, reached through a getter on Object.prototype: no forbidden
# name is written and no code is built from a string, so only the sealed run
# can see what is taken from it.
TS_GLOBAL_REACH = (
    "declare const haltruleReach: { Date: { now(): number }; Function: (code: string) => () => number;"
    " process: { pid: number; env: Record<string, string | undefined> } };\n"
    'Object.defineProperty(Object.prototype, "haltruleReach", { get() { return this; }, configurable: true });\n'
)
PY_RUNNER_FAILS = "  FAIL  python runner exited"

CATALOG: list[Mutant] = []  # filled below


# ----------------------------------------------------------------- the suite


def fail_lines(output: str) -> list[str]:
    """The only lines that count as evidence: a gate's verdict or a runner's
    mismatch receipt. A diff, an echoed expectation, or a traceback is not."""
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
    problems += [
        f"a gate was skipped: {s.strip()}" for s in lines if s.startswith("  SKIP  ")
    ]
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
    included, and give it a private copy of node_modules."""
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
        (PY_RUNNER_FAILS, "FAIL [c] field=s.f"),
        ("(static)",),
    )
    caught = f"{PY_RUNNER_FAILS} 1\nFAIL [c] field=s.f\n"
    for status, output, want, why in [
        (1, caught, True, "the named evidence"),
        (1, "  FAIL  ruff reported issues\n", False, "an unrelated gate failing"),
        (
            2,
            f"{PY_RUNNER_FAILS} 2\nTraceback (most recent call last):\n",
            False,
            "a runner that never ran a case",
        ),
        (0, caught, False, "a passing run that prints FAIL text"),
        (None, caught, False, "a timeout"),
        (1, caught + "  FAIL  x (static) y\n", False, "forbidden evidence"),
        (
            1,
            f"{PY_RUNNER_FAILS} 1\n  expected: FAIL [c] field=s.f\n",
            False,
            "an echoed expectation",
        ),
    ]:
        if judge(sample, status, output)[0] != want:
            problems.append(f"judge {'credits' if not want else 'rejects'} {why}")
    for status, output, want in [
        (0, "  PASS  x\nall gates passed\n", True),
        (0, "  SKIP  x (y)\nall gates passed\n", False),
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


# -------------------------------------------------------------------- shards


def parse_shard(text: str) -> tuple[int, int] | None:
    """I/N with 1 <= I <= N - the one place this grammar is read."""
    found = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", text)
    if not found or int(found[1]) > int(found[2]):
        return None
    return int(found[1]), int(found[2])


def named_shards_problems(entries: list[str]) -> tuple[int, list[str]]:
    """The shard count the entries share, and why they are not 1/N..N/N once each."""
    parsed = [parse_shard(entry) for entry in entries]
    if not entries or not all(parsed):
        return 0, [f"shard entries are not all I/N with 1 <= I <= N: {entries}"]
    counts = sorted({count for _, count in parsed})
    if len(counts) != 1:
        return 0, [f"shard entries name more than one N: {entries}"]
    if sorted(index for index, _ in parsed) != list(range(1, counts[0] + 1)):
        return counts[0], [f"shard entries are not 1/{counts[0]}..{counts[0]}/{counts[0]} once each: {entries}"]
    return counts[0], []


def shard_slices(items: list, count: int) -> list[list]:
    """The `count` interleaved slices: slice i holds items i, i + count, ..."""
    return [items[first::count] for first in range(count)]


def shard_problems(items: list[Mutant], count: int) -> list[str]:
    """Empty when the slices hold every item exactly once. Each CI job sees
    only its own slice, so nothing else would notice one that fell between
    two of them or landed in both."""
    held = sorted(m.id for piece in shard_slices(items, count) for m in piece)
    wanted = sorted(m.id for m in items)
    if held == wanted:
        return []
    return [
        f"the {count} shards hold {len(held)} mutants ({len(set(held))} distinct), not each of the {len(wanted)} exactly once"
    ]


# ---------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument(
        "--only", default="", help="run only mutants whose id starts with this"
    )
    parser.add_argument(
        "--shard",
        default="1/1",
        help="run slice I of N of the selection (every N-th mutant from the I-th), as I/N; the slices of one N cover it exactly once",
    )
    parser.add_argument(
        "--shards-control",
        default="",
        metavar="1/N,...,N/N",
        help="run nothing: check that these shard entries are 1/N..N/N once each and that the N shards hold every catalog mutant exactly once",
    )
    args = parser.parse_args()

    if args.shards_control:
        problems = catalog_problems(CATALOG + [SURVIVOR])
        count, named = named_shards_problems(args.shards_control.split(","))
        problems += named
        if count and not named:
            problems += shard_problems(CATALOG, count)
        if problems:
            print("mutants: " + "\n  ".join(problems))
            return 2
        print(
            f"mutants: the {count} named shards hold each of the {len(CATALOG)} catalog mutants exactly once"
        )
        return 0

    problems = catalog_problems(CATALOG + [SURVIVOR])
    shard = parse_shard(args.shard)
    shard_index, shard_count = shard or (1, 1)
    if not shard:
        problems.append(f"--shard wants I/N with 1 <= I <= N, got {args.shard!r}")
    matching = [m for m in CATALOG if m.id.startswith(args.only)]
    # check.sh (gate 9) runs this control over the whole catalog; here it
    # covers the selection actually being split, which --only can narrow.
    problems += shard_problems(matching, shard_count)
    selected = shard_slices(matching, shard_count)[shard_index - 1] if shard else []
    if not matching:
        problems.append(f"no mutant id starts with {args.only!r}")
    elif not selected:
        problems.append(f"shard {args.shard} of {len(matching)} mutants is empty")
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

    shard_note = f", shard {shard_index} of {shard_count}" if shard_count > 1 else ""
    partial = (
        f" (partial: {len(selected)} of {len(CATALOG)}{shard_note})"
        if len(selected) < len(CATALOG)
        else ""
    )
    print(
        f"\nkilled {killed}/{len(selected)}{partial} · survived {survived} · timed out {timed_out}"
        f" · survivor control {'held' if control_ok else 'FAILED'}"
    )
    return 0 if killed == len(selected) and control_ok else 1


# ------------------------------------------------------------------- catalog
# Each mutant names the promise it breaks. Its evidence is the FAIL line of the
# gate that states that promise and, where a runner catches it, the runner's
# receipt for the case and field that pin it.

CATALOG += [
    mutant(
        "py canonicalize: map keys in UTF-16 order, not code point order",
        "py/haltrule/checkpoint.py",
        "keys = sorted(value, key=_utf16_key)",
        "keys = sorted(value)",
        [
            PY_RUNNER_FAILS,
            "FAIL [key_order_non_bmp_before_private_use] field=canonicalize.expect",
            "FAIL [key_order_non_bmp_before_ffff] field=canonicalize.expect",
        ],
    ),
    mutant(
        "py canonicalize: a fractional float halts, never truncated",
        "py/haltrule/checkpoint.py",
        "not math.isfinite(value) or not value.is_integer()",
        "not math.isfinite(value)",
        [PY_RUNNER_FAILS, "FAIL [fraction] field=canonicalize.expect"],
    ),
    mutant(
        "py canonicalize: booleans are not integers",
        "py/haltrule/checkpoint.py",
        '    if value is True:\n        return "true"\n    if value is False:\n        return "false"\n    if isinstance(value, (int, float)) and not isinstance(value, bool):\n        return _encode_number(value, at)\n',
        '    if isinstance(value, (int, float)):\n        return _encode_number(value, at)\n    if value is True:\n        return "true"\n    if value is False:\n        return "false"\n',
        [PY_RUNNER_FAILS, "FAIL [booleans_are_not_integers] field=canonicalize.expect"],
    ),
    mutant(
        "py checkpoint: a missing status follows JavaScript falsiness",
        "py/haltrule/checkpoint.py",
        "    if _js_falsy(status):",
        "    if not status:",
        [
            PY_RUNNER_FAILS,
            "FAIL [status_empty_list_is_present_not_reusable] field=checkpoint.expect",
        ],
    ),
    mutant(
        "py checkpoint: dependencies checked in UTF-16 order, not insertion order",
        "py/haltrule/checkpoint.py",
        "for dependency_id in sorted(expected_dependencies, key=_utf16_key):",
        "for dependency_id in expected_dependencies:",
        [
            PY_RUNNER_FAILS,
            "FAIL [dependency_mismatches_in_utf16_key_order] field=checkpoint.expect",
        ],
    ),
    mutant(
        "ts canonicalize: 2^53 - 1 itself is in range",
        "ts/checkpoint.ts",
        "Math.abs(value) > MAX_SAFE_INTEGER",
        "Math.abs(value) >= MAX_SAFE_INTEGER",
        [
            TS_RUNNER_FAILS,
            "FAIL [max_safe_integer] field=canonicalize.expect",
            "FAIL [min_safe_integer] field=canonicalize.expect",
        ],
    ),
    mutant(
        "ts canonicalize: a slash is written as itself",
        "ts/checkpoint.ts",
        '  0x5c: "\\\\\\\\",\n',
        '  0x5c: "\\\\\\\\",\n  0x2f: "\\\\/",\n',
        [TS_RUNNER_FAILS, "FAIL [no_escape_slash_and_html] field=canonicalize.expect"],
    ),
    mutant(
        "ts checkpoint: dependencies checked in UTF-16 order, not insertion order",
        "ts/checkpoint.ts",
        "Object.keys(expectedDependencies).sort()",
        "Object.keys(expectedDependencies)",
        [
            TS_RUNNER_FAILS,
            "FAIL [dependency_mismatches_in_utf16_key_order] field=checkpoint.expect",
        ],
    ),
    mutant(
        "ts checkpoint: inherited properties are not recorded digests",
        "ts/checkpoint.ts",
        "isPlainObject(recordedDependencies) && Object.hasOwn(recordedDependencies, dependencyId)",
        "isPlainObject(recordedDependencies)",
        [TS_RUNNER_FAILS, "FAIL [dependency_id_constructor] field=checkpoint.expect"],
    ),
    mutant(
        "ts digest: sha256 over exactly the canonical form",
        "ts/checkpoint.ts",
        'update(result.canonical, "utf8")',
        'update(result.canonical + " ", "utf8")',
        [
            TS_RUNNER_FAILS,
            "FAIL [null] field=canonicalize.expect",
            "digests differ from sha",
        ],
    ),
    mutant(
        "ts purity: no file-system import in a policy file",
        "ts/checkpoint.ts",
        'import { createHash } from "node:crypto";',
        'import { createHash } from "node:crypto";\nimport { readFileSync } from "node:fs";\nvoid readFileSync;',
        ["ts/checkpoint.ts has import(s)/require(s)/re-export(s) beyond"],
    ),
    mutant(
        "py purity: no clock import in a policy file",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\nimport time  # noqa: F401\n",
        ["py/haltrule/checkpoint.py (static)", "policy code imported 'time'"],
    ),
    mutant(
        "py canonicalize: a non-string map key halts",
        "py/haltrule/checkpoint.py",
        '        for key in value:\n            if not isinstance(key, str):\n                raise _DigestInputError(\n                    "digest_input_unsupported", f"{at}: map key {key!r} is not a string"\n                )\n',
        "",
        [
            PY_RUNNER_FAILS,
            "FAIL [non_string_key] field=canonicalize.raised",
            "FAIL [non_string_key_nested] field=canonicalize.raised",
        ],
    ),
    mutant(
        "ts canonicalize: a symbol map key halts",
        "ts/checkpoint.ts",
        '    if (Object.getOwnPropertySymbols(value).length > 0) {\n      throw new DigestInputError("digest_input_unsupported", `${at}: map has a symbol key`);\n    }\n',
        "",
        [
            TS_RUNNER_FAILS,
            "FAIL [non_string_key] field=canonicalize.expect",
            "FAIL [non_string_key_nested] field=canonicalize.expect",
        ],
    ),
    mutant(
        "py checkpoint: a falsy recorded revision reads as absent",
        "py/haltrule/checkpoint.py",
        "_js_falsy(expected_contract_revision) and _js_falsy(revision):",
        "_js_falsy(expected_contract_revision) and revision is None:",
        [
            PY_RUNNER_FAILS,
            "FAIL [contract_revision_empty_string_is_missing] field=checkpoint.expect",
            "FAIL [contract_revision_zero_is_missing] field=checkpoint.expect",
            "FAIL [contract_revision_false_is_missing] field=checkpoint.expect",
        ],
    ),
    mutant(
        "ts checkpoint: a falsy recorded revision reads as absent",
        "ts/checkpoint.ts",
        "if (expectedRevision && !artifact.contract_revision) {",
        "if (expectedRevision && artifact.contract_revision == null) {",
        [
            TS_RUNNER_FAILS,
            "FAIL [contract_revision_empty_string_is_missing] field=checkpoint.expect",
            "FAIL [contract_revision_zero_is_missing] field=checkpoint.expect",
            "FAIL [contract_revision_false_is_missing] field=checkpoint.expect",
        ],
    ),
    mutant(
        "py checkpoint: a falsy expected config digest is not compared",
        "py/haltrule/checkpoint.py",
        "    if not _js_falsy(expected_stage_config_digest) and not _js_strict_equal(",
        "    if expected_stage_config_digest is not None and not _js_strict_equal(",
        [
            PY_RUNNER_FAILS,
            "FAIL [empty_expected_stage_config_skips_the_check] field=checkpoint.expect",
        ],
    ),
    mutant(
        "ts checkpoint: a falsy expected config digest is not compared",
        "ts/checkpoint.ts",
        "if (expectedConfig && artifact.stage_config_digest !== expectedConfig) {",
        "if (expectedConfig != null && artifact.stage_config_digest !== expectedConfig) {",
        [
            TS_RUNNER_FAILS,
            "FAIL [empty_expected_stage_config_skips_the_check] field=checkpoint.expect",
        ],
    ),
    mutant(
        "py checkpoint: dependency ids in UTF-16 order, not code point order",
        "py/haltrule/checkpoint.py",
        "for dependency_id in sorted(expected_dependencies, key=_utf16_key):",
        "for dependency_id in sorted(expected_dependencies):",
        [
            PY_RUNNER_FAILS,
            "FAIL [dependency_order_utf16_not_code_point] field=checkpoint.expect",
        ],
    ),
    mutant(
        "ts canonicalize: a bigint in range is an integer",
        "ts/checkpoint.ts",
        'if (typeof value === "bigint") return encodeBigInt(value, at);\n',
        "",
        [
            TS_RUNNER_FAILS,
            "FAIL [bigint_small] field=canonicalize.expect",
            "FAIL [bigint_max_safe] field=canonicalize.expect",
        ],
    ),
    mutant(
        "py canonicalize: nesting past 100 halts",
        "py/haltrule/checkpoint.py",
        "if isinstance(value, (list, dict)) and depth >= _MAX_DEPTH:",
        "if False:",
        [
            PY_RUNNER_FAILS,
            "FAIL [depth_101_lists] field=canonicalize.expect",
            "FAIL [depth_101_maps] field=canonicalize.expect",
        ],
    ),
    mutant(
        "ts canonicalize: nesting of 101 halts",
        "ts/checkpoint.ts",
        "&& depth >= MAX_DEPTH) {",
        "&& depth > MAX_DEPTH) {",
        [
            TS_RUNNER_FAILS,
            "FAIL [depth_101_lists] field=canonicalize.expect",
            "FAIL [depth_101_maps] field=canonicalize.expect",
        ],
    ),
    mutant(
        "ts runner: an unknown fixture section is refused",
        "ts/run-fixtures.ts",
        'if (key !== "fixture_version" && !known.includes(key)) {',
        "if (false) {",
        ["typescript did not refuse a fixture section no runner reads"],
    ),
    mutant(
        "fixtures: the inventory holds every file the gates name",
        "scripts/check.sh",
        "list_files fixture_files fixtures -type f -name '*.json'\n",
        "list_files fixture_files fixtures -type f -name '*.json' ! -path '*slot*'\n",
        ["the fixture inventory (4 files) is missing: fixtures/slot/v0.json"],
    ),
    mutant(
        "fixtures: every fixture file is ASCII",
        "fixtures/checkpoint/v0.json",
        '"id": "hangul_nfc"',
        '"id": "hangul_nfc_가"',
        ["fixture files with non-ASCII bytes: fixtures/checkpoint/v0.json"],
    ),
    mutant(
        "ts determinism: no clock read in a policy file",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        "export const loadedAt = performance.now();\nexport function canonicalize(value: unknown)",
        ["ts/checkpoint.ts (static)", "policy code used performance.now"],
    ),
    mutant(
        "py purity: no file I/O through pathlib",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\nimport pathlib\n\n_SELF = pathlib.Path(__file__).read_text()\n",
        ["py/haltrule/checkpoint.py (static)", "policy code imported 'pathlib'"],
    ),
    mutant(
        "ts runner: an unknown $unsupported kind is refused",
        "ts/run-fixtures.ts",
        "      throw new Error(`unknown $unsupported kind ${JSON.stringify(inner)}`);",
        "      return new Map();",
        ["typescript did not refuse an $unsupported kind no runner builds"],
    ),
    mutant(
        "py runner: an unknown $unsupported kind is refused",
        "py/run_fixtures.py",
        '            raise ValueError(f"unknown $unsupported kind {inner!r}")',
        "            return object()",
        ["python did not refuse an $unsupported kind no runner builds"],
    ),
    mutant(
        "ts runner: $bigint decodes to a bigint",
        "ts/run-fixtures.ts",
        "const decoded: unknown = BigInt(inner);",
        "const decoded: unknown = Number(inner);",
        [TS_RUNNER_FAILS, "FAIL [bigint_small] field=canonicalize.raised"],
    ),
    mutant(
        "py determinism: open reached through an alias",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\n\n_read = open\n_SOURCE = _read(__file__).read()\n",
        ["py/haltrule/checkpoint.py (static)", "policy code used open"],
    ),
    mutant(
        "ts determinism: Math.random reached by destructuring",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        "const { random } = Math;\nexport const SALT = random();\nexport function canonicalize(value: unknown)",
        ["ts/checkpoint.ts (static)", "policy code used Math.random"],
    ),
    mutant(
        "ts determinism: a clock read the static scan cannot see",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        # the global object, reached through a getter: no forbidden name is
        # written and no code is built from a string
        TS_GLOBAL_REACH
        + 'export const loadedAt = haltruleReach.Date.now();\nexport function canonicalize(value: unknown)',
        ["policy code used Date.now"],
        ["(static)"],
    ),
    mutant(
        "check.sh: the TypeScript sealed run carries the seal",
        "scripts/check.sh",
        'ts_sealed_out=$(node --import "$ts_seal" ts/run-fixtures.ts 2>&1)',
        "ts_sealed_out=$(node ts/run-fixtures.ts 2>&1)",
        ["typescript sealed run carries no seal marker"],
    ),
    mutant(
        "check.sh: the TypeScript seal replaces Date",
        "scripts/check.sh",
        "globalThis.Date = SealedDate;\n",
        "",
        ["typescript sealed run carries no seal marker"],
    ),
    mutant(
        "check.sh: Python policy modules load with the sealed builtins",
        "scripts/check.sh",
        '    module.__dict__["__builtins__"] = SEALED\n',
        "",
        ["python sealed run carries no seal marker"],
    ),
    mutant(
        "py determinism: no hash() or id() draw",
        "py/haltrule/checkpoint.py",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n    _nd = hash(at) ^ id(value)  # noqa: F841\n",
        ["py/haltrule/checkpoint.py (static)", "policy code used hash"],
    ),
    mutant(
        "py determinism: no repr() address draw",
        "py/haltrule/checkpoint.py",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n    _nd = repr(object())  # noqa: F841\n",
        ["py/haltrule/checkpoint.py (static)", "policy code used repr"],
    ),
    mutant(
        "py determinism: no hash-seeded set order in output",
        "py/haltrule/checkpoint.py",
        "for dependency_id in sorted(expected_dependencies, key=_utf16_key):",
        "for dependency_id in list(set(expected_dependencies)):",
        ["python --dump differs by hash seed"],
    ),
    mutant(
        "ts canonicalize: integer-like keys in UTF-16 order",
        "ts/checkpoint.ts",
        "    const keys = Object.keys(value).sort();",
        "    const keys = Object.keys(Object.fromEntries(Object.entries(value).sort()));",
        [
            TS_RUNNER_FAILS,
            "FAIL [key_order_integer_like_keys] field=canonicalize.expect",
        ],
    ),
    mutant(
        "ts checkpoint: failed is not reusable by default",
        "ts/checkpoint.ts",
        '  failed: "failed",',
        '  failed: "complete",',
        [TS_RUNNER_FAILS, "FAIL [status_failed_not_reusable] field=checkpoint.expect"],
    ),
    mutant(
        "ts canonicalize: an array hole halts",
        "ts/checkpoint.ts",
        "    for (let index = 0; index < value.length; index += 1) {\n      parts.push",
        "    for (let index = 0; index < value.length; index += 1) {\n      if (!(index in value)) continue;\n      parts.push",
        [TS_RUNNER_FAILS, "FAIL [sparse_array] field=canonicalize.expect"],
    ),
    mutant(
        "ts determinism: a caught clock read through global",
        "ts/checkpoint.ts",
        "function encodeValue(value: unknown, at: string, depth: number): string {\n",
        "function encodeValue(value: unknown, at: string, depth: number): string {\n  try { global.Date.now(); } catch {}\n",
        ["ts/checkpoint.ts (static)", "policy code used Date.now"],
    ),
    Mutant(
        "ts determinism: a caught clock read the static scan cannot see",
        (
            Edit(
                "ts/checkpoint.ts",
                "export function canonicalize(value: unknown)",
                TS_GLOBAL_REACH + "export function canonicalize(value: unknown)",
            ),
            Edit(
                "ts/checkpoint.ts",
                "function encodeValue(value: unknown, at: string, depth: number): string {\n",
                "function encodeValue(value: unknown, at: string, depth: number): string {\n  try { haltruleReach.Date.now(); } catch {}\n",
            ),
        ),
        tuple(["policy code used Date.now"]),
        tuple(["(static)"]),
    ),
    mutant(
        "py determinism: a caught refused import on the canonicalize path",
        "py/haltrule/checkpoint.py",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n    try:\n        import time  # noqa: F401\n    except ImportError:\n        pass\n",
        ["py/haltrule/checkpoint.py (static)", "policy code imported 'time'"],
    ),
    mutant(
        "py determinism: a caught refused import at module load",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\n\ntry:\n    import time  # noqa: F401\nexcept ImportError:\n    pass\n",
        ["py/haltrule/checkpoint.py (static)", "policy code imported 'time'"],
    ),
    Mutant(
        "runners: an added fixture file of unknown version is refused",
        (
            Edit(
                "fixtures/checkpoint/v1.json",
                None,
                '{"fixture_version": "checkpoint/v999", "canonicalize": [{"id": "added_file_null", "input": null, "expect": {"canonical": "WRONG", "digest": "sha256:WRONG"}}], "checkpoint": []}\n',
            ),
        ),
        tuple([TS_RUNNER_FAILS, PY_RUNNER_FAILS]),
        tuple(["case ids repeat"]),
    ),
    Mutant(
        "runners: --dump covers every section",
        (
            Edit(
                "ts/run-fixtures.ts", "      dumpCheckpoint(fixtures.checkpoint);\n", ""
            ),
            Edit(
                "py/run_fixtures.py",
                '            dump_checkpoint(fixtures["checkpoint"])\n',
                "",
            ),
        ),
        tuple(
            [
                "both --dump outputs are identical but not complete",
                "python --dump under PYTHONHASHSEED=0 is not complete",
            ]
        ),
        tuple([]),
    ),
    Mutant(
        "runners: every section runs",
        (
            Edit(
                "ts/run-fixtures.ts", "      runCheckpoint(fixtures.checkpoint);\n", ""
            ),
            Edit(
                "py/run_fixtures.py",
                '            run_checkpoint(fixtures["checkpoint"])\n',
                "",
            ),
        ),
        tuple(
            [
                "typescript runner reported",
                "python runner reported",
                "the fixtures hold",
            ]
        ),
        tuple([]),
    ),
    mutant(
        "py checkpoint: validation issues from any sequence count",
        "py/haltrule/checkpoint.py",
        "        if isinstance(validation_issues, Sequence)\n        and not isinstance(validation_issues, (str, bytes, bytearray))\n",
        "        if isinstance(validation_issues, (list, tuple))\n",
        [PY_RUNNER_FAILS, "FAIL [validation_issue_defaults] field=checkpoint.raised"],
    ),
    mutant(
        "py canonicalize: a huge integer halts without raising",
        "py/haltrule/checkpoint.py",
        '            else f"a {value.bit_length()}-bit integer"\n',
        '            else f"{value}"\n',
        [PY_RUNNER_FAILS, "FAIL [bigint_5001_digits] field=canonicalize.raised"],
    ),
    mutant(
        "py checkpoint: text validation issues are ignored",
        "py/haltrule/checkpoint.py",
        "not isinstance(validation_issues, (str, bytes, bytearray))",
        "not isinstance(validation_issues, (bytes, bytearray))",
        [
            PY_RUNNER_FAILS,
            "FAIL [validation_issues_text_is_ignored] field=checkpoint.raised",
        ],
    ),
    mutant(
        "py checkpoint: bytes validation issues are ignored",
        "py/haltrule/checkpoint.py",
        "not isinstance(validation_issues, (str, bytes, bytearray))",
        "not isinstance(validation_issues, (str, bytearray))",
        [PY_RUNNER_FAILS, "FAIL [status_complete_is_valid] field=checkpoint.raised"],
    ),
    Mutant(
        "check.sh: a replayed file cannot stand in for another with the same ids",
        (
            Edit("fixtures/breaker/copy.json", None, _copied_breaker_fixture()),
            Edit(
                "ts/run-fixtures.ts",
                "    .map((relative) => path.join(FIXTURE_ROOT, relative));",
                '    .map((relative) => path.join(FIXTURE_ROOT, relative.replace("copy.json", "v0.json")));',
            ),
            Edit(
                "py/run_fixtures.py",
                "fixture_paths = [Path(positional[0])] if positional else default_fixture_paths()",
                'fixture_paths = [Path(positional[0])] if positional else [Path(str(p).replace("copy.json", "v0.json")) for p in default_fixture_paths()]',
            ),
        ),
        tuple(["case ids repeat across the fixture inventory"]),
        tuple([]),
    ),
    mutant(
        "py breaker: trips at the threshold, not after it",
        "py/haltrule/breaker.py",
        "len(self._pending_systemic) >= self.policy.systemic_threshold",
        "len(self._pending_systemic) > self.policy.systemic_threshold",
        [
            PY_RUNNER_FAILS,
            "FAIL [trip_at_threshold_three_rate_limit] field=state.tripped",
        ],
    ),
    mutant(
        "ts breaker: trips at the threshold, not after it",
        "ts/breaker.ts",
        "this.pendingSystemic.length >= this.policy.systemic_threshold",
        "this.pendingSystemic.length > this.policy.systemic_threshold",
        [
            TS_RUNNER_FAILS,
            "FAIL [trip_at_threshold_three_rate_limit] field=state.tripped",
        ],
    ),
    mutant(
        "ts canonicalize: a fractional number halts",
        "ts/checkpoint.ts",
        "if (!Number.isFinite(value) || !Number.isInteger(value)) {",
        "if (!Number.isFinite(value)) {",
        [TS_RUNNER_FAILS, "FAIL [fraction] field=canonicalize.expect"],
    ),
    # --- budget and slot: the first parts to return the one Verdict shape ---
    mutant(
        "ts budget: exhausted when the amount used reaches its cap",
        "ts/budget.ts",
        "this.turns_used >= this.max_turns",
        "this.turns_used > this.max_turns",
        [
            TS_RUNNER_FAILS,
            "FAIL [turns_one_charge_exactly_the_cap] field=charge.expect",
        ],
    ),
    mutant(
        "py budget: exhausted when the amount used reaches its cap",
        "py/haltrule/budget.py",
        "self.turns_used >= self.max_turns",
        "self.turns_used > self.max_turns",
        [
            PY_RUNNER_FAILS,
            "FAIL [turns_one_charge_exactly_the_cap] field=charge.expect",
        ],
    ),
    mutant(
        "ts budget: turns reported before time",
        "ts/budget.ts",
        "if (this.max_turns !== null && this.turns_used >= this.max_turns) {",
        "if (this.max_turns !== null && this.turns_used >= this.max_turns && !(this.time_budget_ms !== null && this.ms_used >= this.time_budget_ms)) {",
        [
            TS_RUNNER_FAILS,
            "FAIL [turns_reported_before_time_before_tokens] field=charge.expect",
        ],
    ),
    mutant(
        "py budget: time reported before tokens",
        "py/haltrule/budget.py",
        "if self.time_budget_ms is not None and self.ms_used >= self.time_budget_ms:",
        "if self.time_budget_ms is not None and self.ms_used >= self.time_budget_ms and not (self.token_budget is not None and self.tokens_used >= self.token_budget):",
        [PY_RUNNER_FAILS, "FAIL [time_reported_before_tokens] field=charge.expect"],
    ),
    mutant(
        "ts budget: a cap past 2^53 stays exact",
        "ts/budget.ts",
        '  if (typeof value === "bigint") n = value;',
        '  if (typeof value === "bigint") n = BigInt(Number(value));',
        [
            TS_RUNNER_FAILS,
            "FAIL [cap_past_the_safe_range_is_exact] field=charge.expect",
        ],
    ),
    mutant(
        "py budget: a null cap means no cap, not zero",
        "py/haltrule/budget.py",
        "    return None if value is None else _ledger(value, what)",
        "    return _ledger(0 if value is None else value, what)",
        [PY_RUNNER_FAILS, "FAIL [no_caps_never_exhausted] field=charge.expect"],
    ),
    mutant(
        "ts budget: a zero cap is exhausted before any charge",
        "ts/budget.ts",
        "    if (this.max_turns !== null && this.turns_used >= this.max_turns) {",
        "    if (this.max_turns !== null && this.turns_used >= this.max_turns && this.turns_used > 0n) {",
        [
            TS_RUNNER_FAILS,
            "FAIL [zero_cap_is_exhausted_before_any_charge] field=charge.expect",
        ],
    ),
    mutant(
        "ts slot: blank is ASCII whitespace only, never a Unicode trim",
        "ts/slot.ts",
        "  if (isBlank(value)) return",
        '  if (value.trim() === "") return',
        [
            TS_RUNNER_FAILS,
            "FAIL [choice_no_break_space_is_invalid_not_missing] field=validate.expect",
        ],
    ),
    mutant(
        "py slot: blank is ASCII whitespace only, never str.strip()",
        "py/haltrule/slot.py",
        '    return text.strip(_ASCII_WHITESPACE) == ""',
        '    return text.strip() == ""',
        [
            PY_RUNNER_FAILS,
            "FAIL [choice_no_break_space_is_invalid_not_missing] field=validate.expect",
        ],
    ),
    mutant(
        "ts slot: length counts scalar values, not UTF-16 units",
        "ts/slot.ts",
        "  const length = Array.from(value).length;",
        "  const length = value.length;",
        [
            TS_RUNNER_FAILS,
            "FAIL [text_length_counts_scalar_values_not_utf16_units] field=validate.expect",
        ],
    ),
    mutant(
        "py slot: length counts scalar values, not bytes",
        "py/haltrule/slot.py",
        "    length = len(value)",
        '    length = len(value.encode("utf-8"))',
        [
            PY_RUNNER_FAILS,
            "FAIL [text_length_counts_scalar_values_not_bytes] field=validate.expect",
        ],
    ),
    mutant(
        "ts slot: a candidate must match exactly, untrimmed",
        "ts/slot.ts",
        "    return candidates.includes(value)",
        "    return candidates.includes(value.trim())",
        [
            TS_RUNNER_FAILS,
            "FAIL [choice_leading_space_is_invalid] field=validate.expect",
        ],
    ),
    mutant(
        "ts slot: a candidate must match exactly, unnormalized",
        "ts/slot.ts",
        "    return candidates.includes(value)",
        '    return candidates.includes(value.normalize("NFC"))',
        [TS_RUNNER_FAILS, "FAIL [choice_hangul_nfd_is_invalid] field=validate.expect"],
    ),
    mutant(
        "py slot: max_length is inclusive",
        "py/haltrule/slot.py",
        "    if maximum is not None and length > maximum:",
        "    if maximum is not None and length >= maximum:",
        [
            PY_RUNNER_FAILS,
            "FAIL [text_exactly_max_length_accepted] field=validate.expect",
        ],
    ),
    mutant(
        "py slot: null is missing, not invalid",
        "py/haltrule/slot.py",
        '    if value is None:\n        return verdict("warning", "slot_missing", f"slot {name}: no value")\n',
        "",
        [PY_RUNNER_FAILS, "FAIL [choice_null_is_missing] field=validate.expect"],
    ),
    mutant(
        "ts verdict: every verdict carries the spec stamp",
        "ts/verdict.ts",
        'export const SPEC = "haltrule/0";',
        'export const SPEC = "haltrule/1";',
        [
            TS_RUNNER_FAILS,
            "FAIL [no_caps_never_exhausted] field=charge.expect",
            "FAIL [choice_first_candidate_accepted] field=validate.expect",
        ],
    ),
    mutant(
        "py verdict: every verdict carries the spec stamp",
        "py/haltrule/verdict.py",
        'SPEC = "haltrule/0"',
        'SPEC = "haltrule/1"',
        [
            PY_RUNNER_FAILS,
            "FAIL [no_caps_never_exhausted] field=charge.expect",
            "FAIL [choice_first_candidate_accepted] field=validate.expect",
        ],
    ),
    mutant(
        "ts runner: a verdict is compared without its message",
        "ts/run-fixtures.ts",
        "  const { message, ...rest } = result;\n  void message;\n  return rest;",
        "  return result;",
        [TS_RUNNER_FAILS, "FAIL [no_caps_never_exhausted] field=charge.expect"],
    ),
    mutant(
        "py budget: a clock import in the policy file",
        "py/haltrule/budget.py",
        "from haltrule.verdict import verdict\n",
        "from haltrule.verdict import verdict\nimport time  # noqa: F401\n",
        ["py/haltrule/budget.py (static)", "policy code imported 'time'"],
    ),
    mutant(
        "ts slot: a clock read in the policy file",
        "ts/slot.ts",
        "export function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        "export const loadedAt = Date.now();\nexport function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        ["ts/slot.ts (static)", "policy code used Date.now"],
    ),
    mutant(
        "check.sh: the seal loads every policy module",
        "scripts/check.sh",
        "for name, file in modules:",
        'for name, file in [entry for entry in modules if entry[0] != "haltrule.slot"]:',
        ["module haltrule.slot loaded outside the seal"],
    ),
    mutant(
        "check.sh: the policy file lists are not empty",
        "scripts/check.sh",
        "list_files ts_policy_files ts -type f -name '*.ts' ! -path ts/run-fixtures.ts",
        "list_files ts_policy_files ts -type f -name '*.tsx' ! -path ts/run-fixtures.ts",
        ["TypeScript and", "policy files; expected at least"],
    ),
    # --- round-4 review: the library contract (lone surrogates, the verdict
    # shape, token overshoot, ledger saturation, a $number a double cannot hold)
    mutant(
        "ts slot: a lone surrogate is not a string of scalar values",
        "ts/slot.ts",
        "  if (hasLoneSurrogate(value)) {",
        "  if (hasLoneSurrogate(value) && value.length > 3) {",
        [TS_RUNNER_FAILS, "FAIL [text_lone_high_surrogate_is_invalid] field=validate.expect"],
    ),
    mutant(
        "py slot: a lone surrogate is not a string of scalar values",
        "py/haltrule/slot.py",
        "    if _has_lone_surrogate(value):",
        "    if _has_lone_surrogate(value) and len(value) > 3:",
        [PY_RUNNER_FAILS, "FAIL [text_lone_high_surrogate_is_invalid] field=validate.expect"],
    ),
    mutant(
        "ts verdict: message is a string, never null",
        "ts/verdict.ts",
        "  return { spec: SPEC, verdict: level, reason, message, resume };",
        "  return { spec: SPEC, verdict: level, reason, message: null as unknown as string, resume };",
        [TS_RUNNER_FAILS, "FAIL [no_caps_never_exhausted] field=charge.raised"],
    ),
    mutant(
        "py verdict: message is a string, never None",
        "py/haltrule/verdict.py",
        '        "message": message,',
        '        "message": None,',
        [PY_RUNNER_FAILS, "FAIL [no_caps_never_exhausted] field=charge.raised"],
    ),
    mutant(
        "ts budget: tokens exhausted past the cap, not only at it",
        "ts/budget.ts",
        "    if (this.token_budget !== null && this.tokens_used >= this.token_budget) {",
        "    if (this.token_budget !== null && this.tokens_used === this.token_budget) {",
        [TS_RUNNER_FAILS, "FAIL [tokens_one_charge_past_the_cap] field=charge.expect"],
    ),
    mutant(
        "py budget: tokens exhausted past the cap, not only at it",
        "py/haltrule/budget.py",
        "        if self.token_budget is not None and self.tokens_used >= self.token_budget:",
        "        if self.token_budget is not None and self.tokens_used == self.token_budget:",
        [PY_RUNNER_FAILS, "FAIL [tokens_one_charge_past_the_cap] field=charge.expect"],
    ),
    mutant(
        "ts budget: the ledger saturates at 2^63 - 1",
        "ts/budget.ts",
        "  return sum > LEDGER_MAX ? LEDGER_MAX : sum;",
        "  return sum;",
        [TS_RUNNER_FAILS, "FAIL [no_caps_never_exhausted] field=charge.expect"],
    ),
    mutant(
        "py budget: the ledger saturates at 2^63 - 1",
        "py/haltrule/budget.py",
        "    return min(used + amount, _LEDGER_MAX)",
        "    return used + amount",
        [PY_RUNNER_FAILS, "FAIL [no_caps_never_exhausted] field=charge.expect"],
    ),
    mutant(
        "ts runner: a $number a double cannot hold is refused, not rounded",
        "ts/run-fixtures.ts",
        "      if (INTEGER_LITERAL.test(inner) && !exactlyRepresentable(inner, decoded)) {",
        "      if (INTEGER_LITERAL.test(inner) && !Number.isFinite(decoded)) {",
        ["typescript did not refuse a $number integer literal a double cannot hold"],
    ),
    mutant(
        "py runner: a $number a double cannot hold is refused, not kept exact",
        "py/run_fixtures.py",
        "            if not _exactly_representable(value):",
        "            if not _exactly_representable(value) and value < 0:",
        ["python did not refuse a $number integer literal a double cannot hold"],
    ),
    # --- round-4 review: the harness (each resource and each bound has its own
    # case; the package marker and hidden files are policy files)
    mutant(
        "ts slot: both bounds hold when both are given",
        "ts/slot.ts",
        "  const max = bound(spec.max_length, `slot ${name}: max_length`);",
        "  const max = min === null ? bound(spec.max_length, `slot ${name}: max_length`) : null;",
        [TS_RUNNER_FAILS, "FAIL [text_both_bounds_above_max_is_invalid] field=validate.expect"],
    ),
    mutant(
        "py slot: both bounds hold when both are given",
        "py/haltrule/slot.py",
        '    maximum = _bound(spec.get("max_length"), f"slot {name}: max_length")',
        '    maximum = None if minimum is not None else _bound(spec.get("max_length"), "max")',
        [PY_RUNNER_FAILS, "FAIL [text_both_bounds_above_max_is_invalid] field=validate.expect"],
    ),
    mutant(
        "ts budget: a zero time cap is exhausted before any charge",
        "ts/budget.ts",
        "    if (this.time_budget_ms !== null && this.ms_used >= this.time_budget_ms) {",
        "    if (this.time_budget_ms !== null && this.ms_used >= this.time_budget_ms && this.ms_used > 0n) {",
        [TS_RUNNER_FAILS, "FAIL [zero_time_cap_is_exhausted_before_any_charge] field=charge.expect"],
    ),
    mutant(
        "ts budget: a zero token cap is exhausted before any charge",
        "ts/budget.ts",
        "    if (this.token_budget !== null && this.tokens_used >= this.token_budget) {",
        "    if (this.token_budget !== null && this.tokens_used >= this.token_budget && this.tokens_used > 0n) {",
        [TS_RUNNER_FAILS, "FAIL [zero_token_cap_is_exhausted_before_any_charge] field=charge.expect"],
    ),
    mutant(
        "py budget: a zero cap is exhausted before any charge",
        "py/haltrule/budget.py",
        "        if self.max_turns is not None and self.turns_used >= self.max_turns:",
        "        if self.max_turns is not None and self.turns_used >= self.max_turns > 0:",
        [PY_RUNNER_FAILS, "FAIL [zero_cap_is_exhausted_before_any_charge] field=charge.expect"],
    ),
    mutant(
        "py budget: a zero time cap is exhausted before any charge",
        "py/haltrule/budget.py",
        "        if self.time_budget_ms is not None and self.ms_used >= self.time_budget_ms:",
        "        if self.time_budget_ms is not None and self.ms_used >= self.time_budget_ms > 0:",
        [PY_RUNNER_FAILS, "FAIL [zero_time_cap_is_exhausted_before_any_charge] field=charge.expect"],
    ),
    mutant(
        "py budget: a zero token cap is exhausted before any charge",
        "py/haltrule/budget.py",
        "        if self.token_budget is not None and self.tokens_used >= self.token_budget:",
        "        if self.token_budget is not None and self.tokens_used >= self.token_budget > 0:",
        [PY_RUNNER_FAILS, "FAIL [zero_token_cap_is_exhausted_before_any_charge] field=charge.expect"],
    ),
    mutant(
        "ts budget: an omitted turns charge is zero",
        "ts/budget.ts",
        '    this.turns_used = saturatingAdd(this.turns_used, ledger(charge.turns, "turns"));',
        '    this.turns_used = saturatingAdd(this.turns_used, ledger(charge.turns ?? 1, "turns"));',
        [TS_RUNNER_FAILS, "FAIL [empty_charge_reports_the_current_state] field=charge.expect"],
    ),
    mutant(
        "ts budget: an omitted ms charge is zero",
        "ts/budget.ts",
        '    this.ms_used = saturatingAdd(this.ms_used, ledger(charge.ms, "ms"));',
        '    this.ms_used = saturatingAdd(this.ms_used, ledger(charge.ms ?? 1, "ms"));',
        [TS_RUNNER_FAILS, "FAIL [empty_charge_under_time_cap_reports_the_current_state] field=charge.expect"],
    ),
    mutant(
        "ts budget: an omitted tokens charge is zero",
        "ts/budget.ts",
        '    this.tokens_used = saturatingAdd(this.tokens_used, ledger(charge.tokens, "tokens"));',
        '    this.tokens_used = saturatingAdd(this.tokens_used, ledger(charge.tokens ?? 1, "tokens"));',
        [TS_RUNNER_FAILS, "FAIL [empty_charge_under_token_cap_reports_the_current_state] field=charge.expect"],
    ),
    mutant(
        "py budget: an omitted turns charge is zero",
        "py/haltrule/budget.py",
        "    def charge(self, *, turns: Any = 0, ms: Any = 0, tokens: Any = 0) -> dict[str, Any]:",
        "    def charge(self, *, turns: Any = 1, ms: Any = 0, tokens: Any = 0) -> dict[str, Any]:",
        [PY_RUNNER_FAILS, "FAIL [empty_charge_reports_the_current_state] field=charge.expect"],
    ),
    mutant(
        "py budget: an omitted ms charge is zero",
        "py/haltrule/budget.py",
        "    def charge(self, *, turns: Any = 0, ms: Any = 0, tokens: Any = 0) -> dict[str, Any]:",
        "    def charge(self, *, turns: Any = 0, ms: Any = 1, tokens: Any = 0) -> dict[str, Any]:",
        [PY_RUNNER_FAILS, "FAIL [empty_charge_under_time_cap_reports_the_current_state] field=charge.expect"],
    ),
    mutant(
        "py budget: an omitted tokens charge is zero",
        "py/haltrule/budget.py",
        "    def charge(self, *, turns: Any = 0, ms: Any = 0, tokens: Any = 0) -> dict[str, Any]:",
        "    def charge(self, *, turns: Any = 0, ms: Any = 0, tokens: Any = 1) -> dict[str, Any]:",
        [PY_RUNNER_FAILS, "FAIL [empty_charge_under_token_cap_reports_the_current_state] field=charge.expect"],
    ),
    Mutant(
        "check.sh: a helper in the package marker is policy code too",
        (
            Edit(
                "py/haltrule/__init__.py",
                '"""haltrule: each part is a module of its own; the package holds nothing else."""\n',
                '"""haltrule: each part is a module of its own; the package holds nothing else."""\n\nimport time\n\n\ndef read_clock():\n    return time.time()\n',
            ),
            Edit(
                "py/haltrule/budget.py",
                "from haltrule.verdict import verdict\n",
                "from haltrule import read_clock\nfrom haltrule.verdict import verdict\n\nREAD_AT = read_clock()\n",
            ),
        ),
        tuple(["py/haltrule/__init__.py (static)", "policy code imported 'time'"]),
    ),
    mutant(
        "check.sh: the seal loads the package marker too",
        "scripts/check.sh",
        "modules = sorted((dotted(file), file) for file in POLICY_FILES)\n",
        'modules = sorted((dotted(file), file) for file in POLICY_FILES if not file.endswith("/__init__.py"))\n',
        ["python sealed run carries no seal marker"],
    ),
    Mutant(
        "check.sh: a hidden policy file is under the gates too",
        (
            Edit(
                "ts/.hidden-policy.ts",
                None,
                'import { readFileSync } from "node:fs";\n\nexport const size = readFileSync(new URL(import.meta.url)).length;\n',
            ),
            Edit(
                "ts/run-fixtures.ts",
                'import { validateSlot, type SlotSpec } from "./slot.ts";\n',
                'import { validateSlot, type SlotSpec } from "./slot.ts";\nimport "./.hidden-policy.ts";\n',
            ),
        ),
        tuple(["ts/.hidden-policy.ts has import(s)/require(s)/re-export(s) beyond ./verdict.ts"]),
    ),
    Mutant(
        "check.sh: the typescript runner executes only the policy inventory (a double-quoted import)",
        (
            Edit("helpers/clock.ts", None, "export const startedAt = 0;\n"),
            Edit(
                "ts/run-fixtures.ts",
                'import { validateSlot, type SlotSpec } from "./slot.ts";\n',
                'import { validateSlot, type SlotSpec } from "./slot.ts";\nimport "../helpers/clock.ts";\n',
            ),
        ),
        tuple(["ts/run-fixtures.ts imports outside the policy inventory: ../helpers/clock.ts -> helpers/clock.ts, not in the policy inventory"]),
    ),
    Mutant(
        "check.sh: the typescript runner executes only the policy inventory (a single-quoted import)",
        (
            Edit("helpers/clock.ts", None, "export const startedAt = 0;\n"),
            Edit(
                "ts/run-fixtures.ts",
                'import { validateSlot, type SlotSpec } from "./slot.ts";\n',
                "import { validateSlot, type SlotSpec } from \"./slot.ts\";\nimport '../helpers/clock.ts';\n",
            ),
        ),
        tuple(["ts/run-fixtures.ts imports outside the policy inventory: ../helpers/clock.ts -> helpers/clock.ts, not in the policy inventory"]),
    ),
    Mutant(
        "check.sh: the python runner executes only the policy inventory",
        (
            Edit("py/clock_helper.py", None, "STARTED_AT = 0\n"),
            Edit(
                "py/run_fixtures.py",
                "from haltrule.slot import validate_slot  # noqa: E402\n",
                "from haltrule.slot import validate_slot  # noqa: E402\nimport clock_helper  # noqa: E402,F401\n",
            ),
        ),
        tuple(
            [
                "py/run_fixtures.py imports outside the standard library and the policy inventory",
                "clock_helper: not in the standard library",
                # and the seal's own location test: a module from inside the tree that it did not load
                "module clock_helper loaded outside the seal",
            ]
        ),
    ),
    # --- round-4 re-review: every branch of the runner import scans has a
    # mutant only it can satisfy; a file the gates cannot read fails; the
    # seal trusts its own registry, not a module's word, and reads location
    Mutant(
        "check.sh: the typescript runner cannot reach outside the inventory by a dynamic import",
        (
            Edit("helpers/clock.ts", None, "export const startedAt = 0;\n"),
            Edit(
                "ts/run-fixtures.ts",
                'import { validateSlot, type SlotSpec } from "./slot.ts";\n',
                'import { validateSlot, type SlotSpec } from "./slot.ts";\nconst outside = await import("../helpers/clock.ts");\nvoid outside;\n',
            ),
        ),
        tuple(["ts/run-fixtures.ts imports outside the policy inventory: a dynamic import() or require()"]),
    ),
    mutant(
        "check.sh: the typescript runner cannot reach outside the inventory by a bare specifier",
        "ts/run-fixtures.ts",
        'import { validateSlot, type SlotSpec } from "./slot.ts";\n',
        'import { validateSlot, type SlotSpec } from "./slot.ts";\nimport "typescript";\n',
        ["ts/run-fixtures.ts imports outside the policy inventory: typescript: neither a node: builtin nor a relative path"],
    ),
    mutant(
        "check.sh: the python runner cannot reach outside the inventory by a relative import",
        "py/run_fixtures.py",
        "from haltrule.slot import validate_slot  # noqa: E402\n",
        "from haltrule.slot import validate_slot  # noqa: E402\nfrom . import clock_helper  # noqa: E402,F401\n",
        ["py/run_fixtures.py imports outside the standard library and the policy inventory", "a relative import"],
    ),
    mutant(
        "check.sh: the python runner cannot reach outside the inventory through importlib",
        "py/run_fixtures.py",
        "from haltrule.slot import validate_slot  # noqa: E402\n",
        "from haltrule.slot import validate_slot  # noqa: E402\nimport importlib  # noqa: E402,F401\n",
        ["py/run_fixtures.py imports outside the standard library and the policy inventory", "importlib loads modules the inventory cannot name"],
    ),
    mutant(
        "check.sh: the python runner cannot import a haltrule module outside the inventory",
        "py/run_fixtures.py",
        "from haltrule.slot import validate_slot  # noqa: E402\n",
        "from haltrule.slot import validate_slot  # noqa: E402\nimport haltrule.evil  # noqa: E402,F401\n",
        ["py/run_fixtures.py imports outside the standard library and the policy inventory", "haltrule.evil -> py/haltrule/evil.py, not in the policy inventory"],
    ),
    Mutant(
        "check.sh: a file the gates cannot read under the policy directories fails",
        (Edit("py/haltrule/evil.pyc", None, "not bytecode, but the gates cannot know that\n"),),
        tuple(["py/haltrule/evil.pyc is under the policy directories but the gates cannot read it"]),
    ),
    Mutant(
        "check.sh: a sibling shadowing a standard-library module on the runners' path",
        (
            Edit(
                "py/hashlib.py",
                None,
                "from _hashlib import openssl_sha256 as sha256  # noqa: F401\nimport time\n\nLOADED_AT = time.time()\n",
            ),
        ),
        tuple(
            [
                "py/hashlib.py is under the policy directories but the gates cannot read it",
                # resolved before any policy runs, so the look-alike never executes
                "module hashlib is not the standard library's",
            ]
        ),
    ),
    Mutant(
        "check.sh: a policy subpackage is built by the seal, whatever it says of its own builtins",
        (
            Edit(
                "py/haltrule/sub/__init__.py",
                None,
                'import sys\n\n__builtins__ = sys.modules["haltrule"].__dict__["__builtins__"]\n',
            ),
            Edit(
                "py/haltrule/budget.py",
                "from haltrule.verdict import verdict\n",
                "from haltrule import sub  # noqa: F401\nfrom haltrule.verdict import verdict\n",
            ),
        ),
        # the seal builds the whole inventory, so the package loads sealed and
        # its import of sys is refused like any other
        tuple(["policy code imported 'sys'"]),
    ),
    # --- round-4 re-review: refusals the spec requires are probed directly,
    # since a raise can never be a fixture expectation
    mutant(
        "py slot: a bound past 2^53 - 1 is refused",
        "py/haltrule/slot.py",
        "_BOUND_MAX = 2**53 - 1",
        "_BOUND_MAX = 2**63 - 1",
        ["python accepted an out-of-contract input: slot bound past 2^53 - 1"],
    ),
    mutant(
        "ts slot: a bound past 2^53 - 1 is refused",
        "ts/slot.ts",
        '  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;',
        '  if (typeof value === "number" && Number.isInteger(value) && value >= 0) return value;',
        ["typescript accepted an out-of-contract input: slot bound past 2^53 - 1"],
    ),
    mutant(
        "py budget: a negative amount is refused",
        "py/haltrule/budget.py",
        "    if not 0 <= value <= _LEDGER_MAX:",
        "    if not value <= _LEDGER_MAX:",
        ["python accepted an out-of-contract input: budget negative charge"],
    ),
    mutant(
        "ts budget: a negative amount is refused",
        "ts/budget.ts",
        "  if (n < 0n || n > LEDGER_MAX) throw new RangeError(",
        "  if (n > LEDGER_MAX) throw new RangeError(",
        ["typescript accepted an out-of-contract input: budget negative charge"],
    ),
    # The look-alike lives outside the tree, as an environment's would: inside
    # the tree the location check would name it first.
    mutant(
        "check.sh: an allowlisted standard-library name must be the standard library's file",
        "scripts/check.sh",
        'py_sealed_out=$(python3 "$py_seal" "$PY_ALLOWED_MODULES" "${py_policy_files[@]}" 2>&1)',
        """py_sealed_out=$(look_alike=$(mktemp -d); printf '%s\\n' 'import os, sysconfig' 'from _hashlib import openssl_sha256 as sha256' '__file__ = os.path.join(sysconfig.get_paths()["stdlib"], "hashlib.py")' > "$look_alike/hashlib.py"; PYTHONPATH="$look_alike" python3 "$py_seal" "$PY_ALLOWED_MODULES" "${py_policy_files[@]}" 2>&1; rm -rf "$look_alike")""",
        ["module hashlib is not the standard library's"],
    ),
    # --- round-4 third review (library): a name is required, both signs of a
    # wide literal, a lone surrogate after a pair, the largest allowed bound,
    # booleans
    mutant(
        "ts slot: a spec needs a string name",
        "ts/slot.ts",
        '  if (typeof name !== "string") throw new TypeError(`slot spec without a string name: ${String(name)}`);',
        '  if (typeof name !== "string" && name !== undefined) throw new TypeError(`slot spec without a string name: ${String(name)}`);',
        ["typescript accepted an out-of-contract input: slot spec without a name"],
    ),
    mutant(
        "py slot: a spec needs a string name",
        "py/haltrule/slot.py",
        "    if not isinstance(name, str):",
        "    if name is not None and not isinstance(name, str):",
        ["python accepted an out-of-contract input: slot spec without a name"],
    ),
    mutant(
        "ts runner: a negative $number a double cannot hold is refused too",
        "ts/run-fixtures.ts",
        "const INTEGER_LITERAL = /^-?(0|[1-9][0-9]*)$/;",
        "const INTEGER_LITERAL = /^(0|[1-9][0-9]*)$/;",
        ["typescript did not refuse a negative $number integer literal a double cannot hold"],
    ),
    mutant(
        "py runner: a negative $number a double cannot hold is refused too",
        "py/run_fixtures.py",
        '_INTEGER_LITERAL = re.compile(r"-?(0|[1-9][0-9]*)")',
        '_INTEGER_LITERAL = re.compile(r"(0|[1-9][0-9]*)")',
        ["python did not refuse a negative $number integer literal a double cannot hold"],
    ),
    mutant(
        "ts slot: the surrogate scan continues past a valid pair",
        "ts/slot.ts",
        "        continue;",
        "        return false;",
        [TS_RUNNER_FAILS, "FAIL [text_lone_surrogate_after_a_pair_is_invalid] field=validate.expect"],
    ),
    mutant(
        "py slot: the surrogate scan reads the whole string",
        "py/haltrule/slot.py",
        "    return any(0xD800 <= ord(character) <= 0xDFFF for character in text)",
        "    return any(0xD800 <= ord(character) <= 0xDFFF for character in text[:1])",
        [PY_RUNNER_FAILS, "FAIL [text_lone_surrogate_after_a_pair_is_invalid] field=validate.expect"],
    ),
    mutant(
        "py slot: the largest allowed bound is allowed",
        "py/haltrule/slot.py",
        "    if not 0 <= value <= _BOUND_MAX:",
        "    if not 0 <= value < _BOUND_MAX:",
        [PY_RUNNER_FAILS, "FAIL [text_max_length_at_the_largest_allowed_bound_accepted] field=validate.raised"],
    ),
    mutant(
        "ts slot: the largest allowed bound is allowed",
        "ts/slot.ts",
        '  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;',
        '  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value < Number.MAX_SAFE_INTEGER) return value;',
        [TS_RUNNER_FAILS, "FAIL [text_max_length_at_the_largest_allowed_bound_accepted] field=validate.raised"],
    ),
    mutant(
        "py budget: a boolean is not a count",
        "py/haltrule/budget.py",
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):",
        "    if not isinstance(value, (int, float)):",
        ["python accepted an out-of-contract input: budget boolean charge"],
    ),
    mutant(
        "py slot: a boolean is not a bound",
        "py/haltrule/slot.py",
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):",
        "    if not isinstance(value, (int, float)):",
        ["python accepted an out-of-contract input: slot boolean bound"],
    ),
    # --- round-5 harness review: a module an importer kept from before the
    # seal replaced it; the whole refusal matrix; exact paths and a checked
    # listing in the unknown-file rule; counters that must be numbers
    Mutant(
        "check.sh: a policy module an importer kept is the seal's own object",
        (
            Edit("py/haltrule/zz_probe.py", None, "VALUE = 1\n"),
            Edit(
                "py/haltrule/budget.py",
                "from haltrule.verdict import verdict\n",
                "from haltrule import zz_probe  # noqa: F401\nfrom haltrule.verdict import verdict\n",
            ),
            # the old loader: each module runs as soon as it exists, and a
            # sibling the seal has not built yet goes to the real import
            Edit(
                "scripts/check.sh",
                '    if name not in sealed_modules:\n        record(f"imported {name!r}, which the seal did not build")\n        raise ImportError(f"sealed: policy code imported {name!r}, which the seal did not build")\n',
                "    if name not in sealed_modules or fromlist:\n        return builtins.__import__(name, globals, locals, fromlist, level)\n",
            ),
            Edit(
                "scripts/check.sh",
                "    sealed_modules[name] = module\n    specs[name] = spec\n",
                "    sealed_modules[name] = module\n    specs[name] = spec\n    run_sealed(name)\n",
            ),
        ),
        tuple(["module haltrule.zz_probe held by haltrule.budget loaded outside the seal"]),
    ),
    mutant(
        "py budget: a cap is held to the same contract as an amount",
        "py/haltrule/budget.py",
        "    return None if value is None else _ledger(value, what)",
        "    return None if value is None else _ledger(max(value, 0), what)",
        ["python accepted an out-of-contract input: budget negative cap"],
    ),
    mutant(
        "ts budget: a cap is held to the same contract as an amount",
        "ts/budget.ts",
        "  return value === null || value === undefined ? null : ledger(value, what);",
        '  return value === null || value === undefined ? null : ledger(typeof value === "number" && value < 0 ? 0 : value, what);',
        ["typescript accepted an out-of-contract input: budget negative cap"],
    ),
    mutant(
        "py budget: a fractional amount is refused",
        "py/haltrule/budget.py",
        "        if not value.is_integer():",
        "        if value != value:",
        ["python accepted an out-of-contract input: budget fractional charge"],
    ),
    mutant(
        "ts budget: a fractional amount is refused",
        "ts/budget.ts",
        '  else if (typeof value === "number" && Number.isInteger(value)) n = BigInt(value);',
        '  else if (typeof value === "number" && Number.isFinite(value)) n = BigInt(Math.trunc(value));',
        ["typescript accepted an out-of-contract input: budget fractional charge"],
    ),
    mutant(
        "py budget: an amount past 2^63 - 1 is refused",
        "py/haltrule/budget.py",
        "    if not 0 <= value <= _LEDGER_MAX:",
        "    if not 0 <= value:",
        ["python accepted an out-of-contract input: budget charge past 2^63 - 1"],
    ),
    mutant(
        "ts budget: an amount past 2^63 - 1 is refused",
        "ts/budget.ts",
        "  if (n < 0n || n > LEDGER_MAX) throw new RangeError(",
        "  if (n < 0n) throw new RangeError(",
        ["typescript accepted an out-of-contract input: budget charge past 2^63 - 1"],
    ),
    mutant(
        "py slot: a fractional bound is refused",
        "py/haltrule/slot.py",
        "        if not value.is_integer():",
        "        if value != value:",
        ["python accepted an out-of-contract input: slot fractional bound"],
    ),
    mutant(
        "ts slot: a fractional bound is refused",
        "ts/slot.ts",
        '  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;',
        '  if (typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER) return value;',
        ["typescript accepted an out-of-contract input: slot fractional bound"],
    ),
    mutant(
        "py slot: a name that is not text is refused",
        "py/haltrule/slot.py",
        "    if not isinstance(name, str):",
        "    if name is None:",
        ["python accepted an out-of-contract input: slot spec with a non-string name"],
    ),
    mutant(
        "ts slot: a name that is not text is refused",
        "ts/slot.ts",
        '  if (typeof name !== "string") throw new TypeError(`slot spec without a string name: ${String(name)}`);',
        "  if (name === undefined) throw new TypeError(`slot spec without a string name: ${String(name)}`);",
        ["typescript accepted an out-of-contract input: slot spec with a non-string name"],
    ),
    Mutant(
        "check.sh: the unknown-file rule compares whole paths",
        (Edit("py/run_fixtures.py ts/run-fixtures.ts", None, "export const PROBE = 1;\n"),),
        tuple(["py/run_fixtures.py ts/run-fixtures.ts is under the policy directories but the gates cannot read it"]),
    ),
    mutant(
        "check.sh: a listing that failed is not an empty listing",
        "scripts/check.sh",
        "find py ts -mindepth 1 \\( -type f -o -type l \\)",
        "find py-absent ts-absent -mindepth 1 \\( -type f -o -type l \\)",
        ["could not list py/ and ts/"],
    ),
    mutant(
        "check.sh: the typescript refusal count must be a number",
        "scripts/check.sh",
        """ts_probe_refused=$(printf '%s\\n' "$ts_probe_out" | grep -c '^refused: ')""",
        "ts_probe_refused=$(false)",
        ["typescript out-of-contract probe counted ''"],
    ),
    mutant(
        "check.sh: the python refusal count must be a number",
        "scripts/check.sh",
        """py_probe_refused=$(printf '%s\\n' "$py_probe_out" | grep -c '^refused: ')""",
        "py_probe_refused=$(false)",
        ["python out-of-contract probe counted ''"],
    ),
    # --- round-6 nine-lens review: what policy code receives for a standard-
    # library import is pinned before it runs and is only the public surface;
    # a sibling runs when first imported; every listing is checked; NaN and
    # the infinities; the CI shards hold the catalog exactly once
    mutant(
        "py determinism: no value read through an allowlisted module's own imports",
        "py/haltrule/budget.py",
        "from haltrule.verdict import verdict\n",
        'import typing\n\nfrom haltrule.verdict import verdict\n\n_stamp = typing.sys.modules["os"].getpid()\n',
        ["reached typing.sys, which is not on the seal's list for typing"],
    ),
    mutant(
        "ts determinism: no code built from a string through a function's constructor",
        "ts/checkpoint.ts",
        'import { createHash } from "node:crypto";\n',
        'import { createHash } from "node:crypto";\nexport const stamp = (createHash as unknown as { constructor: (code: string) => () => number }).constructor("return process.pid")();\n',
        ["typescript policy code reached a refused API under the seal", "policy code used a function constructor"],
    ),
    mutant(
        "check.sh: policy code gets a module's public surface, never the module",
        "scripts/check.sh",
        "        return surfaces[name]\n",
        "        return pinned[name]\n",
        ["haltrule.breaker holds module math itself, not the names the seal hands out"],
    ),
    Mutant(
        "check.sh: a standard-library name still answers with the pinned object",
        (
            Edit("scripts/check.sh", "        return surfaces[name]\n", "        return pinned[name]\n"),
            Edit(
                "py/haltrule/budget.py",
                "from haltrule.verdict import verdict\n",
                'import typing\n\nfrom haltrule.verdict import verdict\n\ntyping.sys.modules["hashlib"] = typing.sys.modules["math"]\n',
            ),
        ),
        tuple(["module hashlib replaced under the seal"]),
    ),
    Mutant(
        "check.sh: the object policy code gets is loaded from the spec that was checked",
        (
            Edit(
                "scripts/check.sh",
                "pinned = {}\nsurfaces = {}\n",
                'counterfeit = types.ModuleType("hashlib")\ncounterfeit.sha256 = lambda data=b"": None\nsys.modules["hashlib"] = counterfeit\npinned = {}\nsurfaces = {}\n',
            ),
            Edit(
                "scripts/check.sh",
                "    module = importlib.util.module_from_spec(spec)\n    sys.modules[module_name] = module\n    spec.loader.exec_module(module)\n    pinned[module_name] = module\n",
                "    module = sys.modules.get(module_name) or importlib.import_module(module_name)\n    pinned[module_name] = module\n",
            ),
        ),
        tuple(["python: sealing changed the outcome under the seal"]),
    ),
    mutant(
        "check.sh: a sibling runs when it is first imported, whatever its name",
        "scripts/check.sh",
        '    run_sealed(name)\n    for part in fromlist or ():\n        if f"{name}.{part}" in sealed_modules:\n            run_sealed(f"{name}.{part}")\n',
        "",
        ["python sealed run carries no seal marker"],
    ),
    mutant(
        "check.sh: a listing that failed is not an inventory",
        "scripts/check.sh",
        "list_files fixture_files fixtures -type f -name '*.json'\n",
        "list_files fixture_files fixtures fixtures.absent -type f -name '*.json'\n",
        ["could not list every file: fixture_files (find exit"],
    ),
    mutant(
        "py budget: an amount that is not finite is refused",
        "py/haltrule/budget.py",
        "        if not value.is_integer():\n",
        '        if value != value or value in (float("inf"), float("-inf")):\n            return 0\n        if not value.is_integer():\n',
        ["python accepted an out-of-contract input: budget NaN charge"],
    ),
    mutant(
        "ts budget: an amount that is not finite is refused",
        "ts/budget.ts",
        '  if (typeof value === "bigint") n = value;\n',
        '  if (typeof value === "number" && !Number.isFinite(value)) n = 0n;\n  else if (typeof value === "bigint") n = value;\n',
        ["typescript accepted an out-of-contract input: budget NaN charge"],
    ),
    mutant(
        "py slot: a bound that is not finite is refused",
        "py/haltrule/slot.py",
        "        if not value.is_integer():\n",
        '        if value != value or value in (float("inf"), float("-inf")):\n            return None\n        if not value.is_integer():\n',
        ["python accepted an out-of-contract input: slot NaN bound"],
    ),
    mutant(
        "ts slot: a bound that is not finite is refused",
        "ts/slot.ts",
        "  if (value === null || value === undefined) return null;\n",
        '  if (value === null || value === undefined) return null;\n  if (typeof value === "number" && !Number.isFinite(value)) return null;\n',
        ["typescript accepted an out-of-contract input: slot NaN bound"],
    ),
    mutant(
        "py budget: a refusal is a TypeError or a ValueError, not any exception",
        "py/haltrule/budget.py",
        "        if not value.is_integer():\n            raise TypeError(",
        "        if not value.is_integer():\n            raise ArithmeticError(",
        ["python met an out-of-contract input with something other than a refusal: budget fractional charge: ArithmeticError"],
    ),
    mutant(
        "ts budget: a refusal is a TypeError or a RangeError, not any error",
        "ts/budget.ts",
        "  else throw new TypeError(`${what} must be an integer, got ${String(value)}`);",
        "  else throw new Error(`${what} must be an integer, got ${String(value)}`);",
        ["typescript met an out-of-contract input with something other than a refusal: budget fractional charge"],
    ),
    mutant(
        "ci: the matrix names every shard",
        ".github/workflows/check.yml",
        '        shard: ["1/3", "2/3", "3/3"]\n',
        '        shard: ["1/3", "2/3"]\n',
        ["the shards the workflow names (1/3,2/3) do not hold every catalog mutant exactly once", "are not 1/3..3/3 once each"],
    ),
    mutant(
        "ci: each matrix entry reaches --shard as it is",
        ".github/workflows/check.yml",
        "        run: python3 scripts/mutants.py --shard ${{ matrix.shard }}\n",
        "        run: python3 scripts/mutants.py --shard 1/3\n",
        ["the workflow does not name every mutation slice once in one job", "does not hand each matrix entry to --shard as it is"],
    ),
    mutant(
        "mutants.py: the shards hold every mutant exactly once",
        "scripts/mutants.py",
        "    return [items[first::count] for first in range(count)]\n",
        "    return [items[first + 1 :: count] for first in range(count)]\n",
        ["the shards the workflow names (1/3,2/3,3/3) do not hold every catalog mutant exactly once"],
    ),
    # --- round-7 nine-lens review (another model): a listed name, not a public
    # one; the capability, not the route; the job, not two lines of it
    mutant(
        "py determinism: no string evaluated through a public function of an allowlisted module",
        "py/haltrule/budget.py",
        "from haltrule.verdict import verdict\n",
        "import typing\n\nfrom haltrule.verdict import verdict\n\n\n"
        "def _probe(x: \"__import__('os').getpid()\") -> None:\n    return None\n\n\n"
        "_stamp = typing.get_type_hints(_probe, globalns={})\n",
        ["reached typing.get_type_hints, which is not on the seal's list for typing"],
    ),
    mutant(
        "ts determinism: no code built from a string through the global Function",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        TS_GLOBAL_REACH
        + 'export const stamp = haltruleReach.Function("return 1")();\nexport function canonicalize(value: unknown)',
        ["policy code used a function constructor"],
        ["(static)"],
    ),
    mutant(
        "ts determinism: no process id read through the global object",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        TS_GLOBAL_REACH + "export const stamp = haltruleReach.process.pid;\nexport function canonicalize(value: unknown)",
        ["policy code used process.pid"],
        ["(static)"],
    ),
    mutant(
        "ts determinism: no environment read through the global object",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        TS_GLOBAL_REACH + "export const stamp = haltruleReach.process.env.HOME;\nexport function canonicalize(value: unknown)",
        ["policy code used process.env"],
        ["(static)"],
    ),
    mutant(
        "ts determinism: no built-in module taken from the process object",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        TS_GLOBAL_REACH.replace("process: {", "process: { getBuiltinModule(name: string): unknown;")
        + 'export const stamp = haltruleReach.process.getBuiltinModule("node:os");\nexport function canonicalize(value: unknown)',
        ["policy code used process.getBuiltinModule"],
        ["(static)"],
    ),
    Mutant(
        "check.sh: a sibling that fails while loading is on record even when its importer catches it",
        (
            Edit("py/haltrule/zz_broken.py", None, '"""A sibling that cannot load."""\n\nraise RuntimeError("broken")\n'),
            Edit(
                "py/haltrule/budget.py",
                "from haltrule.verdict import verdict\n",
                "from haltrule.verdict import verdict\n\ntry:\n    from haltrule import zz_broken\nexcept RuntimeError:\n    zz_broken = None\n",
            ),
        ),
        tuple(["module haltrule.zz_broken failed while loading: RuntimeError"]),
    ),
    mutant(
        "check.sh: a listing whose sort failed says so",
        "scripts/check.sh",
        '  elif ! LC_ALL=C sort -z -o "$listing" "$listing"; then\n',
        "  elif ! false; then\n",
        ["could not list every file: fixture_files (sort failed)"],
    ),
    mutant(
        "py budget: negative infinity is refused",
        "py/haltrule/budget.py",
        "        if not value.is_integer():\n",
        '        if value == float("-inf"):\n            return 0\n        if not value.is_integer():\n',
        ["python accepted an out-of-contract input: budget negative infinite charge"],
    ),
    mutant(
        "ts budget: negative infinity is refused",
        "ts/budget.ts",
        '  if (typeof value === "bigint") n = value;\n',
        '  if (value === -Infinity) n = 0n;\n  else if (typeof value === "bigint") n = value;\n',
        ["typescript accepted an out-of-contract input: budget negative infinite charge"],
    ),
    mutant(
        "ci: no slice is excluded from the matrix",
        ".github/workflows/check.yml",
        '        shard: ["1/3", "2/3", "3/3"]\n',
        '        shard: ["1/3", "2/3", "3/3"]\n        exclude:\n          - shard: "3/3"\n',
        ["the workflow does not name every mutation slice once in one job", "uses exclude:"],
    ),
    mutant(
        "ci: a failing slice is not excused",
        ".github/workflows/check.yml",
        "      - name: Every mutant must make check.sh fail with its own evidence\n",
        "      - name: Every mutant must make check.sh fail with its own evidence\n        continue-on-error: true\n",
        ["the workflow does not name every mutation slice once in one job", "uses continue-on-error:"],
    ),
    mutant(
        "ci: the mutation job is not switched off",
        ".github/workflows/check.yml",
        "  gates-detect-a-defect:\n    runs-on: ubuntu-latest\n",
        "  gates-detect-a-defect:\n    if: false\n    runs-on: ubuntu-latest\n",
        ["the workflow does not name every mutation slice once in one job", "uses if:"],
    ),
    mutant(
        "ci: the matrix has no second key",
        ".github/workflows/check.yml",
        '        shard: ["1/3", "2/3", "3/3"]\n',
        '        shard: ["1/3", "2/3", "3/3"]\n        flavour: ["a"]\n',
        ["the workflow does not name every mutation slice once in one job", "not just shard"],
    ),
    # --- the result-line protocol and the literal grammar (fixtures/README.md):
    # each rule of the written format has a vector, and each vector is shown to
    # catch the serializer a port would reach for first
    mutant(
        "ts result line: keys are sorted, not left in the object's own order",
        "ts/run-fixtures.ts",
        "    const members = keys\n      .sort()\n",
        "    const members = keys\n",
        [TS_RUNNER_FAILS, "FAIL [line_keys_numeric_looking_sort_as_text] field=result_line.expect"],
    ),
    mutant(
        "ts result line: keys are sorted by code unit, not by locale",
        "ts/run-fixtures.ts",
        "    const members = keys\n      .sort()\n",
        "    const members = keys\n      .sort((a, b) => a.localeCompare(b))\n",
        [TS_RUNNER_FAILS, "FAIL [line_keys_empty_case_and_prefix] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a fraction is refused",
        "ts/run-fixtures.ts",
        "    if (!Number.isSafeInteger(value)) {\n",
        "    if (!Number.isFinite(value)) {\n",
        [TS_RUNNER_FAILS, "FAIL [line_fraction_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: an integer past the range is refused",
        "ts/run-fixtures.ts",
        "    if (!Number.isSafeInteger(value)) {\n",
        "    if (!Number.isInteger(value)) {\n",
        [TS_RUNNER_FAILS, "FAIL [line_integer_past_range_refused] field=result_line.expect"],
        ["FAIL [line_fraction_refused]"],
    ),
    mutant(
        "ts result line: a bigint past the range is refused",
        "ts/run-fixtures.ts",
        "    if (value > BigInt(Number.MAX_SAFE_INTEGER) || value < -BigInt(Number.MAX_SAFE_INTEGER)) {\n",
        "    if (false) {\n",
        [TS_RUNNER_FAILS, "FAIL [line_bigint_past_range_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a bigint within the range is an integer",
        "ts/run-fixtures.ts",
        '  if (typeof value === "bigint") {\n    // An integer is an integer however the language holds it.\n',
        '  if (typeof value === "bigint" && value < 0n) {\n    // An integer is an integer however the language holds it.\n',
        [TS_RUNNER_FAILS, "FAIL [line_bigint_within_range_is_an_integer] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a map with a key that is not a string is refused",
        "ts/run-fixtures.ts",
        "    if (Reflect.ownKeys(source).length !== keys.length) {\n",
        "    if (false) {\n",
        [TS_RUNNER_FAILS, "FAIL [line_non_string_key_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a hole in a list is refused, not skipped",
        "ts/run-fixtures.ts",
        "    for (let index = 0; index < value.length; index += 1) items.push(canonicalStringify(value[index]));\n",
        "    value.forEach((item) => items.push(canonicalStringify(item)));\n",
        [TS_RUNNER_FAILS, "FAIL [line_sparse_array_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: an instance is refused, not written as a map",
        "ts/run-fixtures.ts",
        '  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {\n',
        '  if (typeof value === "object") {\n',
        [TS_RUNNER_FAILS, "FAIL [line_instance_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: markup characters are written as themselves",
        "ts/run-fixtures.ts",
        '  if (typeof value === "string") return JSON.stringify(value);\n',
        '  if (typeof value === "string") return JSON.stringify(value).replace(/</g, "\\\\u003c");\n',
        [TS_RUNNER_FAILS, "FAIL [line_string_markup_and_separators_are_not_escaped] field=result_line.expect"],
    ),
    mutant(
        "py result line: keys are sorted by code unit, not by code point",
        "py/run_fixtures.py",
        "            for key in sorted(value, key=_utf16_units)\n",
        "            for key in sorted(value)\n",
        [
            PY_RUNNER_FAILS,
            "FAIL [line_keys_sort_by_utf16_code_unit] field=result_line.expect",
            # and the parity gate sees it on its own: a caller's keys reach a checkpoint result
            "runner output diverges",
        ],
        ["FAIL [line_keys_numeric_looking_sort_as_text]"],
    ),
    mutant(
        "py result line: keys are sorted, not left in insertion order",
        "py/run_fixtures.py",
        "            for key in sorted(value, key=_utf16_units)\n",
        "            for key in value\n",
        [PY_RUNNER_FAILS, "FAIL [line_keys_numeric_looking_sort_as_text] field=result_line.expect"],
    ),
    mutant(
        "py result line: an integral float is written as an integer",
        "py/run_fixtures.py",
        "        value = int(value)\n    if isinstance(value, int):\n",
        "        return repr(value)\n    if isinstance(value, int):\n",
        [PY_RUNNER_FAILS, "FAIL [line_integral_floats_are_written_as_integers] field=result_line.expect"],
    ),
    mutant(
        "py result line: a fraction is refused, not truncated",
        "py/run_fixtures.py",
        "        if not value.is_integer():  # false for NaN and the infinities too\n",
        "        if value != value or value in (float('inf'), float('-inf')):\n",
        [PY_RUNNER_FAILS, "FAIL [line_fraction_refused] field=result_line.expect"],
        ["FAIL [line_nan_refused]", "FAIL [line_infinity_refused]"],
    ),
    mutant(
        "py result line: an integer past the range is refused",
        "py/run_fixtures.py",
        "        if abs(value) > _SAFE_INTEGER:\n",
        "        if False:\n",
        [PY_RUNNER_FAILS, "FAIL [line_integer_past_range_refused] field=result_line.expect"],
    ),
    mutant(
        "py result line: a boolean is not an integer",
        "py/run_fixtures.py",
        '    if isinstance(value, bool):\n        return "true" if value else "false"\n    if isinstance(value, str):\n        return _result_string(value)\n',
        "    if isinstance(value, str):\n        return _result_string(value)\n",
        [PY_RUNNER_FAILS, "FAIL [line_booleans_and_null] field=result_line.expect"],
    ),
    mutant(
        "py result line: a map with a key that is not a string is refused",
        "py/run_fixtures.py",
        "        if not all(isinstance(key, str) for key in value):\n",
        "        if False:\n",
        [PY_RUNNER_FAILS, "FAIL [line_non_string_key_refused] field=result_line"],
    ),
    mutant(
        "py result line: an unpaired surrogate is escaped",
        "py/run_fixtures.py",
        '        f"\\\\u{ord(ch):04x}" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in escaped\n',
        "        ch for ch in escaped\n",
        [PY_RUNNER_FAILS, "FAIL [line_string_lone_surrogates_are_escaped] field=result_line.expect"],
    ),
    mutant(
        "py result line: text beyond ASCII is written as itself",
        "py/run_fixtures.py",
        "    escaped = json.dumps(text, ensure_ascii=False)\n",
        "    escaped = json.dumps(text)\n",
        [PY_RUNNER_FAILS, "FAIL [line_string_beyond_ascii_is_written_as_itself] field=result_line.expect"],
    ),
    # the instrument: each way a result_line expectation can be wrong is reported
    mutant(
        "ts runner: a wrong result line is reported",
        "ts/run-fixtures.ts",
        '    if (!deepEqual(actual, tc.expect)) {\n      fail(tc.id, "result_line.expect", tc.expect, actual);\n',
        '    if (false) {\n      fail(tc.id, "result_line.expect", tc.expect, actual);\n',
        ["typescript did not report a mismatch for a result line with two members swapped"],
    ),
    mutant(
        "ts runner: a line expected where the result is refused is reported",
        "ts/run-fixtures.ts",
        '    if (!deepEqual(actual, tc.expect)) {\n      fail(tc.id, "result_line.expect", tc.expect, actual);\n',
        '    if ("line" in actual && !deepEqual(actual, tc.expect)) {\n      fail(tc.id, "result_line.expect", tc.expect, actual);\n',
        ["typescript did not report a mismatch for a refused result rewritten as a line"],
        ["a result line with two members swapped"],
    ),
    mutant(
        "ts runner: a refusal expected where the result is a line is reported",
        "ts/run-fixtures.ts",
        '    if (!deepEqual(actual, tc.expect)) {\n      fail(tc.id, "result_line.expect", tc.expect, actual);\n',
        '    if ("line" in tc.expect && !deepEqual(actual, tc.expect)) {\n      fail(tc.id, "result_line.expect", tc.expect, actual);\n',
        ["typescript did not report a mismatch for a result line rewritten as a refusal"],
        ["a result line with two members swapped"],
    ),
    mutant(
        "py runner: a wrong result line is reported",
        "py/run_fixtures.py",
        '        if actual != tc["expect"]:\n            _fail(tc["id"], "result_line.expect", tc["expect"], actual)\n',
        '        if False:\n            _fail(tc["id"], "result_line.expect", tc["expect"], actual)\n',
        ["python did not report a mismatch for a result line with two members swapped"],
    ),
    mutant(
        "py runner: a line expected where the result is refused is reported",
        "py/run_fixtures.py",
        '        if actual != tc["expect"]:\n            _fail(tc["id"], "result_line.expect", tc["expect"], actual)\n',
        '        if "line" in actual and actual != tc["expect"]:\n            _fail(tc["id"], "result_line.expect", tc["expect"], actual)\n',
        ["python did not report a mismatch for a refused result rewritten as a line"],
        ["a result line with two members swapped"],
    ),
    mutant(
        "py runner: a refusal expected where the result is a line is reported",
        "py/run_fixtures.py",
        '        if actual != tc["expect"]:\n            _fail(tc["id"], "result_line.expect", tc["expect"], actual)\n',
        '        if "line" in tc["expect"] and actual != tc["expect"]:\n            _fail(tc["id"], "result_line.expect", tc["expect"], actual)\n',
        ["python did not report a mismatch for a result line rewritten as a refusal"],
        ["a result line with two members swapped"],
    ),
    # the literal grammar: the language's own number parser does not decide
    mutant(
        "ts runner: a $number literal is read by the grammar, not by Number()",
        "ts/run-fixtures.ts",
        "      if (!NUMBER_LITERAL.test(inner)) {\n",
        "      if (false) {\n",
        [
            "typescript did not refuse the $number literal '0x10'",
            "typescript did not refuse the $number literal ''",
            "typescript did not refuse the $number literal ' 7 '",
            "typescript did not refuse the $number literal '01'",
        ],
    ),
    mutant(
        "ts runner: the $number grammar has no leading zeros",
        "ts/run-fixtures.ts",
        "const NUMBER_LITERAL = /^(-?(0|[1-9][0-9]*)(\\.[0-9]+)?",
        "const NUMBER_LITERAL = /^(-?([0-9]+)(\\.[0-9]+)?",
        ["typescript did not refuse the $number literal '01'"],
        ["the $number literal '0x10'"],
    ),
    mutant(
        "ts runner: a $bigint literal is read by the grammar, not by BigInt()",
        "ts/run-fixtures.ts",
        "      if (!INTEGER_LITERAL.test(inner)) {\n",
        "      if (false) {\n",
        [
            "typescript did not refuse the $bigint literal '0x10'",
            "typescript did not refuse the $bigint literal ''",
        ],
    ),
    mutant(
        "py runner: a $number literal is read by the grammar, not by float()",
        "py/run_fixtures.py",
        "            if not _NUMBER_LITERAL.fullmatch(inner):\n",
        "            if False:\n",
        [
            "python did not refuse the $number literal '1_0'",
            "python did not refuse the $number literal ' 7 '",
            "python did not refuse the $number literal 'nan'",
            "python did not refuse the $number literal '01'",
        ],
    ),
    mutant(
        "py runner: the $number grammar covers the whole literal",
        "py/run_fixtures.py",
        "            if not _NUMBER_LITERAL.fullmatch(inner):\n",
        "            if not _NUMBER_LITERAL.match(inner):\n",
        ["python did not refuse the $number literal '1_0'"],
        ["the $number literal 'nan'"],
    ),
    mutant(
        "py runner: a $bigint literal is read by the grammar, not by int()",
        "py/run_fixtures.py",
        '            if not _INTEGER_LITERAL.fullmatch(inner):\n                raise ValueError(\n                    f"$bigint literal',
        '            if False:\n                raise ValueError(\n                    f"$bigint literal',
        ["python did not refuse the $bigint literal '1_0'"],
    ),
    # the breaker contract as written: full Unicode lowercasing, not an ASCII fold
    mutant(
        "ts breaker: a message is lowercased by the full Unicode conversion",
        "ts/breaker.ts",
        "  const normalized = message.toLowerCase();\n",
        "  const normalized = message.replace(/[A-Z]/g, (letter) => letter.toLowerCase());\n",
        [TS_RUNNER_FAILS, "FAIL [classify_kelvin_sign_folds_to_k] field=classify.expect"],
    ),
    mutant(
        "py breaker: a message is lowercased by the full Unicode conversion",
        "py/haltrule/breaker.py",
        "    normalized = message.lower()\n",
        "    normalized = message.translate({code: code + 32 for code in range(65, 91)})\n",
        [PY_RUNNER_FAILS, "FAIL [classify_kelvin_sign_folds_to_k] field=classify.expect"],
    ),
    mutant(
        "ts breaker: U+0130 lowercases to two characters, not to a plain i",
        "ts/breaker.ts",
        "  const normalized = message.toLowerCase();\n",
        '  const normalized = message.replaceAll("\\u0130", "i").toLowerCase();\n',
        [TS_RUNNER_FAILS, "FAIL [classify_dotted_capital_i_is_not_a_plain_i] field=classify.expect"],
    ),
    mutant(
        "py breaker: U+0130 lowercases to two characters, not to a plain i",
        "py/haltrule/breaker.py",
        "    normalized = message.lower()\n",
        '    normalized = message.replace("\\u0130", "i").lower()\n',
        [PY_RUNNER_FAILS, "FAIL [classify_dotted_capital_i_is_not_a_plain_i] field=classify.expect"],
    ),
    mutant(
        "py breaker: a doubling past a double's range is the cap, not an error",
        "py/haltrule/breaker.py",
        "    try:\n        power = 2.0**exponent\n    except OverflowError:\n        power = math.inf\n",
        "    power = 2.0**exponent\n",
        [PY_RUNNER_FAILS, "FAIL [backoff_attempt_past_double_range]"],
    ),
    # the gates' own lines for the new part
    mutant(
        "fixtures: the inventory holds the protocol vectors too",
        "scripts/check.sh",
        "list_files fixture_files fixtures -type f -name '*.json'\n",
        "list_files fixture_files fixtures -type f -name '*.json' ! -path '*protocol*'\n",
        ["the fixture inventory (4 files) is missing: fixtures/protocol/v0.json"],
    ),
    mutant(
        "fixtures: the result-line vectors have a floor",
        "fixtures/protocol/v0.json",
        '  "result_line": [\n',
        '  "result_line_unused": [\n',
        ["could not read fixture case counts"],
    ),
]


if __name__ == "__main__":
    sys.exit(main())
