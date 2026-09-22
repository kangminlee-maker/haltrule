"""The Python adapter: reads the fixture files, feeds each case to the package
under py/haltrule, and prints one result line per case. It judges nothing -
scripts/conform.py compares the lines with what the fixtures expect, for every
language alike - and it is the only place that knows how a fixture's fields
map to this port's functions.

With no argument it reads every .json file under fixtures/, by path; given
paths, those. A line is {"actual": <result>, "id", "section"}, or "raised"
in place of "actual" when the case raised something that is not a refusal.
The line format and the $number / $bigint / $unsupported tags are defined in
fixtures/README.md; the driver has already checked every literal's spelling.

Standard library only.
"""

from __future__ import annotations

import sys

# Python puts this script's directory first on the path, where a file named like
# a standard-library module (py/hashlib.py) would be imported in its place, by
# the package under test too. It goes last: haltrule is still found, and
# nothing here can stand in for the standard library.
sys.path.append(sys.path.pop(0))

import dataclasses  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from haltrule.breaker import (  # noqa: E402
    Failure,
    Skipped,
    Success,
    run_batch,
    DispatchBreakerPolicy,
    DispatchBreakerState,
    DispatchDeadLetterEntry,
    classify_systemic_dispatch_failure,
    dispatch_backoff_delay_ms,
)
from haltrule.budget import Budget  # noqa: E402
from haltrule.checkpoint import (  # noqa: E402
    canonicalize,
    checkpoint_digest,
    evaluate_checkpoint_artifact,
)
from haltrule.slot import validate_slot  # noqa: E402

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures"

# ------------------------------------------------------------------ the line

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


# ------------------------------------------------------------ fixture inputs

_INTEGER_LITERAL = re.compile(r"-?(0|[1-9][0-9]*)")
_UNSUPPORTED = {
    "undefined": object,
    "instance": object,
    "non_string_key": lambda: {1: 1},
    # Python has no holes; a list holding an unsupported element is the value a hole reads as.
    "sparse_array": lambda: [object()],
}


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


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        ((key, inner),) = value.items()
        if key == "$number":
            return (
                _int_from_literal(inner)
                if _INTEGER_LITERAL.fullmatch(inner)
                else float(inner)
            )
        if key == "$bigint":
            return _int_from_literal(inner)
        if key == "$unsupported":
            return _UNSUPPORTED[inner]()
    return {key: _decode(inner) for key, inner in value.items()}


# ------------------------------------------------- one function per section

_REFUSED = object()


def _or_refused(call):
    """The part's own refusal of an argument outside its contract, and only
    that. Inputs are decoded before any section function runs, and a verdict's
    shape is checked outside this, so neither can pass as a refusal."""
    try:
        return call()
    except (TypeError, ValueError):
        return _REFUSED


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


def _normative_open(result: dict) -> dict:
    """The same for a verdict carrying the fields its reason names beside the
    five: those may be there, the five must be, and `message` still goes."""
    missing = [field for field in _VERDICT_FIELDS if field not in result]
    if missing:
        raise AssertionError(f"a verdict has no {missing[0]}")
    if not isinstance(result["message"], str):
        raise AssertionError(
            f"verdict message is {type(result['message']).__name__}, not a string"
        )
    return {key: value for key, value in result.items() if key != "message"}


def _to_plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _classify(tc: dict):
    result = _or_refused(
        lambda: classify_systemic_dispatch_failure(message=tc.get("message"))
    )
    return {"refused": True} if result is _REFUSED else result


def _backoff(tc: dict):
    # The case's own map is the argument: a field the contract does not name
    # reaches the part, as it would from a caller.
    result = _or_refused(lambda: dispatch_backoff_delay_ms(**tc))
    return {"refused": True} if result is _REFUSED else result


def _state(tc: dict):
    state = _or_refused(
        lambda: DispatchBreakerState(DispatchBreakerPolicy(**tc["policy"]))
    )
    if state is _REFUSED:
        return {"refused": True}
    returns = []
    for event in tc["events"]:
        # A refused report leaves the batch as it was, as a refused charge
        # leaves the ledger: the answer is the refusal and the next report
        # goes on.
        # An id that is not given reaches the part as absent, and the part
        # refuses it: the adapter holds no rule of its own.
        if event["kind"] == "success":
            result = _or_refused(
                lambda event=event: state.record_item_success(event.get("item_id"))
            )
        elif event["kind"] == "skipped":
            result = _or_refused(
                lambda event=event: state.record_item_skipped(event.get("item_id"))
            )
        else:
            entry = {key: value for key, value in event.items() if key != "kind"}
            result = _or_refused(
                lambda entry=entry: state.record_item_failure(
                    DispatchDeadLetterEntry(**entry)
                )
            )
        if result is _REFUSED:
            returns.append({"refused": True})
        else:
            returns.append(
                None if result is None else _normative_open(_to_plain(result))
            )
    return {
        "returns": _to_plain(returns),
        "completed": list(state.completed_item_ids()),
        "dead_letter": _to_plain(list(state.dead_letter_entries())),
        "tripped": (
            None
            if state.tripped() is None
            else _normative_open(_to_plain(state.tripped()))
        ),
    }


class _Script:
    """`call` as a case scripts it: answers[i] is what item i's calls answer,
    in order, and a case without answers is every call succeeding. A call the
    script has no answer for, or an answer no call asks for, is the case's
    own mistake and is reported under its id."""

    def __init__(self, items, answers) -> None:
        self._items = items
        self._answers = (
            None if answers is None else [list(script) for script in answers]
        )
        self._at = 0

    def _script(self, at: int) -> list:
        return self._answers[at] if at < len(self._answers) else []

    def __call__(self, item_id: str):
        if self._answers is None:
            return Success()
        if not self._script(self._at):
            self._at += 1
        if (
            self._at >= len(self._items)
            or self._items[self._at] != item_id
            or not self._script(self._at)
        ):
            raise AssertionError(
                f"the loop called {item_id!r} where the script has no answer"
            )
        outcome = self._script(self._at).pop(0)
        if outcome["kind"] == "success":
            return Success()
        if outcome["kind"] == "skipped":
            return Skipped()
        return Failure(outcome["failure_message"], outcome["failure_class"])

    def unasked(self) -> bool:
        return self._answers is not None and any(self._answers)


def _run(tc: dict):
    slept: list = []
    script = _Script(
        tc["items"] if isinstance(tc["items"], list) else [], tc.get("answers")
    )
    result = _or_refused(
        lambda: run_batch(
            DispatchBreakerPolicy(**tc["policy"]),
            tc["items"],
            script,
            slept.append,
        )
    )
    if result is _REFUSED:
        return {"refused": True}
    if script.unasked():
        raise AssertionError("the script holds answers no call asked for")
    return {
        "completed": list(result.completed),
        "dead_letter": _to_plain(result.dead_letter),
        "tripped": (
            None
            if result.tripped is None
            else _normative_open(_to_plain(result.tripped))
        ),
        "incomplete": list(result.incomplete),
        "slept": slept,
    }


def _canonicalize(tc: dict) -> dict:
    canonical = canonicalize(tc["input"])
    digest = checkpoint_digest(tc["input"])
    # The two entry points must agree about the same value; if they do not,
    # that is a bug in the implementation, not a result to compare.
    if "verdict" in canonical or "verdict" in digest:
        # They must agree in the whole verdict, not only the reason: only one
        # of the two is printed, so a field this comparison leaves out is a
        # field no case can see.
        shown = _normative(canonical) if "verdict" in canonical else None
        other = _normative(digest) if "verdict" in digest else None
        if shown != other:
            raise AssertionError(
                f"canonicalize halts with {shown} and the digest with {other}"
            )
        return shown
    return {"canonical": canonical["canonical"], "digest": digest["digest"]}


def _checkpoint(tc: dict):
    result = _or_refused(lambda: evaluate_checkpoint_artifact(**tc["args"]))
    if result is _REFUSED:
        return {"refused": True}
    return [_normative_open(issue) for issue in result]


def _charge(tc: dict) -> dict:
    budget = _or_refused(lambda: Budget(**tc["budget"]))
    if budget is _REFUSED:
        return {"refused": True}
    verdicts = []
    for charge in tc["charges"]:
        result = _or_refused(lambda charge=charge: budget.charge(**charge))
        verdicts.append({"refused": True} if result is _REFUSED else _normative(result))
    # The ledger in decimal, so 2^63 - 1 survives JSON in every language.
    return {
        "verdicts": verdicts,
        "used": {
            "turns": str(budget.turns_used),
            "ms": str(budget.ms_used),
            "tokens": str(budget.tokens_used),
        },
    }


def _validate(tc: dict) -> dict:
    result = _or_refused(lambda: validate_slot(tc["value"], **tc["spec"]))
    return {"refused": True} if result is _REFUSED else _normative(result)


def _result_line(tc: dict) -> dict:
    """The line format's own vectors: what this adapter writes for a value."""
    try:
        return {"line": _canonical_json(tc["value"])}
    except ValueError:
        return {"refused": True}


SECTIONS = {
    "classify": _classify,
    "backoff": _backoff,
    "state": _state,
    "run": _run,
    "canonicalize": _canonicalize,
    "checkpoint": _checkpoint,
    "charge": _charge,
    "validate": _validate,
    "result_line": _result_line,
}


def main() -> None:
    # A line carries non-ASCII text; write it as UTF-8 whatever the locale says.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    paths = [Path(arg) for arg in sys.argv[1:]] or sorted(
        FIXTURE_ROOT.rglob("*.json"),
        key=lambda p: p.relative_to(FIXTURE_ROOT).as_posix(),
    )
    for path in paths:
        fixtures = json.loads(path.read_text(encoding="ascii"))
        for section, cases in fixtures.items():
            if section == "fixture_version":
                continue
            # An unknown section stops the run; the driver counts the lines.
            compute = SECTIONS[section]
            for case in cases:
                inputs = _decode(
                    {
                        key: value
                        for key, value in case.items()
                        if key not in ("id", "expect")
                    }
                )
                line = {"id": case["id"], "section": section}
                try:
                    line["actual"] = compute(inputs)
                    text = _canonical_json(line)
                except Exception as error:  # noqa: BLE001 - reported under the case's id, never hidden
                    line.pop("actual", None)
                    text = _canonical_json(
                        {**line, "raised": f"{type(error).__name__}: {error}"}
                    )
                print(text)


if __name__ == "__main__":
    main()
