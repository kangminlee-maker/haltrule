#!/usr/bin/env python3
"""haltrule from a shell: one entry point of the contract per call.

    python3 py/cli.py <entry point> [<arguments file> | -]

What a shell program takes and answers is spec/README.md, "From a shell": an
entry point of spec/contract.json by its own name, the arguments as one JSON
object under the contract's names, the answer as one line of JSON with each
verdict's message kept, and the worst verdict in that answer as the exit code -
0 ok, 1 warning, 2 halt; 3 for arguments the part refuses; 4 for a call that
was never made. This program is the Python port's, and holds no rule of its own
beyond reading the contract file and calling the port: the fixtures hold it to
the same answers the adapter gives (scripts/conform.py cli).

Two things it decides for itself, both left to it there: it reads the arguments
from stdin when none is named and stdin is not a terminal, and it prints a
budget's ledger in decimal strings, so that 2^63 - 1 survives every JSON reader
- which is what the fixtures expect of every port.

Standard library only.
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

from cli_listing import listing as _listing  # noqa: E402

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

# ------------------------------------------------------------ the entry points


def _fields(value):
    """A dataclass the port answers with - an entry, a trip - as its fields."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
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
        "returns": [_fields(answer) for answer in returns],
        "completed": list(batch.completed_item_ids()),
        "dead_letter": [_fields(entry) for entry in batch.dead_letter_entries()],
        "tripped": _fields(batch.tripped()),
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
    """The worst verdict anywhere in the answer; 0 where it holds none. A walk
    with its own stack: an answer echoes the caller's own values, however deep
    they are nested, and this program keeps no limit of its own on that."""
    worst, pending = 0, [answer]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            worst = max(worst, LEVELS.get(node.get("verdict"), 0))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return worst


def line(answer) -> str:
    return json.dumps(
        answer,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


# --------------------------------------------------------------------- main


def listing(only: str = "") -> str:
    """What this program takes, from the contract file: py/cli_listing.py. No
    name is every name, as it is for the Go program beside this one."""
    return _listing(CONTRACT, set(ENTRY_POINTS), only)


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
    except RecursionError:
        sys.stderr.write("the arguments are nested deeper than this program reads\n")
        return NO_CALL
    if not isinstance(args, dict):
        sys.stderr.write("the arguments are one JSON object\n")
        return NO_CALL
    try:
        answer = entry(args)
    except (TypeError, ValueError) as error:
        sys.stderr.write(f"refused: {error}\n")
        return REFUSED
    # The answer echoes the caller's own values; what JSON cannot carry back -
    # a NaN it read, a nesting past the writer - is no answer, not a traceback.
    try:
        text = line(answer)
    except (ValueError, RecursionError) as error:
        sys.stderr.write(f"the answer could not be written as JSON: {error}\n")
        return NO_CALL
    sys.stdout.write(text + "\n")
    return worst_verdict(answer)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
