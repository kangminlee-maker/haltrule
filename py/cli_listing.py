#!/usr/bin/env python3
"""What the shell program says when it is asked for nothing: the entry points
and their arguments, rendered from spec/contract.json.

The spec says a program asked for nothing says what it takes, and that what the
listing looks like is for a person and not part of the contract - so this file
is where the looking lives, and the mutation tools leave it alone, as they leave
a message's wording alone. That it names every entry point is the contract's,
and scripts/conform.py checks it.
"""

from __future__ import annotations

import json
from pathlib import Path

SAFE_INTEGER = 2**53 - 1


def describe(t: dict) -> str:
    """One type of the contract, in a line: what a value must be."""
    kind = t["type"]
    if kind == "integer":
        low, high = t["min"], t["max"]
        shown = (
            "integer"
            if (low, high) == (-SAFE_INTEGER, SAFE_INTEGER)
            else f"integer {low}..{high}"
        )
    elif kind == "enum":
        shown = " | ".join(t["of"])
    elif kind == "list":
        shown = f"list of {describe(t['of'])}"
    elif kind == "map":
        fields = ", ".join(_field(name, f) for name, f in t["fields"].items())
        shown = "{" + fields + (", ..." if t.get("closed") is False else "") + "}"
    elif kind == "open_map":
        parts = [f"<any key>: {describe(t['of'])}"]
        parts += [_field(name, f) for name, f in t.get("fields", {}).items()]
        shown = "{" + ", ".join(parts) + "}"
        if t.get("forbidden"):
            shown += " without " + ", ".join(t["forbidden"])
    elif kind == "one_of":
        shown = " | ".join(
            f"{t['by']}={name} {describe(case)}" for name, case in t["cases"].items()
        )
    else:
        shown = kind
    return shown + (" or null" if t.get("null_is_a_value") else "")


def _field(name: str, t: dict) -> str:
    return f"{name}{'!' if t.get('required') else ''}: {describe(t)}"


def listing(contract: Path, known: set[str], only: str = "") -> str:
    """The entry points and their arguments, read from the contract file."""
    doc = json.loads(contract.read_text(encoding="ascii"))
    lines = [
        "python3 py/cli.py <entry point> [<arguments file> | -]",
        "",
        "the arguments are one JSON object under these names (! required); the exit",
        "code is the worst verdict in the answer (0 ok, 1 warning, 2 halt), 3 for",
        "arguments outside the contract, 4 when no call was made. rules between",
        "arguments (at_most, requires) are in spec/contract.json.",
        "",
    ]
    for name, entry in doc["entry_points"].items():
        if only != "" and name != only:
            continue
        if entry.get("takes_a_function"):
            lines.append(f"{name}  (takes a function: not from a shell)")
            continue
        if name not in known:
            lines.append(f"{name}  (the contract has it and this program does not)")
            continue
        lines.append(name)
        if "call" in entry:
            lines.append(f"  the object itself: {describe(entry['call'])}")
        for argument, t in entry.get("arguments", {}).items():
            lines.append(f"  {_field(argument, t)}")
    return "\n".join(lines) + "\n"
