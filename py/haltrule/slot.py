# Ported from ts/slot.ts. fixtures/slot/v0.json is the contract both language
# ports must satisfy - do not change behavior here without checking it still
# passes that fixture.
#
# Does a value a person or a model filled in satisfy its contract? Lifted from
# three pipelines' checks on submitted values: a spreadsheet adjudication that
# accepts only a pick from a closed set, a workflow worker that rejects a
# model-filled slot outside its contract before anything is written, and a
# hold flow that takes a person's own sentence and checks only its display
# contract. Two kinds cover them: "choice", a value that must be one of the
# candidates exactly, and "text", a value of the person's own within length
# bounds. A reference to something that exists is a "choice" whose candidates
# are the known identifiers.
#
# A missing value is a warning: the judgment has not been made yet. A present
# value that fails its contract is a halt: it would be written to a ledger as
# if it were valid. Missing is None, or a string that is empty or holds only
# ASCII whitespace. Comparison is exact - no trimming, no case folding, no
# Unicode normalization; a caller that wants those applies them first.
# Lengths count Unicode scalar values, so every language counts the same, and
# a string that is not made of them (it holds a lone surrogate) fails its
# contract. A shape beyond length - a UUID, a URL - is the caller's to check,
# as a float's rendering is the caller's in a digest.

from __future__ import annotations

from typing import Any, Mapping, Optional

from haltrule.verdict import verdict

_ASCII_WHITESPACE = " \t\n\r\f\v"


def _is_blank(text: str) -> bool:
    return text.strip(_ASCII_WHITESPACE) == ""


def _has_lone_surrogate(text: str) -> bool:
    # A Python str never pairs surrogates, so any surrogate code point is lone.
    return any(0xD800 <= ord(character) <= 0xDFFF for character in text)


# The largest integer every language holds exactly: JavaScript's limit, as for
# digest inputs.
_BOUND_MAX = 2**53 - 1


def _bound(value: Any, what: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be a non-negative integer, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise TypeError(f"{what} must be a non-negative integer, got {value!r}")
        value = int(value)
    if not 0 <= value <= _BOUND_MAX:
        raise TypeError(
            f"{what} must be a non-negative integer up to 2^53 - 1, got {value!r}"
        )
    return value


def validate_slot(spec: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """`spec` is a mapping with `name`, `kind` ("choice" or "text"), and for a
    choice `candidates` (strings, compared exactly), for a text `min_length`
    and `max_length` (bounds in Unicode scalar values, each in 0..2^53 - 1; None
    for none)."""
    name = spec.get("name")
    if not isinstance(name, str):
        raise TypeError(f"slot spec without a string name: {name!r}")
    kind = spec.get("kind")
    if kind not in ("choice", "text"):
        raise TypeError(f"slot {name}: unknown kind {kind!r}")
    candidates = spec.get("candidates")
    if kind == "choice" and not isinstance(candidates, (list, tuple)):
        raise TypeError(f"slot {name}: a choice needs candidates")
    minimum = _bound(spec.get("min_length"), f"slot {name}: min_length")
    maximum = _bound(spec.get("max_length"), f"slot {name}: max_length")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            f"slot {name}: min_length {minimum} exceeds max_length {maximum}"
        )

    if value is None:
        return verdict("warning", "slot_missing", f"slot {name}: no value")
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
