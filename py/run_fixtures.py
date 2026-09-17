#!/usr/bin/env python3
"""Conformance runner for fixtures/breaker/v0.json against py/haltrule/breaker.py.

Loads the fixture file, feeds each case to the pure policy functions, and
diffs the actual result against the fixture's expected value. Exits non-zero
and prints every mismatch (fixture id + field + expected/actual) on any
failure - this is the instrument the "corrupt one expectation and confirm it
fails" check in fixtures/README.md exercises.

Only the standard library (json, sys, pathlib, dataclasses, argparse) is
used; no third-party dependencies. Mirrors ts/run-fixtures.ts field-for-field
so the two runners' pass/fail verdicts are directly comparable.
"""

from __future__ import annotations

import dataclasses
import json
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

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "breaker" / "v0.json"
)

_failure_count = 0
_case_count = 0


def _to_plain(value):
    """Recursively convert dataclass instances (and containers of them) to
    plain dicts/lists so they compare equal to the JSON-decoded expected
    values with plain `==` (Python's built-in equality already does the
    deep, order-independent structural comparison the TS runner's deepEqual
    implements by hand).
    """
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _fail(case_id: str, field: str, expected, actual) -> None:
    global _failure_count
    _failure_count += 1
    print(
        f"FAIL [{case_id}] field={field}\n"
        f"  expected: {json.dumps(expected)}\n"
        f"  actual:   {json.dumps(actual)}",
        file=sys.stderr,
    )


def run_classify(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        # `message` may be a non-string (e.g. a JSON number) to exercise the
        # classifier's isinstance guard - matches how an untyped caller could
        # hand it anything.
        actual = classify_systemic_dispatch_failure(tc["message"])
        if actual != tc["expect"]:
            _fail(tc["id"], "classify.expect", tc["expect"], actual)


def run_backoff(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
        actual = dispatch_backoff_delay_ms(
            attempt=tc["attempt"],
            initial_ms=tc["initial_ms"],
            cap_ms=tc["cap_ms"],
        )
        if actual != tc["expect_ms"]:
            _fail(tc["id"], "backoff.expect_ms", tc["expect_ms"], actual)


def run_state(cases: list[dict]) -> None:
    global _case_count
    for tc in cases:
        _case_count += 1
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

        actual_returns = _to_plain(returns)
        if actual_returns != tc["expect"]["returns"]:
            _fail(tc["id"], "state.returns", tc["expect"]["returns"], actual_returns)

        completed = list(state.completed_item_ids())
        if completed != tc["expect"]["completed"]:
            _fail(tc["id"], "state.completed", tc["expect"]["completed"], completed)

        dead_letter = _to_plain(list(state.dead_letter_entries()))
        if dead_letter != tc["expect"]["dead_letter"]:
            _fail(
                tc["id"], "state.dead_letter", tc["expect"]["dead_letter"], dead_letter
            )

        tripped = _to_plain(state.tripped())
        if tripped != tc["expect"]["tripped"]:
            _fail(tc["id"], "state.tripped", tc["expect"]["tripped"], tripped)


def main() -> None:
    fixture_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE_PATH
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))

    run_classify(fixtures["classify"])
    run_backoff(fixtures["backoff"])
    run_state(fixtures["state"])

    if _failure_count > 0:
        print(
            f"\n{_failure_count} mismatch(es) across {_case_count} cases "
            f"(fixture_version={fixtures['fixture_version']}).",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"OK: {_case_count} cases passed (fixture_version={fixtures['fixture_version']})."
    )


if __name__ == "__main__":
    main()
