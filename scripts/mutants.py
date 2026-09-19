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

    python3 scripts/mutants.py [--jobs N] [--only ID-PREFIX]

Standard library only. CI runs every mutant on every push and pull request.
"""

from __future__ import annotations

import argparse
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


# ---------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument(
        "--only", default="", help="run only mutants whose id starts with this"
    )
    args = parser.parse_args()

    selected = [m for m in CATALOG if m.id.startswith(args.only)]
    problems = catalog_problems(CATALOG + [SURVIVOR])
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
        'export const loadedAt = Object.getPrototypeOf(function () {}).constructor("return Date.now()")();\nexport function canonicalize(value: unknown)',
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
    mutant(
        "ts determinism: a caught clock read the static scan cannot see",
        "ts/checkpoint.ts",
        "function encodeValue(value: unknown, at: string, depth: number): string {\n",
        'function encodeValue(value: unknown, at: string, depth: number): string {\n  try { Object.getPrototypeOf(function () {}).constructor("return Date.now()")(); } catch {}\n',
        ["policy code used Date.now"],
        ["(static)"],
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
        "for name, file in modules[:-1]:",
        ["loaded outside the seal"],
    ),
    mutant(
        "check.sh: the policy file lists are not empty",
        "scripts/check.sh",
        "find ts -type f -name '*.ts' ! -path ts/run-fixtures.ts",
        "find ts -type f -name '*.tsx' ! -path ts/run-fixtures.ts",
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
        'modules = [("haltrule", "py/haltrule/__init__.py")] + [',
        "import haltrule  # noqa: E402\nmodules = [",
        ["module haltrule loaded outside the seal"],
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
                "module hashlib loaded outside the seal",
            ]
        ),
    ),
    Mutant(
        "check.sh: a module that names itself sealed is still one the seal did not load",
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
        tuple(["module haltrule.sub loaded outside the seal"]),
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
        'py_sealed_out=$(python3 "$py_seal" 2>&1)',
        'py_sealed_out=$(look_alike=$(mktemp -d); printf \'from _hashlib import openssl_sha256 as sha256\\n\' > "$look_alike/hashlib.py"; PYTHONPATH="$look_alike" python3 "$py_seal" 2>&1; rm -rf "$look_alike")',
        ["module hashlib is not the standard library's"],
    ),
]


if __name__ == "__main__":
    sys.exit(main())
