#!/usr/bin/env python3
"""haltrule from a shell: one entry point of the contract per call.

    python3 py/cli.py <entry point> [<arguments file> | -]

The entry points are those of spec/contract.json - checkpoint.evaluate,
breaker.classify, ... - and the arguments are one JSON object under the names
that file gives them, read from the file named or from stdin (`-`); an
argument left out is absent, as null is. The answer is printed as one line of
JSON, `message` and all; a budget's ledger is in decimal strings, so that
2^63 - 1 survives every JSON reader. The exit code is the worst verdict in the
answer - 0 ok, 1 warning, 2 halt - or 3 when the arguments are refused, being
outside the contract, or 4 when no call was made: an entry point that is not
one, JSON that could not be read, or breaker.run, which takes a function and
cannot be called from a shell. With no entry point, or with no arguments where
stdin is a terminal, the entry points and their arguments are listed instead,
read from the contract file, and the exit code is 4.

A program of the Python port, not a fifth port: it holds no rule of its own
beyond the mapping above, and the fixtures hold it to the same answers as the
adapter (scripts/conform.py cli). Standard library only.
"""

from __future__ import annotations

import sys

# Python puts this script's directory first on the path, where a file named like
# a standard-library module would be imported in its place, by the package too.
# It goes last: haltrule is still found, and nothing here can stand in for the
# standard library.
sys.path.append(sys.path.pop(0))

import dataclasses  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from haltrule.breaker import (  # noqa: E402
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

CONTRACT = Path(__file__).resolve().parent.parent / "spec" / "contract.json"
LEVELS = {"ok": 0, "warning": 1, "halt": 2}
REFUSED = 3
NO_CALL = 4
SAFE_INTEGER = 2**53 - 1

# ------------------------------------------------------------ the entry points


def _plain(value):
    """An answer as JSON holds it: a dataclass is its fields, all the way down."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _calls(given) -> list:
    """A list of calls - charges, reports. Absent is none; anything that is
    not a list is outside the contract."""
    if given is None:
        return []
    if not isinstance(given, list):
        raise TypeError("a list of calls must be a list")
    return given


def _one(call):
    """One call of a batch: its answer, or the refusal that leaves the batch
    as it was, as a refused charge leaves the ledger."""
    try:
        return call()
    except (TypeError, ValueError):
        return {"refused": True}


def validate(args: dict):
    return validate_slot(args.get("value"), **args.get("spec"))


def charge(args: dict):
    budget = Budget(**args.get("budget"))
    verdicts = [
        _one(lambda charge=charge: budget.charge(**charge))
        for charge in _calls(args.get("charges"))
    ]
    return {
        "verdicts": verdicts,
        "used": {
            "turns": str(budget.turns_used),
            "ms": str(budget.ms_used),
            "tokens": str(budget.tokens_used),
        },
    }


def classify(args: dict):
    return classify_systemic_dispatch_failure(message=args.get("message"))


def backoff(args: dict):
    # The object itself is the argument, as the contract says of this call.
    return dispatch_backoff_delay_ms(**args)


def _report(batch: DispatchBreakerState, event):
    if not isinstance(event, dict):
        raise TypeError("a report is a map with a kind")
    kind = event.get("kind")
    if kind == "success":
        batch.record_item_success(event.get("item_id"))
        return None
    if kind == "skipped":
        batch.record_item_skipped(event.get("item_id"))
        return None
    if kind == "failure":
        entry = {key: value for key, value in event.items() if key != "kind"}
        return batch.record_item_failure(DispatchDeadLetterEntry(**entry))
    raise TypeError(f"a report's kind is success, skipped or failure, not {kind!r}")


def state(args: dict):
    batch = DispatchBreakerState(DispatchBreakerPolicy(**args.get("policy")))
    returns = [
        _one(lambda event=event: _report(batch, event))
        for event in _calls(args.get("events"))
    ]
    return {
        "returns": returns,
        "completed": list(batch.completed_item_ids()),
        "dead_letter": list(batch.dead_letter_entries()),
        "tripped": batch.tripped(),
    }


def canonical(args: dict):
    value = args.get("input")
    result = canonicalize(value)
    if "verdict" in result:
        return result
    return {
        "canonical": result["canonical"],
        "digest": checkpoint_digest(value)["digest"],
    }


def evaluate(args: dict):
    return evaluate_checkpoint_artifact(**args.get("args"))


ENTRY_POINTS = {
    "slot.validate": validate,
    "budget.charge": charge,
    "breaker.classify": classify,
    "breaker.backoff": backoff,
    "breaker.state": state,
    "checkpoint.canonicalize": canonical,
    "checkpoint.evaluate": evaluate,
}

# ------------------------------------------------------------- answer and exit


def worst_verdict(answer) -> int:
    """The worst verdict anywhere in the answer; 0 where it holds none."""
    if isinstance(answer, dict):
        own = LEVELS.get(answer.get("verdict"), 0)
        return max([own] + [worst_verdict(value) for value in answer.values()])
    if isinstance(answer, list):
        return max([0] + [worst_verdict(item) for item in answer])
    return 0


def line(answer) -> str:
    return json.dumps(
        answer,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


# ---------------------------------------------------------------- the listing


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


def listing(only: str | None = None) -> str:
    """The entry points and their arguments, read from the contract file."""
    doc = json.loads(CONTRACT.read_text(encoding="ascii"))
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
        if only is not None and name != only:
            continue
        if name not in ENTRY_POINTS:
            lines.append(f"{name}  (takes a function: not from a shell)")
            continue
        lines.append(name)
        if "call" in entry:
            lines.append(f"  the object itself: {describe(entry['call'])}")
        for argument, t in entry.get("arguments", {}).items():
            lines.append(f"  {_field(argument, t)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or len(argv) > 2:
        sys.stdout.write(listing())
        return NO_CALL
    name, source = argv[0], argv[1] if len(argv) > 1 else None
    entry = ENTRY_POINTS.get(name)
    if entry is None:
        sys.stderr.write(f"no entry point is named {name!r}\n")
        sys.stdout.write(listing())
        return NO_CALL
    if source is None and sys.stdin.isatty():
        sys.stdout.write(listing(only=name))
        return NO_CALL
    try:
        raw = (
            sys.stdin.buffer.read()
            if source in (None, "-")
            else Path(source).read_bytes()
        )
        args = json.loads(raw)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"the arguments could not be read: {error}\n")
        return NO_CALL
    if not isinstance(args, dict):
        sys.stderr.write("the arguments are one JSON object\n")
        return NO_CALL
    try:
        answer = _plain(entry(args))
    except (TypeError, ValueError) as error:
        sys.stderr.write(f"refused: {error}\n")
        return REFUSED
    sys.stdout.write(line(answer) + "\n")
    return worst_verdict(answer)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
