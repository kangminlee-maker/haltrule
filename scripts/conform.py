#!/usr/bin/env python3
"""The one conformance driver.

A port conforms when the lines its adapter prints are, byte for byte, the lines
the fixtures expect. The adapter is the only piece written per language: it
reads the fixture files, feeds each case to the port, and prints one result
line per case (fixtures/README.md, "Result lines"). Everything that judges is
here, once, for every language: which line each case expects, whether a fixture
file is well formed, and whether a wrong expectation would be noticed.

    conform.py check [--every-input] <adapter command...>   compare an adapter's lines with the fixtures
    conform.py self-test                                    show that this driver can fail

A few cases hand a part what not every language can hold - a string with an
unpaired surrogate, an $unsupported value. A port whose types cannot build such
an input prints {"id", "section", "unbuildable": true} for it and conforms on
the rest; the driver accepts that for those cases only - it reads which they
are off the input itself - and says how many there were. --every-input is for a
port in a language that can build them all: it may not say unbuildable at all.

It imports nothing from any port, the Python one included.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def check(command: list[str], every_input: bool) -> int:
    cases = read_fixtures()
    run = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, timeout=300)
    try:
        output = run.stdout.decode("utf-8")
    except UnicodeDecodeError:
        print("FAIL [output] field=output.encoding — the output is not UTF-8")
        return 1
    failures, unbuilt = compare(cases, output, every_input)
    if run.returncode != 0:
        failures.append(
            f"FAIL [output] field=adapter.exit — the adapter exited {run.returncode}"
        )
    for failure in failures:
        print(failure)
    if failures:
        print(f"{len(failures)} problem(s) across {len(cases)} cases")
        return 1
    if unbuilt:
        print(
            f"OK: {len(cases)} cases: {len(cases) - unbuilt} lines as the fixtures expect them, "
            f"and {unbuilt} inputs this port's types cannot hold"
        )
    else:
        print(f"OK: {len(cases)} cases, each line as the fixtures expect it")
    return 0


# -------------------------------------------------------------- the self-test

_VALID = {
    "fixture_version": "part/v0",
    "section": [{"id": "a", "input": {"$number": "1"}, "expect": {"n": 1}}],
}


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
    for problem in problems:
        print(f"FAIL [self-test] {problem}")
    if problems:
        return 1
    print(
        f"OK: every one of {len(cases)} corrupted expectations fails under its own id; malformed fixtures and malformed output are refused"
    )
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv[:2] == ["check", "--every-input"] and len(argv) > 2:
            return check(argv[2:], every_input=True)
        if argv[:1] == ["check"] and len(argv) > 1:
            return check(argv[1:], every_input=False)
        if argv == ["self-test"]:
            return self_test()
    except Malformed as error:
        print(f"FAIL [fixtures] {error}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
