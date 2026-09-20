"""Verdict - the one shape every part returns.

A verdict is a value, never an exception: a part decides, the caller acts.
It is plain JSON by construction, so it can be written into an artifact
field, a spreadsheet cell, or a database column as it is. `message` is for a
person and is not part of conformance; the other four fields are.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

# The spec version stamped on every verdict: draft 0 until the spec freezes.
SPEC = "haltrule/0"

VerdictLevel = Literal["ok", "warning", "halt"]


def verdict(
    level: VerdictLevel, reason: str, message: str, resume: Optional[str] = None
) -> dict[str, Any]:
    return {
        "spec": SPEC,
        "verdict": level,
        "reason": reason,
        "message": message,
        "resume": resume,
    }


def is_verdict_level(value: Any) -> bool:
    """Whether a level is one of the three. The library never asks it of
    itself; a caller's own verdict, laid over a checkpoint issue, is the one
    place a level arrives from outside."""
    return value in ("ok", "warning", "halt")
