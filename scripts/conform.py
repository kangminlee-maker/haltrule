#!/usr/bin/env python3
"""The one conformance driver.

A port conforms when the lines its adapter prints are, byte for byte, the lines
the fixtures expect, and when it keeps the argument contract over inputs nobody
wrote. The adapter is the only piece written per language: it reads the case
files it is given, feeds each case to the port, and prints one result line per
case (fixtures/README.md, "Result lines"). Everything that judges is here, once,
for every language: which line each case expects, whether a fixture file is
well formed, what the contract makes of a generated call, and whether a wrong
expectation would be noticed.

    conform.py check [--every-input] <adapter command...>   the fixtures and the contract, one port
    conform.py identity <adapter command> -- <adapter command> ...   the same answer from every port
    conform.py judge <lines file>                           check, over lines already written
    conform.py cli <cli command...>                         the fixtures through a shell program, one process per case
    conform.py generate                                     write the generated cases; print the path
    conform.py self-test                                    show that this driver can fail

The fixtures are examples, each with an answer written by hand. The contract
(spec/contract.json) is a rule over every call, and is checked as one: from a
fixed seed, scripts/contract.py makes calls inside it and calls with one defect,
and three properties are asked of the answers - a defect is refused, a call
inside the contract is not, and every port gives it the same line. `check` runs
an adapter over the fixture files and the generated file together; `identity`
runs several over the generated file and compares.

A few cases hand a part what not every language can hold - a string with an
unpaired surrogate, an $unsupported value. A port whose types cannot build such
an input prints {"id", "section", "unbuildable": true} for it and conforms on
the rest; the driver accepts that for those cases only - it reads which they
are off the input itself - and says how many there were. --every-input is for a
port in a language that can build them all: it may not say unbuildable at all.

A CLI is a program of one port that takes an entry point's arguments as a JSON
object and prints the answer, with the worst verdict in it as the exit code
(py/cli.py says the rest). `cli` runs one process per fixture case a shell can
make - every section but run, which takes a function, and the line format's own
vectors, over every input some JSON text can carry and this driver's own reader
reads - and holds the answer, `message` aside, and the exit code to the case's
expectation.

It imports nothing from any port, the Python one included.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract  # noqa: E402  (the driver's own, beside this file)

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / ".generated" / "contract.json"
SAFE_INTEGER = 2**53 - 1
NUMBER_LITERAL = re.compile(
    r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?|NaN|Infinity|-Infinity"
)
INTEGER_LITERAL = re.compile(r"-?(0|[1-9][0-9]*)")
# The three a decimal cannot spell, and the only $number literals with no value to compare.
SPELLED_OUT = {"NaN", "Infinity", "-Infinity"}
UNSUPPORTED_KINDS = {"undefined", "instance", "non_string_key", "sparse_array"}

Case = tuple[
    str, str, object, bool
]  # section, id, expect, and: can every language hold its input?


class Malformed(Exception):
    """A fixture file that must not be run."""


# ------------------------------------------------------------------ the line


def line_of(value) -> str:
    """A result line, as fixtures/README.md defines it."""
    if value is None:
        return "null"
    if value is True or value is False:
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = json.dumps(value, ensure_ascii=False)
        return "".join(
            f"\\u{ord(ch):04x}" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in escaped
        )
    if isinstance(value, int) and abs(value) <= SAFE_INTEGER:
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(line_of(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return (
            "{"
            + ",".join(f"{line_of(key)}:{line_of(value[key])}" for key in keys)
            + "}"
        )
    raise Malformed(f"a result line cannot carry {value!r}")


# -------------------------------------------------------------- the fixtures


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Malformed(
            f"an object repeats a key: {sorted(k for k in keys if keys.count(k) > 1)[0]!r}"
        )
    return dict(pairs)


def _a_double_reads_it_back(literal: str) -> bool:
    """The double a port is handed is the number the literal says.

    A port never sees the text, so the only thing a case can be about is the double the literal
    reads as. Two ways a literal can name one number and mean another: a written integer the
    double does not hold, where `1e300` reads as a number 284 digits larger and `9007199254740993`
    as one smaller; and a written fraction that reads as an integer, where `9007199254740991.5`
    is 2^53 and `1e-400` is zero. Either way the case tests a value nobody wrote down, and the
    two kinds are not interchangeable: a port may render an integer-valued double as an integer."""
    exact = decimal.Decimal(literal)
    held = decimal.Decimal(float(literal))
    if exact == exact.to_integral_value():
        return held == exact
    return held != held.to_integral_value()


def _check_input(node, where: str) -> None:
    """An input holds no raw number, and a $ tag is one of the three, well spelled."""
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        raise Malformed(
            f'{where}: raw JSON number {node!r} in an input; write {{"$number": "{node}"}}'
        )
    if isinstance(node, list):
        for item in node:
            _check_input(item, where)
    if not isinstance(node, dict):
        return
    tags = [key for key in node if key.startswith("$")]
    if tags:
        if len(node) != 1 or not isinstance(node[tags[0]], str):
            raise Malformed(f"{where}: {tags[0]} must be the only key, with a string")
        tag, literal = tags[0], node[tags[0]]
        if tag == "$number":
            if not NUMBER_LITERAL.fullmatch(literal):
                raise Malformed(
                    f"{where}: $number literal {literal!r} is outside the fixture grammar"
                )
            if literal not in SPELLED_OUT and not _a_double_reads_it_back(literal):
                # A plain integer has somewhere else to go; anything else has to be rewritten.
                advice = (
                    "write $bigint"
                    if INTEGER_LITERAL.fullmatch(literal)
                    else "write the value a double holds"
                )
                raise Malformed(
                    f"{where}: $number {literal} is one number written and another read"
                    f" as a double; {advice}"
                )
        elif tag == "$bigint":
            if not INTEGER_LITERAL.fullmatch(literal):
                raise Malformed(
                    f"{where}: $bigint literal {literal!r} is outside the fixture grammar"
                )
        elif tag == "$unsupported":
            if literal not in UNSUPPORTED_KINDS:
                raise Malformed(f"{where}: unknown $unsupported kind {literal!r}")
        else:
            raise Malformed(f"{where}: unknown tag {tag}")
        return
    for value in node.values():
        _check_input(value, where)


def every_language_holds(node) -> bool:
    """False for an input some languages cannot build at all: a string or a key with an unpaired
    surrogate (json.loads has already joined the pairs, so any surrogate left is alone), a $bigint
    outside a signed 64-bit integer, which is the widest integer a language need not build out of
    parts, or one of the $unsupported values. Decided from the input alone; it is not a list anyone
    keeps. A $number is a double in every language, however large, so it is held everywhere."""
    if isinstance(node, str):
        return not any(0xD800 <= ord(character) <= 0xDFFF for character in node)
    if isinstance(node, list):
        return all(every_language_holds(item) for item in node)
    if isinstance(node, dict):
        if "$unsupported" in node:
            return False
        if "$bigint" in node:
            digits = node["$bigint"].lstrip("-")
            # _check_input has already refused a literal outside the grammar; the guard is so that a
            # broken grammar check fails there and not here. Past 19 digits it is past the range, and
            # reading it would hit Python's own limit on how long an integer may be spelled.
            if not digits.isascii() or not digits.isdigit() or len(digits) > 19:
                return False
            return -(2**63) <= int(node["$bigint"]) < 2**63
        return all(
            every_language_holds(key) and every_language_holds(value)
            for key, value in node.items()
        )
    return True


def answers_refused(expect) -> bool:
    """True when a refusal is part of the answer, wherever it sits: a charge case answers one verdict
    per charge, and a charge the budget refuses is one of them."""
    if isinstance(expect, dict):
        return expect == {"refused": True} or any(
            answers_refused(value) for value in expect.values()
        )
    return isinstance(expect, list) and any(answers_refused(item) for item in expect)


def unbuildable_line(section: str, case_id: str) -> str:
    """What a port prints for a case whose input its types cannot hold."""
    return line_of({"id": case_id, "section": section, "unbuildable": True})


def read_fixture(name: str, raw: bytes) -> list[Case]:
    """The cases of one fixture file, `name` being its path under fixtures/ without .json."""
    try:
        doc = json.loads(raw.decode("ascii"), object_pairs_hook=_no_duplicate_keys)
    except UnicodeDecodeError:
        raise Malformed(
            f"{name}: not ASCII; write every other character as a \\u escape"
        ) from None
    except json.JSONDecodeError as error:
        raise Malformed(f"{name}: not JSON ({error})") from None
    if not isinstance(doc, dict) or doc.get("fixture_version") != name:
        raise Malformed(f"{name}: fixture_version must be {name!r}")
    cases: list[Case] = []
    for section, entries in doc.items():
        if section == "fixture_version":
            continue
        if not isinstance(entries, list):
            raise Malformed(f"{name}: section {section!r} is not a list of cases")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or "expect" not in entry
            ):
                raise Malformed(f"{name}: a {section} case lacks an id or an expect")
            where = f"{name} [{entry['id']}]"
            inputs = {
                key: value
                for key, value in entry.items()
                if key not in ("id", "expect")
            }
            _check_input(inputs, where)
            problem = contract.protocol_problem(section, inputs)
            if problem:
                raise Malformed(f"{where}: {problem}")
            expect = entry["expect"]
            try:
                line_of(expect)
            except Malformed as error:
                raise Malformed(f"{where}: expect: {error}") from None
            # A digest is sha256 of its canonical form: checked here with no port involved.
            if isinstance(expect, dict) and {"canonical", "digest"} <= expect.keys():
                digest = (
                    "sha256:"
                    + hashlib.sha256(
                        expect["canonical"].encode("utf-8", "surrogatepass")
                    ).hexdigest()
                )
                if digest != expect["digest"]:
                    raise Malformed(
                        f"{where}: the digest is not sha256 of the canonical form"
                    )
            cases.append((section, entry["id"], expect, every_language_holds(inputs)))
    return cases


def read_fixtures() -> list[Case]:
    """Every case of every fixture file, in the order adapters print them: files
    by path, then the file's own order."""
    cases: list[Case] = []
    for path in sorted(
        (ROOT / "fixtures").glob("*/*.json"),
        key=lambda p: p.relative_to(ROOT).as_posix(),
    ):
        cases += read_fixture(
            path.relative_to(ROOT / "fixtures").with_suffix("").as_posix(),
            path.read_bytes(),
        )
    ids = [case_id for _, case_id, _, _ in cases]
    repeated = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if repeated:
        raise Malformed(f"a case id is used twice: {repeated[0]}")
    if not cases:
        raise Malformed("no fixture cases found")
    return cases


# --------------------------------------------------------------- the verdict


def compare(
    cases: list[Case], output: str, every_input: bool = False
) -> tuple[list[str], int]:
    """What is wrong with an adapter's output - nothing, when it conforms - and how many inputs the
    port said its types cannot hold. It may say so only of an input not every language can hold, never
    where the answer is a refusal - a port whose types cannot build an argument has refused it already -
    and not at all when it is held to every input, as a port that can build them all is."""
    want = {
        (section, case_id): line_of(
            {"actual": expect, "id": case_id, "section": section}
        )
        for section, case_id, expect, _ in cases
    }
    may_be_unbuildable = {
        (section, case_id)
        for section, case_id, expect, every_language in cases
        if not every_language and not every_input and not answers_refused(expect)
    }
    unbuilt = 0
    failures: list[str] = []
    seen: list[tuple[str, str]] = []
    if output and not output.endswith("\n"):
        failures.append(
            "FAIL [output] field=output.unterminated — the last line has no newline"
        )
    for raw in output.split("\n")[:-1] if output.endswith("\n") else output.split("\n"):
        try:
            parsed = json.loads(raw)
            key = (parsed["section"], parsed["id"])
        except (ValueError, KeyError, TypeError):
            failures.append(
                f"FAIL [output] field=output.unreadable\n  line:     {raw[:300]}"
            )
            continue
        if key not in want:
            failures.append(
                f"FAIL [{key[1]}] field={key[0]}.unexpected — no fixture holds this case"
            )
        elif key in seen:
            failures.append(f"FAIL [{key[1]}] field={key[0]}.repeated")
        elif raw == unbuildable_line(*key):
            if key in may_be_unbuildable:
                unbuilt += 1
            else:
                failures.append(
                    f"FAIL [{key[1]}] field={key[0]}.unbuildable — this port has to build this input"
                )
        elif raw != want[key]:
            field = "raised" if "raised" in parsed else "expect"
            failures.append(
                f"FAIL [{key[1]}] field={key[0]}.{field}\n  expected: {want[key]}\n  actual:   {raw}"
            )
        seen.append(key)
    for section, case_id in want:
        if (section, case_id) not in seen:
            failures.append(
                f"FAIL [{case_id}] field={section}.missing — the adapter printed no line for this case"
            )
    if not failures and seen != list(want):
        failures.append(
            "FAIL [output] field=output.order — the lines are right and not in fixture order"
        )
    return failures, unbuilt


def fixture_paths() -> list[Path]:
    return sorted(
        (ROOT / "fixtures").glob("*/*.json"),
        key=lambda p: p.relative_to(ROOT).as_posix(),
    )


def generated() -> tuple[Path, dict]:
    """Writes the generated cases where every run in this tree finds them, and returns the path and
    the cells. The document is held to the fixture grammar like any file an adapter reads, and every
    input in it must be one every language builds: a generator that made one it cannot is broken."""
    doc, cells = contract.generate()
    raw = (json.dumps(doc, indent=1, ensure_ascii=True) + "\n").encode("ascii")
    name = doc["fixture_version"]
    cases = read_fixture(name, raw)
    if not all(every_language for _, _, _, every_language in cases):
        raise Malformed(f"{name}: the generator made an input not every language holds")
    if {case_id for _, case_id, _, _ in cases} != set(cells):
        raise Malformed(f"{name}: the generator's cells and cases disagree")
    contract.write(GENERATED, doc)
    return GENERATED, cells


def split_output(output: str, cells: dict) -> tuple[str, dict, list[str]]:
    """The fixture lines back as text for `compare`, the generated lines parsed by id, and what is
    wrong with the generated ones as lines: unreadable, repeated, or for no case."""
    fixture_lines: list[str] = []
    by_id: dict = {}
    failures: list[str] = []
    terminated = output.endswith("\n")
    for raw in output.split("\n")[:-1] if terminated else output.split("\n"):
        try:
            parsed = json.loads(raw)
            case_id = parsed["id"]
        except (ValueError, KeyError, TypeError):
            fixture_lines.append(raw)  # compare reports an unreadable line
            continue
        if case_id not in cells:
            fixture_lines.append(raw)
        elif case_id in by_id:
            failures.append(f"FAIL [{case_id}] field=generated.repeated")
        else:
            by_id[case_id] = parsed
    for case_id in cells:
        if case_id not in by_id:
            failures.append(
                f"FAIL [{case_id}] field=generated.missing — the adapter printed no line for this case"
            )
    text = "\n".join(fixture_lines)
    if fixture_lines and (terminated or by_id):
        text += "\n"
    return text, by_id, failures


def judge_output(
    cases: list[Case], cells: dict, output: str, every_input: bool
) -> tuple[list[str], int]:
    fixture_text, by_id, failures = split_output(output, cells)
    compared, unbuilt = compare(cases, fixture_text, every_input)
    failures = compared + failures + contract.judge(cells, by_id)
    return failures, unbuilt


def report(failures: list[str], cases: list[Case], cells: dict, unbuilt: int) -> int:
    for failure in failures:
        print(failure)
    if failures:
        print(
            f"{len(failures)} problem(s) across {len(cases)} cases and {len(cells)} generated calls"
        )
        return 1
    held = "and the port keeps the contract on {} generated calls".format(len(cells))
    if unbuilt:
        print(
            f"OK: {len(cases)} cases: {len(cases) - unbuilt} lines as the fixtures expect them, "
            f"and {unbuilt} inputs this port's types cannot hold; {held}"
        )
    else:
        print(f"OK: {len(cases)} cases, each line as the fixtures expect it, {held}")
    return 0


def run_adapter(command: list[str], paths: list[Path]) -> tuple[str, int]:
    run = subprocess.run(
        command + [str(path) for path in paths],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        timeout=300,
    )
    try:
        return run.stdout.decode("utf-8"), run.returncode
    except UnicodeDecodeError:
        return "", -1


def check(command: list[str], every_input: bool) -> int:
    cases = read_fixtures()
    path, cells = generated()
    output, code = run_adapter(command, fixture_paths() + [path])
    if code == -1:
        print("FAIL [output] field=output.encoding — the output is not UTF-8")
        return 1
    failures, unbuilt = judge_output(cases, cells, output, every_input)
    if code != 0:
        failures.append(f"FAIL [output] field=adapter.exit — the adapter exited {code}")
    return report(failures, cases, cells, unbuilt)


def judge(lines: Path) -> int:
    """`check` over lines an adapter already wrote for the fixture files and the generated file, for
    a port that runs in-process under a mutation tool."""
    cases = read_fixtures()
    _, cells = generated()
    failures, unbuilt = judge_output(
        cases, cells, lines.read_text("utf-8"), every_input=False
    )
    return report(failures, cases, cells, unbuilt)


def identity(commands: list[list[str]]) -> int:
    """Every port's line for every generated call inside the contract, compared."""
    path, cells = generated()
    outputs: dict[str, dict] = {}
    failures: list[str] = []
    for command in commands:
        name = " ".join(command)
        output, code = run_adapter(command, [path])
        if code != 0:
            failures.append(f"FAIL [output] field=adapter.exit — {name} exited {code}")
        _, by_id, missing = split_output(output, cells)
        failures += [f"{line} ({name})" for line in missing]
        outputs[name] = by_id
    failures += contract.disagreements(cells, outputs)
    for failure in failures:
        print(failure)
    accepted = sum(1 for cell in cells.values() if cell[0] == "accepted")
    if failures:
        print(
            f"{len(failures)} problem(s) across {accepted} generated calls and {len(commands)} ports"
        )
        return 1
    print(
        f"OK: {len(commands)} ports give the same line on every one of {accepted} generated calls"
    )
    return 0


# -------------------------------------------------------------- the self-test

_VALID = {
    "fixture_version": "part/v0",
    "section": [{"id": "a", "input": {"$number": "1"}, "expect": {"n": 1}}],
}


# ------------------------------------------------------------------ the cli

VERDICT_LEVELS = {"ok": 0, "warning": 1, "halt": 2}
CLI_SECTIONS_OUT = ("run", "result_line")


def read_calls() -> list[tuple[str, str, dict, object]]:
    """(section, id, inputs, expect) of every fixture case, in the adapters' order, once
    read_fixtures has held every file to the grammar."""
    read_fixtures()
    calls = []
    for path in fixture_paths():
        doc = json.loads(path.read_bytes())
        for section, entries in doc.items():
            if section == "fixture_version":
                continue
            for entry in entries:
                inputs = {k: v for k, v in entry.items() if k not in ("id", "expect")}
                calls.append((section, entry["id"], inputs, entry["expect"]))
    return calls


def plain_json(node) -> str | None:
    """A fixture input as the JSON text a caller would write: a $number or $bigint literal is
    the number itself. None where no JSON text can carry the value - an $unsupported one."""
    if isinstance(node, dict):
        if len(node) == 1:
            ((tag, literal),) = node.items()
            if tag in ("$number", "$bigint"):
                return literal
            if tag == "$unsupported":
                return None
        members = []
        for key, value in node.items():
            inner = plain_json(value)
            if inner is None:
                return None
            members.append(json.dumps(key) + ":" + inner)
        return "{" + ",".join(members) + "}"
    if isinstance(node, list):
        items = [plain_json(item) for item in node]
        if any(item is None for item in items):
            return None
        return "[" + ",".join(items) + "]"
    return json.dumps(node)


def readable(text: str) -> bool:
    """Whether this driver's own JSON reader reads the text: it asks a cli nothing it cannot read
    itself - an integer past Python's digit limit is the one such input in the fixtures."""
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def worst_verdict(node) -> int:
    """The worst verdict anywhere in an answer or an expectation; 0 where there is none."""
    if isinstance(node, dict):
        own = VERDICT_LEVELS.get(node.get("verdict"), 0)
        return max([own] + [worst_verdict(value) for value in node.values()])
    if isinstance(node, list):
        return max([0] + [worst_verdict(item) for item in node])
    return 0


def without_messages(node):
    """An answer as the fixtures write it: a verdict's `message` is for people, not conformance."""
    if isinstance(node, dict):
        return {
            key: without_messages(value)
            for key, value in node.items()
            if not (key == "message" and node.get("verdict") in VERDICT_LEVELS)
        }
    if isinstance(node, list):
        return [without_messages(item) for item in node]
    return node


def judge_cli(section: str, case_id: str, expect, stdout: str, code: int) -> list[str]:
    """What is wrong with one case's run through a cli: the answer, `message` aside, must be the
    expectation, and the exit code its worst verdict; a refusal prints nothing and exits 3."""
    if expect == {"refused": True}:
        if stdout != "" or code != 3:
            return [
                f"FAIL [{case_id}] field={section}.cli_exit — a refusal exits 3 and prints nothing; got exit {code}: {stdout[:200]!r}"
            ]
        return []
    if stdout.count("\n") != 1 or not stdout.endswith("\n"):
        return [
            f"FAIL [{case_id}] field={section}.cli_output — not one line: {stdout[:300]!r}"
        ]
    try:
        actual = line_of(without_messages(json.loads(stdout)))
    except (ValueError, Malformed) as error:
        return [f"FAIL [{case_id}] field={section}.cli_output — unreadable: {error}"]
    failures = []
    if actual != line_of(expect):
        failures.append(
            f"FAIL [{case_id}] field={section}.cli_answer\n  expected: {line_of(expect)}\n  actual:   {actual}"
        )
    if code != worst_verdict(expect):
        failures.append(
            f"FAIL [{case_id}] field={section}.cli_exit — expected {worst_verdict(expect)}, got {code}"
        )
    return failures


def cli(command: list[str]) -> int:
    """Every fixture case a shell can make, through the cli, one process each."""
    entry_of = {
        entry["section"]: name
        for name, entry in contract.load()["entry_points"].items()
    }
    runnable, uncarried = [], 0
    for section, case_id, inputs, expect in read_calls():
        if section in CLI_SECTIONS_OUT:
            continue
        text = plain_json(inputs)
        if text is None or not readable(text):
            uncarried += 1
            continue
        runnable.append((section, case_id, text, expect))

    def one(item) -> list[str]:
        section, case_id, text, expect = item
        done = subprocess.run(
            [*command, entry_of[section], "-"],
            input=text.encode("ascii"),
            capture_output=True,
        )
        return judge_cli(
            section,
            case_id,
            expect,
            done.stdout.decode("utf-8", "replace"),
            done.returncode,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        failures = [failure for found in pool.map(one, runnable) for failure in found]
    for failure in failures:
        print(failure)
    if failures:
        print(
            f"{len(failures)} problem(s) across {len(runnable)} cases through the cli"
        )
        return 1
    print(
        f"OK: {len(runnable)} cases through the cli, each answered as the fixtures expect it and exited"
        f" with its worst verdict; {uncarried} inputs no JSON text can carry"
    )
    return 0


def cli_judge_self_test() -> list[str]:
    """The cli judge held to the same standard: a wrong answer, a wrong exit code, a line too
    many or too few, and an answer where a refusal was expected must each fail."""
    problems = []
    expect = {"spec": "haltrule/0", "verdict": "halt", "reason": "x", "resume": None}
    right = json.dumps({**expect, "message": "m"}) + "\n"
    if judge_cli("s", "a", expect, right, 2):
        problems.append("a right cli answer with the right exit code failed")
    if not judge_cli("s", "a", expect, right, 0):
        problems.append("a wrong cli exit code passed")
    if not judge_cli(
        "s",
        "a",
        expect,
        json.dumps({**expect, "reason": "y", "message": "m"}) + "\n",
        2,
    ):
        problems.append("a wrong cli answer passed")
    if not judge_cli("s", "a", expect, right + right, 2):
        problems.append("two cli lines passed")
    if not judge_cli("s", "a", expect, "", 2):
        problems.append("no cli line passed")
    if judge_cli("s", "a", {"refused": True}, "", 3):
        problems.append("a refusal answered as a refusal failed")
    if not judge_cli("s", "a", {"refused": True}, right, 2):
        problems.append("an answer where a refusal was expected passed")
    both = [{**expect, "message": "m"}, {**expect, "verdict": "ok", "message": "m"}]
    if not judge_cli(
        "s", "a", [expect, {**expect, "verdict": "ok"}], json.dumps(both) + "\n", 0
    ):
        problems.append("an exit code that is not the worst verdict passed")
    if (
        plain_json({"a": {"$number": "1.5"}, "b": [{"$bigint": "9223372036854775807"}]})
        != '{"a":1.5,"b":[9223372036854775807]}'
    ):
        problems.append("plain_json does not write a literal as the number itself")
    if plain_json({"a": [{"$unsupported": "undefined"}]}) is not None:
        problems.append("plain_json wrote a value no JSON text can carry")
    if readable("1" * 5001) or not readable("[1]"):
        problems.append("readable does not say what the driver's reader reads")
    return problems


def _malformed_table():
    """(what is wrong, the file's bytes, a piece of the refusal)."""

    def doc(change) -> bytes:
        copy = json.loads(json.dumps(_VALID))
        change(copy)
        return json.dumps(copy).encode("ascii")

    def case(copy):
        return copy["section"][0]

    yield (
        "a raw number in an input",
        doc(lambda d: case(d).update(input=7)),
        "raw JSON number",
    )
    yield (
        "a raw 1.0 in an input",
        b'{"fixture_version":"part/v0","section":[{"id":"a","input":1.0,"expect":null}]}',
        "raw JSON number",
    )
    for literal in ("1_0", "0x10", "", " 7 ", "nan", "01", "+1", "1.", ".5"):
        yield (
            f"the $number literal {literal!r}",
            doc(lambda d, s=literal: case(d).update(input={"$number": s})),
            "outside the fixture grammar",
        )
    for literal in ("1_0", "0x10", "", "1.0", "1e3"):
        yield (
            f"the $bigint literal {literal!r}",
            doc(lambda d, s=literal: case(d).update(input={"$bigint": s})),
            "outside the fixture grammar",
        )
    for literal in ("9007199254740993", "-9007199254740993"):
        yield (
            f"the $number {literal}, which a double rounds",
            doc(lambda d, s=literal: case(d).update(input={"$number": s})),
            "another read as a double; write $bigint",
        )
    for literal in ("1e300", "-1e308", "1.7976931348623157e308"):
        yield (
            f"the $number {literal}, an integer written and a different integer read",
            doc(lambda d, s=literal: case(d).update(input={"$number": s})),
            "another read as a double; write the value a double holds",
        )
    for literal in ("9007199254740991.5", "1e-400"):
        yield (
            f"the $number {literal}, a fraction written and an integer read",
            doc(lambda d, s=literal: case(d).update(input={"$number": s})),
            "another read as a double; write the value a double holds",
        )
    yield (
        "an unknown $unsupported kind",
        doc(lambda d: case(d).update(input={"$unsupported": "no_such_kind"})),
        "unknown $unsupported kind",
    )
    yield (
        "an unknown tag",
        doc(lambda d: case(d).update(input={"$float": "1"})),
        "unknown tag",
    )
    yield (
        "a tag beside another key",
        doc(lambda d: case(d).update(input={"$number": "1", "x": "y"})),
        "must be the only key",
    )
    for what, events in [
        ("an event whose kind names no call", [{"kind": "unknown", "item_id": "x"}]),
        ("an event without a kind", [{"item_id": "x"}]),
        ("an event that is not a map", ["not-a-report"]),
    ]:
        yield (
            what,
            json.dumps(
                {
                    "fixture_version": "part/v0",
                    "state": [
                        {"id": "a", "policy": {}, "events": events, "expect": None}
                    ],
                }
            ).encode("ascii"),
            "names no call",
        )
    for what, answers in [
        ("answers that are not a list", "x"),
        ("a script that is not a list", ["x"]),
        ("an answer that is not a map", [["x"]]),
        ("an answer whose kind names no outcome", [[{"kind": "unknown"}]]),
        (
            "a failure without its class",
            [[{"kind": "failure", "failure_message": "m"}]],
        ),
        (
            "a failure whose message is not a string",
            [
                [
                    {
                        "kind": "failure",
                        "failure_message": {"$number": "1"},
                        "failure_class": None,
                    }
                ]
            ],
        ),
        ("a success carrying a field", [[{"kind": "success", "item_id": "x"}]]),
    ]:
        yield (
            what,
            json.dumps(
                {
                    "fixture_version": "part/v0",
                    "run": [
                        {
                            "id": "a",
                            "policy": {},
                            "items": ["x"],
                            "answers": answers,
                            "expect": None,
                        }
                    ],
                }
            ).encode("ascii"),
            "no outcome",
        )
    yield (
        "a fixture_version that is not the file's path",
        doc(lambda d: d.update(fixture_version="part/v999")),
        "fixture_version must be",
    )
    yield (
        "a case without an expect",
        doc(lambda d: case(d).pop("expect")),
        "lacks an id or an expect",
    )
    yield (
        "a fraction in an expect",
        doc(lambda d: case(d).update(expect=1.5)),
        "cannot carry",
    )
    yield (
        "a non-ASCII byte",
        json.dumps(_VALID, ensure_ascii=False).replace('"a"', '"é"').encode("utf-8"),
        "not ASCII",
    )
    yield (
        "a repeated key",
        b'{"fixture_version":"part/v0","section":[{"id":"a","id":"b","expect":null}]}',
        "repeats a key",
    )
    yield (
        "a digest that is not sha256 of its canonical form",
        doc(
            lambda d: case(d).update(
                expect={"canonical": "null", "digest": "sha256:" + "0" * 64}
            )
        ),
        "not sha256",
    )


def self_test() -> int:
    problems: list[str] = []
    cases = read_fixtures()
    perfect = "".join(
        line_of({"actual": expect, "id": case_id, "section": section}) + "\n"
        for section, case_id, expect, _ in cases
    )
    lines = perfect.split("\n")[:-1]

    def failing(output: str, against=cases, every_input=False) -> list[str]:
        failures, _ = compare(against, output, every_input)
        return [failure.split("\n")[0] for failure in failures]

    if failing(perfect):
        problems.append("the lines the fixtures expect do not pass")
    # Every case, not a sample: an expectation that is wrong must fail under its own id.
    corrupted = [
        (section, case_id, {"corrupted": expect}, every)
        for section, case_id, expect, every in cases
    ]
    noticed = failing(perfect, corrupted)
    unnoticed = [
        case_id
        for section, case_id, _, _ in cases
        if f"FAIL [{case_id}] field={section}.expect" not in noticed
    ]
    if unnoticed:
        problems.append(
            f"{len(unnoticed)} corrupted expectation(s) passed, the first being {unnoticed[0]}"
        )
    # A port whose types cannot hold an input says so, and only of an input not every language holds.
    # Beyond every language, and the answer is not the refusal that being unable to build it already is.
    beyond = [case for case in cases if not case[3] and not answers_refused(case[2])]
    sits_out = {(section, case_id) for section, case_id, _, _ in beyond}
    typed = "".join(
        (unbuildable_line(section, case_id) if (section, case_id) in sits_out else line)
        + "\n"
        for line, (section, case_id, _, _) in zip(lines, cases)
    )
    if compare(cases, typed) != ([], len(beyond)) or len(beyond) < 20:
        problems.append(
            "a port that cannot build what not every language holds did not pass, or few such cases were found"
        )
    for what, expect in [
        ("the answer", {"refused": True}),
        (
            "one of the answers",
            {"verdicts": [{"refused": True}], "used": {"turns": "0"}},
        ),
    ]:
        refusing = [("part", "refuses", expect, False)]
        if not compare(refusing, unbuildable_line("part", "refuses") + "\n")[0]:
            problems.append(f"unbuildable, where a refusal is {what}, passed")
    held = " ".join(failing(typed, every_input=True))
    if (
        not beyond
        or f"FAIL [{beyond[0][1]}] field={beyond[0][0]}.unbuildable" not in held
    ):
        problems.append("a port held to every input said unbuildable and passed")
    for what, inputs, every in [
        ("a plain input", {"value": ["a\U0001f600", {"k": None}]}, True),
        ("an unpaired surrogate in a value", {"value": ["a\ud83d"]}, False),
        ("an unpaired surrogate in a key", {"value": {"\udc00": 1}}, False),
        ("an $unsupported value", {"args": [{"$unsupported": "undefined"}]}, False),
        (
            "a $bigint a signed 64-bit integer holds",
            {"v": {"$bigint": "-9223372036854775808"}},
            True,
        ),
        ("a $bigint past it", {"v": {"$bigint": "9223372036854775808"}}, False),
        ("a $bigint of 5001 digits", {"v": {"$bigint": "9" * 5001}}, False),
        ("a $number, which is a double anywhere", {"v": {"$number": "1e300"}}, True),
    ]:
        if every_language_holds(inputs) is not every:
            problems.append(
                f"{what} was read as {'beyond' if every else 'within'} every language"
            )
    section, case_id, _, every = cases[0]
    if not every:
        problems.append("the first case is no longer one every language can build")
    for what, output, evidence in [
        (
            "unbuildable, of an input every language holds",
            perfect.replace(lines[0], unbuildable_line(section, case_id), 1),
            f"FAIL [{case_id}] field={section}.unbuildable",
        ),
        (
            "a missing line",
            "\n".join(lines[1:]) + "\n",
            f"FAIL [{case_id}] field={section}.missing",
        ),
        (
            "a repeated line",
            lines[0] + "\n" + perfect,
            f"FAIL [{case_id}] field={section}.repeated",
        ),
        (
            "lines out of order",
            "\n".join([lines[1], lines[0]] + lines[2:]) + "\n",
            "field=output.order",
        ),
        (
            "a line for no case",
            perfect + '{"actual":null,"id":"no-such-case","section":"x"}\n',
            "FAIL [no-such-case] field=x.unexpected",
        ),
        ("a line that is not JSON", perfect + "not json\n", "field=output.unreadable"),
        (
            "a case that raised",
            perfect.replace(
                lines[0],
                line_of({"id": case_id, "raised": "Boom", "section": section}),
                1,
            ),
            f"FAIL [{case_id}] field={section}.raised",
        ),
        (
            "the right value, spelled with a space",
            perfect.replace(lines[0], lines[0].replace(":", ": ", 1), 1),
            f"FAIL [{case_id}] field={section}.expect",
        ),
        ("a last line without its newline", perfect[:-1], "field=output.unterminated"),
        ("no output at all", "", f"FAIL [{case_id}] field={section}.missing"),
    ]:
        if not any(evidence in failure for failure in failing(output)):
            problems.append(f"{what} passed")
    for what, raw, refusal in _malformed_table():
        try:
            read_fixture("part/v0", raw)
            problems.append(f"{what} was accepted")
        except Malformed as error:
            if refusal not in str(error):
                problems.append(f"{what} was refused for another reason: {error}")
        except Exception as error:  # a reader that blows up hides every row after it
            problems.append(f"{what} broke the reader: {error!r}")
    try:
        read_fixture("part/v0", json.dumps(_VALID).encode("ascii"))
    except Malformed as error:
        problems.append(f"a well-formed fixture was refused: {error}")
    # The driver's own line writer against the vectors written by hand.
    doc = json.loads((ROOT / "fixtures/protocol/v0.json").read_text(encoding="ascii"))
    vectors = [
        c
        for c in doc["result_line"]
        if "line" in c["expect"] and "$" not in json.dumps(c["value"])
    ]
    if len(vectors) < 10:
        problems.append(f"only {len(vectors)} protocol vectors hold a plain value")
    for vector in vectors:
        if line_of(vector["value"]) != vector["expect"]["line"]:
            problems.append(
                f"the driver writes {vector['id']} differently from the vector"
            )
    problems += generator_self_test()
    problems += cli_judge_self_test()
    for problem in problems:
        print(f"FAIL [self-test] {problem}")
    if problems:
        return 1
    print(
        f"OK: every one of {len(cases)} corrupted expectations fails under its own id; malformed fixtures and malformed output are refused; the generated contract checks and the cli judge can fail"
    )
    return 0


def generator_self_test() -> list[str]:
    """The contract judge held to the same standard as the fixture judge: it must fail. A port that
    answers a defect with anything but the refusal, or a call inside the contract with a refusal, or
    whose line differs from another port's, fails under that case's id; a generator that makes the same
    cases twice is the only kind a port can be held to."""
    problems: list[str] = []
    first, cells = contract.generate()
    if json.dumps(first) != json.dumps(contract.generate()[0]):
        problems.append("the generator does not make the same cases twice")
    kinds = {cell[0] for cell in cells.values()}
    if kinds != {"accepted", "refused"} or len(cells) < 500:
        problems.append(
            f"the generator made {len(cells)} cells of kinds {sorted(kinds)}"
        )
    by_entry = {section for section in first if section != "fixture_version"}
    if len(by_entry) < 8:
        problems.append(
            f"the generator reached {len(by_entry)} sections, not every entry point"
        )

    def perfect_line(case_id: str) -> dict:
        cell = cells[case_id]
        if cell == ("accepted",):
            return {"actual": {"ok": 1}, "id": case_id, "section": "s"}
        field_at = cell[1]
        if field_at is None:
            return {"actual": {"refused": True}, "id": case_id, "section": "s"}
        field, index = field_at
        answers = [None] * (index + 1)
        answers[index] = {"refused": True}
        return {"actual": {field: answers}, "id": case_id, "section": "s"}

    perfect = {case_id: perfect_line(case_id) for case_id in cells}
    if contract.judge(cells, perfect):
        problems.append(
            "the answers the contract asks for do not pass the contract judge"
        )
    # Every cell, not a sample: the wrong answer must fail under the cell's own id.
    unnoticed = []
    for case_id, cell in cells.items():
        wrong = dict(perfect[case_id])
        wrong["actual"] = {"ok": 1} if cell[0] == "refused" else {"refused": True}
        if cell[0] == "refused" and cell[1] is not None:
            field, index = cell[1]
            wrong["actual"] = {field: [None] * (index + 1)}
        found = contract.judge(cells, {**perfect, case_id: wrong})
        if len(found) != 1 or not found[0].startswith(
            f"FAIL [{case_id}] field=generated."
        ):
            unnoticed.append(case_id)
    if unnoticed:
        problems.append(
            f"{len(unnoticed)} wrong generated answer(s) passed, the first being {unnoticed[0]}"
        )
    accepted = next(case_id for case_id, cell in cells.items() if cell == ("accepted",))
    raised = {
        **perfect,
        accepted: {"id": accepted, "section": "s", "raised": "TypeError: x"},
    }
    if f"FAIL [{accepted}] field=generated.raised" not in " ".join(
        contract.judge(cells, raised)
    ):
        problems.append("a port that raised on a call inside the contract passed")
    unbuilt = {
        **perfect,
        accepted: {"id": accepted, "section": "s", "unbuildable": True},
    }
    if f"FAIL [{accepted}] field=generated.unbuildable" not in " ".join(
        contract.judge(cells, unbuilt)
    ):
        problems.append("a port that could not build a generated input passed")
    # Identity: one port's one line differs.
    other = {**perfect, accepted: {**perfect[accepted], "actual": {"ok": 2}}}
    found = contract.disagreements(cells, {"a": perfect, "b": perfect, "c": other})
    if len(found) != 1 or not found[0].startswith(
        f"FAIL [{accepted}] field=generated.identity"
    ):
        problems.append("ports that disagree on a call inside the contract passed")
    if contract.disagreements(cells, {"a": perfect, "b": perfect}):
        problems.append("ports that agree were said to disagree")
    # A generated line missing, or repeated, is noticed where the output is split.
    text = "".join(line_of(perfect[case_id]) + "\n" for case_id in cells)
    _, _, failures = split_output(text, cells)
    if failures:
        problems.append("a complete generated output was found wanting")
    short = "".join(line_of(perfect[case_id]) + "\n" for case_id in list(cells)[1:])
    if f"FAIL [{next(iter(cells))}] field=generated.missing" not in " ".join(
        split_output(short, cells)[2]
    ):
        problems.append("a missing generated line passed")
    if f"FAIL [{accepted}] field=generated.repeated" not in " ".join(
        split_output(text + line_of(perfect[accepted]) + "\n", cells)[2]
    ):
        problems.append("a repeated generated line passed")
    return problems


def main(argv: list[str]) -> int:
    try:
        if argv[:2] == ["check", "--every-input"] and len(argv) > 2:
            return check(argv[2:], every_input=True)
        if argv[:1] == ["check"] and len(argv) > 1:
            return check(argv[1:], every_input=False)
        if argv[:1] == ["identity"] and len(argv) > 1:
            commands: list[list[str]] = [[]]
            for word in argv[1:]:
                commands.append([]) if word == "--" else commands[-1].append(word)
            return identity([command for command in commands if command])
        if argv[:1] == ["judge"] and len(argv) == 2:
            return judge(Path(argv[1]))
        if argv[:1] == ["cli"] and len(argv) > 1:
            return cli(argv[1:])
        if argv == ["generate"]:
            path, cells = generated()
            print(path)
            return 0
        if argv == ["self-test"]:
            return self_test()
    except Malformed as error:
        print(f"FAIL [fixtures] {error}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
