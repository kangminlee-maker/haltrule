# Ported from ts/slot.ts. fixtures/slot/v0.json is the contract both language
# ports must satisfy - do not change behavior here without checking it still
# passes that fixture.
#
# Does a value a person or a model filled in satisfy its contract? Lifted from
# three pipelines' checks on submitted values: a spreadsheet adjudication that
# accepts only a pick from a closed set, a workflow worker that rejects a
# model-filled slot outside its contract before anything is written, and a
# hold flow that takes a person's own sentence and checks only its display
# contract. Three kinds cover them: "choice", a value that must be one of the
# candidates exactly, "text", a value of the person's own within length
# bounds, and "score", a number held against a bar. A reference to something
# that exists is a "choice" whose candidates are the known identifiers.
#
# A missing value is a warning: the judgment has not been made yet. A present
# value that fails its contract is a halt: it would be written to a ledger as
# if it were valid. Missing is None, or a string that is empty or holds only
# ASCII whitespace. Comparison is exact - no trimming, no case folding, no
# Unicode normalization; a caller that wants those applies them first.
# Lengths count Unicode scalar values, so every language counts the same, and
# a string that is not made of them (it holds a lone surrogate) fails its
# contract. A shape beyond length - a UUID, a URL - is the caller's to check,
# as a float's rendering is the caller's in a digest. A score is only
# compared: rounding it, summing it or weighting it is the caller's, done
# first, where it can be reviewed.
#
# The spec is the keyword arguments, so the call itself refuses a spec that is
# not a map or holds a field the contract does not name.

from __future__ import annotations

from typing import Any, Optional

from haltrule.contract import integer
from haltrule.verdict import verdict

_ASCII_WHITESPACE = " \t\n\r\f\v"


def _is_blank(text: str) -> bool:
    return not text.strip(_ASCII_WHITESPACE)


def _has_lone_surrogate(text: str) -> bool:
    # A Python str never pairs surrogates, so any surrogate code point is lone.
    return any(0xD800 <= ord(character) <= 0xDFFF for character in text)


# The largest integer every language holds exactly: JavaScript's limit, as for
# digest inputs.
_BOUND_MAX = 2**53 - 1


def _bound(value: Any, what: str) -> Optional[int]:
    return None if value is None else integer(value, what, 0, _BOUND_MAX)


def _is_number(value: Any) -> bool:
    # A number as this spec has it: a finite double, and an integral one an
    # integer within +/-(2^53 - 1), as every other number in the spec is. Every
    # double past 2^53 - 1 is integral, so that is one range test, which NaN
    # and the infinities fail too; it compares an int of any size exactly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return abs(value) <= _BOUND_MAX


def _bar(value: Any, what: str) -> Optional[float]:
    if value is None:
        return None
    if not _is_number(value):
        raise TypeError(
            f"{what} must be a finite number, an integral one within 2^53 - 1, got {value!r}"
        )
    return value


def validate_slot(
    value: Any,
    *,
    name: Any = None,
    kind: Any = None,
    candidates: Any = None,
    min_length: Any = None,
    max_length: Any = None,
    # The spec's own field names; the body calls neither builtin they shadow.
    min: Any = None,
    max: Any = None,
) -> dict[str, Any]:
    """The spec is the keyword arguments - `validate_slot(value, **spec)` - so a
    spec that is not a map, or holds a field beyond these seven, is refused by
    the call itself: `name`, `kind` ("choice", "text" or "score"), and for a
    choice `candidates` (strings, compared exactly), for a text `min_length`
    and `max_length` (bounds in Unicode scalar values, each in 0..2^53 - 1;
    None for none), for a score `min` and `max` (numbers, both included; None
    for none)."""
    if not isinstance(name, str):
        raise TypeError(f"slot spec without a string name: {name!r}")
    if kind not in ("choice", "text", "score"):
        raise TypeError(f"slot {name}: unknown kind {kind!r}")
    # A field that is given is held to its type whatever the kind, as the bounds are below.
    if candidates is not None and not (
        isinstance(candidates, (list, tuple))
        and all(isinstance(candidate, str) for candidate in candidates)
    ):
        raise TypeError(f"slot {name}: candidates must be a list of strings")
    if kind == "choice" and candidates is None:
        raise TypeError(f"slot {name}: a choice needs candidates")
    minimum = _bound(min_length, f"slot {name}: min_length")
    maximum = _bound(max_length, f"slot {name}: max_length")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            f"slot {name}: min_length {minimum} exceeds max_length {maximum}"
        )
    floor = _bar(min, f"slot {name}: min")
    ceiling = _bar(max, f"slot {name}: max")
    if floor is not None and ceiling is not None and floor > ceiling:
        raise ValueError(f"slot {name}: min {floor!r} exceeds max {ceiling!r}")

    if value is None:
        return verdict("warning", "slot_missing", f"slot {name}: no value")
    if kind == "score" and not isinstance(value, str):
        if not _is_number(value):
            return verdict(
                "halt",
                "slot_invalid",
                f"slot {name}: {value!r} is not a number this spec holds",
            )
        if floor is not None and value < floor:
            return verdict(
                "halt", "slot_invalid", f"slot {name}: {value!r} is below {floor!r}"
            )
        if ceiling is not None and value > ceiling:
            return verdict(
                "halt", "slot_invalid", f"slot {name}: {value!r} is above {ceiling!r}"
            )
        return verdict("ok", "slot_accepted", f"slot {name}: {value!r}")
    if not isinstance(value, str):
        return verdict(
            "halt",
            "slot_invalid",
            f"slot {name}: a {type(value).__name__} is not a string",
        )
    if _has_lone_surrogate(value):
        return verdict(
            "halt",
            "slot_invalid",
            f"slot {name}: not a string of Unicode scalar values (a lone surrogate)",
        )
    if _is_blank(value):
        return verdict("warning", "slot_missing", f"slot {name}: blank")
    if kind == "score":
        return verdict(
            "halt",
            "slot_invalid",
            f"slot {name}: a string is not a number, however it reads",
        )

    if kind == "choice":
        if value in candidates:
            return verdict(
                "ok", "slot_accepted", f"slot {name}: {value!r} is a candidate"
            )
        return verdict(
            "halt",
            "slot_invalid",
            f"slot {name}: {value!r} is not one of {len(candidates)} candidates",
        )
    length = len(value)
    if minimum is not None and length < minimum:
        return verdict(
            "halt",
            "slot_invalid",
            f"slot {name}: {length} characters, fewer than {minimum}",
        )
    if maximum is not None and length > maximum:
        return verdict(
            "halt",
            "slot_invalid",
            f"slot {name}: {length} characters, more than {maximum}",
        )
    return verdict("ok", "slot_accepted", f"slot {name}: {length} characters")
