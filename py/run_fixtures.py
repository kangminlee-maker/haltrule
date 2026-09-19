#!/usr/bin/env python3
"""Conformance runner for the fixtures under fixtures/ against
py/haltrule/breaker.py, checkpoint.py, budget.py and slot.py.

With no path, runs every .json file under fixtures/, in path order; with one,
runs that file only. The file's own `fixture_version` picks the part under
test, and an unknown version is an error rather than zero cases passed, so a
fixture file added under fixtures/ is run or refused, never skipped.

Loads the fixture file, feeds each case to the pure policy functions, and
diffs the actual result against the fixture's expected value. Exits non-zero
and prints every mismatch (fixture id + field + expected/actual) on any
failure - this is the instrument the "corrupt one expectation and confirm it
fails" check in fixtures/README.md exercises.

With `--dump`, skips the expected-value comparison entirely and instead
prints one canonical JSON line per case holding this implementation's actual
result (never the fixture's expectation). scripts/check.sh diffs this output
byte-for-byte against `ts/run-fixtures.ts --dump` - that is the real parity
check the README promises ("CI compares their output byte for byte"), not
just the summary line both runners happen to print at the end.

Fixture inputs never hold a raw JSON number: json and JavaScript's
JSON.parse disagree about some (1.0, anything past 2^53), so both would test
different values. A number is written {"$number": "<literal>"} and each runner
decodes the literal itself; {"$bigint": "<literal>"} is an integer (a plain int
here, a bigint in the TypeScript runner); {"$unsupported": "<kind>"} builds a
value outside the digest model that JSON cannot spell. A file section, or an
$unsupported kind, this runner does not know is an error.

Only the standard library (collections, json, re, sys, pathlib, dataclasses)
is used; no
third-party dependencies. Mirrors ts/run-fixtures.ts field-for-field so the
two runners' pass/fail verdicts, and their --dump output, are directly
comparable.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from haltrule.breaker import (  # noqa: E402
    DispatchBreakerPolicy,
    DispatchBreakerState,
    DispatchDeadLetterEntry,
    classify_systemic_dispatch_failure,
    dispatch_backoff_delay_ms,
)
from haltrule.checkpoint import (  # noqa: E402
    canonicalize,
    checkpoint_digest,
    evaluate_checkpoint_artifact,
)
from haltrule.budget import Budget  # noqa: E402
from haltrule.slot import validate_slot  # noqa: E402

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures"


def default_fixture_paths() -> list[Path]:
    # Every .json under fixtures/, in the order ts/run-fixtures.ts uses too:
    # relative paths compared as ASCII strings.
    return sorted(
        FIXTURE_ROOT.rglob("*.json"),
        key=lambda p: p.relative_to(FIXTURE_ROOT).as_posix(),
    )


_failure_count = 0
_case_count = 0


def _to_plain(value):
    """Recursively convert dataclass instances (and containers of them) to
    plain dicts/lists so they compare equal to the JSON-decoded expected
    values with plain `==` (Python's built-in equality already does the
    deep, order-independent structural comparison the TS runner's deepEqual
    implements by hand), and so they serialize with `json.dumps` for --dump.
    """
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


_SAFE_INTEGER = 2**53 - 1


def _utf16_units(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def _result_string(text: str) -> str:
    # json.dumps escapes as the protocol says, except that it leaves a lone
    # surrogate as it is - which no UTF-8 stream can carry.
    escaped = json.dumps(text, ensure_ascii=False)
    return "".join(
        f"\\u{ord(ch):04x}" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in escaped
    )


def _canonical_json(value) -> str:
    """A result line, as fixtures/README.md defines it; fixtures/protocol/v0.json
    holds its vectors. Written out member by member: `sort_keys` orders keys by
    code point, and the protocol orders them by UTF-16 code unit.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _result_string(value)
    if isinstance(value, float):
        if not value.is_integer():  # false for NaN and the infinities too
            raise ValueError(
                f"a result holds the number {value!r}; a result number is an integer within +/-(2^53 - 1)"
            )
        value = int(value)
    if isinstance(value, int):
        if abs(value) > _SAFE_INTEGER:
            raise ValueError(
                f"a result holds the number {value}; a result number is an integer within +/-(2^53 - 1)"
            )
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if type(value) is dict:
        if not all(isinstance(key, str) for key in value):
            raise ValueError("a result holds a map with a key that is not a string")
        members = (
            f"{_result_string(key)}:{_canonical_json(value[key])}"
            for key in sorted(value, key=_utf16_units)
        )
        return "{" + ",".join(members) + "}"
    raise ValueError(
        f"a result holds a value a result line cannot carry: {type(value).__name__}"
    )


def _fail(case_id: str, field: str, expected, actual) -> None:
    global _failure_count
    _failure_count += 1
    print(
        f"FAIL [{case_id}] field={field}\n"
        f"  expected: {json.dumps(expected)}\n"
        f"  actual:   {json.dumps(actual)}",
        file=sys.stderr,
    )


_RAISED = object()


def _compute_or_fail(case_id: str, section: str, compute):
    """A case whose computation raises is a failure of that case, reported
    under its id - not a crash that hides which case failed, or whether any
    case ran at all."""
    try:
        return compute()
    except Exception as error:
        _fail(case_id, f"{section}.raised", None, f"{type(error).__name__}: {error}")
        return _RAISED


def _actual_classify(tc: dict):
    # `message` may be a non-string (e.g. a JSON number) to exercise the
    # classifier's isinstance guard - matches how an untyped caller could
    # hand it anything.
    return classify_systemic_dispatch_failure(tc["message"])


def _actual_backoff(tc: dict):
    return dispatch_backoff_delay_ms(
        attempt=tc["attempt"],
        initial_ms=tc["initial_ms"],
        cap_ms=tc["cap_ms"],
    )


def _actual_state(tc: dict) -> dict:
    policy = DispatchBreakerPolicy(
        enabled=tc["policy"]["enabled"],
        systemic_threshold=tc["policy"]["systemic_threshold"],
        per_call_max_attempts=tc["policy"]["per_call_max_attempts"],
        backoff_initial_ms=tc["policy"]["backoff_initial_ms"],
        backoff_cap_ms=tc["policy"]["backoff_cap_ms"],
        concurrent=tc["policy"].get("concurrent"),
    )
    state = DispatchBreakerState(policy)
    returns = []
    for event in tc["events"]:
        if event["kind"] == "success":
            state.record_item_success(event["item_id"])
            returns.append(None)
        elif event["kind"] == "skipped":
            state.record_item_skipped(event["item_id"])
            returns.append(None)
        else:
            entry = DispatchDeadLetterEntry(
                item_id=event["item_id"],
                failure_class=event["failure_class"],
                failure_message=event["failure_message"],
                attempt_count=event["attempt_count"],
            )
            returns.append(state.record_item_failure(entry))

    return {
        "returns": _to_plain(returns),
        "completed": list(state.completed_item_ids()),
        "dead_letter": _to_plain(list(state.dead_letter_entries())),
        "tripped": _to_plain(state.tripped()),
    }


def run_classify(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(tc["id"], "classify", lambda: _actual_classify(tc))
        if actual is _RAISED:
            continue
        if actual != tc["expect"]:
            _fail(tc["id"], "classify.expect", tc["expect"], actual)


def run_backoff(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(tc["id"], "backoff", lambda: _actual_backoff(tc))
        if actual is _RAISED:
            continue
        if actual != tc["expect_ms"]:
            _fail(tc["id"], "backoff.expect_ms", tc["expect_ms"], actual)


def run_state(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(tc["id"], "state", lambda: _actual_state(tc))
        if actual is _RAISED:
            continue

        if actual["returns"] != tc["expect"]["returns"]:
            _fail(tc["id"], "state.returns", tc["expect"]["returns"], actual["returns"])
        if actual["completed"] != tc["expect"]["completed"]:
            _fail(
                tc["id"],
                "state.completed",
                tc["expect"]["completed"],
                actual["completed"],
            )
        if actual["dead_letter"] != tc["expect"]["dead_letter"]:
            _fail(
                tc["id"],
                "state.dead_letter",
                tc["expect"]["dead_letter"],
                actual["dead_letter"],
            )
        if actual["tripped"] != tc["expect"]["tripped"]:
            _fail(tc["id"], "state.tripped", tc["expect"]["tripped"], actual["tripped"])


_INTEGER_LITERAL = re.compile(r"-?(0|[1-9][0-9]*)")
# The JSON number grammar, plus the three values JSON cannot spell. float() and
# int() read more than this - "1_0", " 7 ", "nan" - and JavaScript's Number()
# and BigInt() read other things again - "0x10", "" - so neither decides.
_NUMBER_LITERAL = re.compile(
    r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?|NaN|Infinity|-Infinity"
)


def _exactly_representable(value: int) -> bool:
    try:
        return int(float(value)) == value
    except OverflowError:
        return False


def _int_from_literal(text: str) -> int:
    """int(text) without Python's int-to-str digit limit, which int() enforces
    on a literal past 4300 digits. Built in chunks rather than by raising the
    limit, because the limit must stay in force for the code under test."""
    digits = text.lstrip("-")
    value = 0
    for start in range(0, len(digits), 1000):
        chunk = digits[start : start + 1000]
        value = value * 10 ** len(chunk) + int(chunk)
    return -value if text.startswith("-") else value


def _decode_fixture_value(value):
    """Decode a fixture input: see the module docstring for why numbers
    arrive as text."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        raise ValueError(
            f"fixture input holds the raw JSON number {value!r}; write "
            f'{{"$number": "{value}"}} so both runners decode the same literal'
        )
    if isinstance(value, list):
        return [_decode_fixture_value(item) for item in value]
    if len(value) == 1:
        ((key, inner),) = value.items()
        if key == "$number" and isinstance(inner, str):
            if not _NUMBER_LITERAL.fullmatch(inner):
                raise ValueError(
                    f"$number literal {json.dumps(inner)} is outside the fixture grammar"
                )
            if not _INTEGER_LITERAL.fullmatch(inner):
                return float(inner)
            value = int(inner)
            # An integer literal a double cannot hold stays exact here and
            # rounds in JavaScript, so the two runners would test two values.
            if not _exactly_representable(value):
                raise ValueError(
                    f"$number {inner} is not exactly representable as a double; "
                    f'write {{"$bigint": "{inner}"}} so both runners decode the same value'
                )
            return value
        if key == "$bigint" and isinstance(inner, str):
            if not _INTEGER_LITERAL.fullmatch(inner):
                raise ValueError(
                    f"$bigint literal {json.dumps(inner)} is outside the fixture grammar"
                )
            return _int_from_literal(inner)
        if key == "$unsupported":
            if inner in ("undefined", "instance"):
                return object()
            if inner == "non_string_key":
                return {1: 1}
            if inner == "sparse_array":
                # Python has no holes; a list holding an unsupported element is
                # the value a hole reads as.
                return [object()]
            raise ValueError(f"unknown $unsupported kind {inner!r}")
    return {key: _decode_fixture_value(inner) for key, inner in value.items()}


def _actual_canonicalize(tc: dict) -> dict:
    value = _decode_fixture_value(tc["input"])
    canonical = canonicalize(value)
    digest = checkpoint_digest(value)
    # The two entry points must agree about the same value; if they do not,
    # that is a bug in the implementation, not a verdict to compare.
    if "halt" in canonical or "halt" in digest:
        if canonical.get("halt") is None or canonical.get("halt") != digest.get("halt"):
            raise AssertionError(
                f"[{tc['id']}] canonicalize and checkpoint_digest disagree about halting"
            )
        return {"halt": canonical["halt"]}
    return {"canonical": canonical["canonical"], "digest": digest["digest"]}


def _actual_checkpoint(tc: dict) -> list:
    args = _decode_fixture_value(tc["args"])
    actual = evaluate_checkpoint_artifact(**args)
    # validation_issues is typed Sequence: any sequence must give the verdict a
    # list gives. If it does not, that is a bug, not a verdict to compare.
    if isinstance(args.get("validation_issues"), list):
        as_sequence = dict(
            args, validation_issues=collections.UserList(args["validation_issues"])
        )
        if evaluate_checkpoint_artifact(**as_sequence) != actual:
            raise AssertionError(
                f"[{tc['id']}] validation_issues as a non-list sequence gives a different verdict"
            )
    # Text is a sequence of characters, never of issues: bytes and bytearray,
    # which no fixture can spell, must be ignored exactly as absent issues are.
    if args.get("validation_issues") is None:
        for text in (b"invalid", bytearray(b"invalid")):
            if (
                evaluate_checkpoint_artifact(**dict(args, validation_issues=text))
                != actual
            ):
                raise AssertionError(
                    f"[{tc['id']}] validation_issues as {type(text).__name__} is not ignored"
                )
    return actual


_VERDICT_FIELDS = ["message", "reason", "resume", "spec", "verdict"]


def _normative(result: dict) -> dict:
    """A verdict as conformance sees it: without `message`, which is for
    people - after checking the shape the spec promises: exactly the five
    fields, `message` a string. Its wording is not conformance; its presence
    and type are."""
    fields = sorted(result)
    if fields != _VERDICT_FIELDS:
        raise AssertionError(f"verdict has fields {fields}, not {_VERDICT_FIELDS}")
    if not isinstance(result["message"], str):
        raise AssertionError(
            f"verdict message is {type(result['message']).__name__}, not a string"
        )
    return {key: value for key, value in result.items() if key != "message"}


def _actual_charge(tc: dict) -> dict:
    budget = Budget(**_decode_fixture_value(tc["budget"]))
    verdicts = [
        _normative(budget.charge(**_decode_fixture_value(charge)))
        for charge in tc["charges"]
    ]
    # The ledger in decimal, so 2^63 - 1 survives JSON in every language.
    return {
        "verdicts": verdicts,
        "used": {
            "turns": str(budget.turns_used),
            "ms": str(budget.ms_used),
            "tokens": str(budget.tokens_used),
        },
    }


def _actual_validate(tc: dict) -> dict:
    return _normative(
        validate_slot(
            _decode_fixture_value(tc["spec"]), _decode_fixture_value(tc["value"])
        )
    )


def run_canonicalize(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(
            tc["id"], "canonicalize", lambda: _actual_canonicalize(tc)
        )
        if actual is _RAISED:
            continue
        if actual != tc["expect"]:
            _fail(tc["id"], "canonicalize.expect", tc["expect"], actual)


def run_checkpoint(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(
            tc["id"], "checkpoint", lambda: _actual_checkpoint(tc)
        )
        if actual is _RAISED:
            continue
        if actual != tc["expect"]:
            _fail(tc["id"], "checkpoint.expect", tc["expect"], actual)


def run_charge(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(tc["id"], "charge", lambda: _actual_charge(tc))
        if actual is _RAISED:
            continue
        if actual != tc["expect"]:
            _fail(tc["id"], "charge.expect", tc["expect"], actual)


def run_validate(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(tc["id"], "validate", lambda: _actual_validate(tc))
        if actual is _RAISED:
            continue
        if actual != tc["expect"]:
            _fail(tc["id"], "validate.expect", tc["expect"], actual)


def dump_classify(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {"section": "classify", "id": tc["id"], "actual": _actual_classify(tc)}
            )
        )


def dump_backoff(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {"section": "backoff", "id": tc["id"], "actual": _actual_backoff(tc)}
            )
        )


def dump_state(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {"section": "state", "id": tc["id"], "actual": _actual_state(tc)}
            )
        )


def dump_canonicalize(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {
                    "section": "canonicalize",
                    "id": tc["id"],
                    "actual": _actual_canonicalize(tc),
                }
            )
        )


def dump_checkpoint(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {
                    "section": "checkpoint",
                    "id": tc["id"],
                    "actual": _actual_checkpoint(tc),
                }
            )
        )


def dump_charge(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {"section": "charge", "id": tc["id"], "actual": _actual_charge(tc)}
            )
        )


def dump_validate(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {"section": "validate", "id": tc["id"], "actual": _actual_validate(tc)}
            )
        )


def _actual_result_line(tc: dict) -> dict:
    """The protocol's own vectors: what the serializer writes for a value,
    against a line written by hand from fixtures/README.md."""
    value = _decode_fixture_value(tc["value"])
    try:
        return {"line": _canonical_json(value)}
    except ValueError:
        return {"refused": True}


def run_result_line(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = _compute_or_fail(
            tc["id"], "result_line", lambda tc=tc: _actual_result_line(tc)
        )
        if actual is _RAISED:
            continue
        if actual != tc["expect"]:
            _fail(tc["id"], "result_line.expect", tc["expect"], actual)


def dump_result_line(cases: list[dict]) -> None:
    for tc in cases:
        print(
            _canonical_json(
                {
                    "section": "result_line",
                    "id": tc["id"],
                    "actual": _actual_result_line(tc),
                }
            )
        )


_SECTIONS = {
    "breaker/v0": ("classify", "backoff", "state"),
    "checkpoint/v0": ("canonicalize", "checkpoint"),
    "budget/v0": ("charge",),
    "slot/v0": ("validate",),
    "protocol/v0": ("result_line",),
}


def run_file(fixtures: dict, dump: bool) -> None:
    version = fixtures["fixture_version"]
    # A section no runner reads would pass with every case in it wrong.
    for key in fixtures:
        if (
            version in _SECTIONS
            and key != "fixture_version"
            and key not in _SECTIONS[version]
        ):
            raise ValueError(f"unknown fixture section {key!r} in {version}")
    if version == "breaker/v0":
        if dump:
            dump_classify(fixtures["classify"])
            dump_backoff(fixtures["backoff"])
            dump_state(fixtures["state"])
        else:
            run_classify(fixtures["classify"])
            run_backoff(fixtures["backoff"])
            run_state(fixtures["state"])
    elif version == "checkpoint/v0":
        if dump:
            dump_canonicalize(fixtures["canonicalize"])
            dump_checkpoint(fixtures["checkpoint"])
        else:
            run_canonicalize(fixtures["canonicalize"])
            run_checkpoint(fixtures["checkpoint"])
    elif version == "budget/v0":
        if dump:
            dump_charge(fixtures["charge"])
        else:
            run_charge(fixtures["charge"])
    elif version == "slot/v0":
        if dump:
            dump_validate(fixtures["validate"])
        else:
            run_validate(fixtures["validate"])
    elif version == "protocol/v0":
        if dump:
            dump_result_line(fixtures["result_line"])
        else:
            run_result_line(fixtures["result_line"])
    else:
        raise ValueError(f"unknown fixture_version {version!r}")


def main() -> None:
    # A --dump line carries non-ASCII text; write it as UTF-8 whatever the
    # locale says, as node does.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    args = sys.argv[1:]
    dump = "--dump" in args
    positional = [a for a in args if a != "--dump"]
    fixture_paths = [Path(positional[0])] if positional else default_fixture_paths()
    versions = []
    for fixture_path in fixture_paths:
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
        run_file(fixtures, dump)
        versions.append(fixtures["fixture_version"])
    if dump:
        return

    label = "fixture_version=" + ", ".join(versions)
    if _failure_count > 0:
        print(
            f"\n{_failure_count} mismatch(es) across {_case_count} cases ({label}).",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"OK: {_case_count} cases passed ({label}).")


if __name__ == "__main__":
    main()
