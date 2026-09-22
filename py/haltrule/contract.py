# How a part reads an argument and refuses one outside its contract
# (spec/contract.json). A refusal raises: the call fails and changes nothing.
# Each kind of argument is read here once, for every part: a rule written in
# one place has one place to be wrong. Map arguments need no reader: an entry
# point takes keyword arguments, and the call itself refuses a non-map and a
# key the signature does not name.

from __future__ import annotations

from typing import Any


def integer(value: Any, what: str, low: int, high: int) -> int:
    """An integer however the caller holds it - an int or an integral float -
    within low..high."""
    # bool before int: bool is an int subclass, and True is not a count.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be an integer, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():  # false for NaN and the infinities too
            raise TypeError(f"{what} must be an integer, got {value!r}")
        value = int(value)
    if not low <= value <= high:
        raise ValueError(f"{what} must be within [{low}, {high}], got {value}")
    return value


def text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, got {value!r}")
    return value


def flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{what} must be a boolean, got {value!r}")
    return value
