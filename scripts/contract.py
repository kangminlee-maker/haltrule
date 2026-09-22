"""The argument contract, spec/contract.json, as cases nobody wrote.

A fixture is an example: one input, one answer, both written by hand. A contract is a rule over every
input, and no list of examples holds a rule - for any list there is a change that misbehaves only on an
input the list lacks. So the rule is checked from the rule: this module reads the contract and makes,
from a fixed seed, calls inside it and calls with exactly one defect, and scripts/conform.py holds every
port to three properties over them, none of which needs an expectation anyone wrote:

    refusal      a call with one defect is refused - the whole call, or the one charge or report in it
    acceptance   a call inside the contract is not refused, and raises nothing
    identity     a call inside the contract gets the same line from every port

The defects come from the types alone: what is not a string, what is outside an integer's range, a
field the map does not name, a required field missing, a rule broken. A defect is planted in a call that
is otherwise valid and otherwise random, so that the port's other branches are exercised around it. A
value that a part may hand back in its answer - what an artifact records, a caller's own issue field - is
drawn from what a result line can hold, since that is the fixture protocol's limit and not the part's.

Standard library only; imports nothing from any port.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "spec" / "contract.json"
SEED = 2026
# How many calls each defect is planted in. The first is bare - every optional argument absent, a batch of
# one - so that a refusal moved behind the first question a port asks of a value is met by a value that
# answers that question; the rest are random, so that the port's other branches are exercised around it.
PLANTINGS = 3
MAX_SAFE = 2**53 - 1
I64_MAX = 2**63 - 1

# A cell: what the judge asks of one generated case.
#   ("refused", None)            the whole answer is {"refused": true}
#   ("refused", (field, index))  answer[field][index] is {"refused": true}: one charge, one report
#   ("accepted",)                nothing in the answer is a refusal, and the port raised nothing
Cell = tuple


def num(literal: str) -> dict:
    return {"$number": literal}


def big(literal: str) -> dict:
    return {"$bigint": literal}


def load() -> dict:
    return json.loads(CONTRACT.read_text("ascii"))


# ------------------------------------------------------------ valid values

STRINGS = [
    "",
    "a",
    "draft",
    "outline",
    "sha256:a",
    "sha256:b",
    "complete",
    "done",
    "é",
    "0.9",
    " ",
    "\t",
    "429 too many requests",
    "x" * 40,
]
STATUSES = ["complete", "partial", "failed", "blocked"]
FRACTIONS = [
    "0.5",
    "-0",
    "0.8",
    "0.7999999999999999",
    "1e-300",
    "-2.5",
    "123.5",
    "4.9e-324",
]


def spelled(rng: random.Random, n: int) -> dict:
    """One integer in one of the spellings the fixture grammar gives it: a $number where a double
    holds it exactly (as a plain integer, with a fraction part, with an exponent), a $bigint within
    a signed 64-bit integer. The port must read them all as the same integer."""
    forms = []
    if abs(n) <= MAX_SAFE:
        forms += [num(str(n)), num(f"{n}.0"), num(f"{n}e0")]
    if -(2**63) <= n <= I64_MAX:
        forms.append(big(str(n)))
    return rng.choice(forms)


def echoed(rng: random.Random, depth: int = 0):
    """A value a part may hand back: anything a result line holds."""
    leaves = [
        None,
        True,
        False,
        num("0"),
        num("1"),
        num("-3"),
        big("1"),
        num(str(MAX_SAFE)),
        "",
        " ",
        "a",
        "draft",
        "complete",
        "done",
        "sha256:a",
        "sha256:b",
    ]
    if depth > 2 or rng.random() < 0.7:
        return rng.choice(leaves)
    if rng.random() < 0.5:
        return [echoed(rng, depth + 1) for _ in range(rng.choice([0, 1, 2]))]
    return {
        key: echoed(rng, depth + 1)
        for key in rng.sample(["a", "b", "status"], rng.choice([0, 1, 2]))
    }


def anything(rng: random.Random, depth: int = 0):
    """A value of the value model, whatever it is: a slot value, a digest input."""
    leaves = [
        None,
        True,
        False,
        num("0"),
        num("1"),
        num("0.5"),
        num("NaN"),
        num("Infinity"),
        num("-1e20"),
        big("1"),
        big(str(MAX_SAFE + 1)),
        num(str(MAX_SAFE)),
        "",
        " ",
        "a",
        "draft",
        "complete",
        "sha256:a",
        "é",
    ]
    if depth > 3 or rng.random() < 0.6:
        return rng.choice(leaves)
    if rng.random() < 0.5:
        return [anything(rng, depth + 1) for _ in range(rng.choice([0, 1, 2, 3]))]
    return {
        key: anything(rng, depth + 1)
        for key in rng.sample(
            ["a", "b", "status", "é", "2", "10"], rng.choice([0, 1, 2, 3])
        )
    }


# What an artifact records is data, read leniently; drawing it from these pools makes the recorded
# fields meet and miss the expectations, so that every reuse verdict is reached.
RECORDED = {
    "status": STATUSES + ["done", "", None, num("0"), False],
    "contract_revision": STRINGS[:8] + [None, num("1")],
    "stage_config_digest": STRINGS[:8] + [None],
    "dependency_digests": [
        {"a": "sha256:a"},
        {"a": "sha256:b", "b": "sha256:a"},
        {},
        None,
        "junk",
    ],
}


def valid(rng: random.Random, t: dict, name: str = ""):
    kind = t["type"]
    if kind == "string":
        return rng.choice(STRINGS)
    if kind == "boolean":
        return rng.choice([True, False])
    if kind == "enum":
        return rng.choice(t["of"])
    if kind == "integer":
        lo, hi = t["min"], t["max"]
        pool = [
            n
            for n in (lo, lo + 1, hi - 1, hi, 0, 1, 2, 3, 1000, 30000)
            if lo <= n <= hi
        ]
        return spelled(rng, rng.choice(pool))
    if kind == "number":
        if rng.random() < 0.4:
            return spelled(rng, rng.choice([0, 1, 2, 5, -3, MAX_SAFE, -MAX_SAFE]))
        return num(rng.choice(FRACTIONS))
    if kind == "value":
        return anything(rng)
    if kind == "list":
        return [valid(rng, t["of"], name) for _ in range(rng.choice([0, 1, 2, 3]))]
    if kind == "open_map":
        return valid_open_map(rng, t, name)
    if kind == "map":
        return valid_map(rng, t)
    if kind == "one_of":
        case = rng.choice(sorted(t["cases"]))
        return {t["by"]: case, **valid_map(rng, t["cases"][case])}
    raise ValueError(f"unknown type {kind!r} in the contract")


def valid_open_map(rng: random.Random, t: dict, name: str) -> dict:
    out = {}
    for field, f in t.get("fields", {}).items():
        if rng.random() < 0.3:
            out[field] = rng.choice([valid(rng, f), None])
    if name == "artifact":
        for key in rng.sample(sorted(RECORDED), rng.choice([0, 1, 2, 3, 4])):
            out[key] = rng.choice(RECORDED[key])
        if rng.random() < 0.3:
            out["extra"] = echoed(rng)
        return out
    keys = rng.sample(["a", "b", "done", "status", "é"], rng.choice([0, 1, 2, 3]))
    for key in keys:
        of = t["of"]
        out[key] = echoed(rng) if of["type"] == "value" else valid(rng, of)
    return out


def as_number(v) -> float:
    return float(v["$number"]) if "$number" in v else float(int(v["$bigint"]))


def valid_map(rng: random.Random, t: dict) -> dict:
    out = {}
    for field, f in t["fields"].items():
        if f.get("required"):
            out[field] = (
                None
                if f.get("null_is_a_value") and rng.random() < 0.3
                else valid(rng, f, field)
            )
        elif rng.random() < 0.55:
            out[field] = valid(rng, f, field)
        elif rng.random() < 0.3:
            out[field] = None  # null is absent
    for rule in t.get("rules", []):
        if "at_most" in rule:
            a, b = rule["at_most"]
            if (
                out.get(a) is not None
                and out.get(b) is not None
                and as_number(out[a]) > as_number(out[b])
            ):
                out[a], out[b] = out[b], out[a]
        else:
            when, needed = rule["requires"]["when"], rule["requires"]["field"]
            if (
                all(out.get(k) == v for k, v in when.items())
                and out.get(needed) is None
            ):
                out[needed] = valid(rng, t["fields"][needed], needed)
    return out


# ------------------------------------------------------------------ defects


def exact_double(n: int) -> bool:
    return float(n) == n and int(float(n)) == n


def integer_literals(n: int) -> list:
    """Every spelling of an integer just outside a range: a $number where a double holds it exactly,
    a $bigint where a signed 64-bit integer does; past both, the $bigint alone, which a port that
    cannot build it has refused already."""
    forms = []
    if exact_double(n):
        forms.append(num(str(n)))
    if -(2**63) <= n <= I64_MAX or not forms:
        forms.append(big(str(n)))
    return forms


def defects(t: dict) -> list:
    """Every way a value can miss its type, from the type alone."""
    kind = t["type"]
    if kind == "value":
        return []
    if kind == "string":
        return [num("7"), num("0.5"), True, False, [], {}, ["a"]]
    if kind == "boolean":
        return [num("1"), num("0"), "true", "", [], {}]
    if kind == "enum":
        return [
            "",
            "not_in_the_vocabulary",
            t["of"][0].upper(),
            "constructor",
            num("1"),
            True,
            [],
            {},
        ]
    if kind == "integer":
        return (
            [
                "1",
                "",
                True,
                False,
                [],
                {},
                num("1.5"),
                num("NaN"),
                num("Infinity"),
                num("-Infinity"),
            ]
            + integer_literals(t["min"] - 1)
            + integer_literals(t["max"] + 1)
        )
    if kind == "number":
        return (
            [
                "0.8",
                "",
                True,
                False,
                [],
                {},
                num("NaN"),
                num("Infinity"),
                num("-Infinity"),
                num("1e20"),
            ]
            + integer_literals(MAX_SAFE + 1)
            + integer_literals(-MAX_SAFE - 1)
        )
    if kind == "list":
        return ["a", num("1"), True, {}]
    if kind in ("open_map", "map", "one_of"):
        return ["a", num("1"), True, False, []]
    raise ValueError(f"unknown type {kind!r} in the contract")


def broken(rng: random.Random, t: dict, name: str = "", as_element: bool = False):
    """(what was done, the broken value) for every defect one value of type `t` can carry - its own
    type missed, and one level in: an element, a field, a rule. A one_of is told apart by a key the
    fixture protocol owns, so a one_of that is not a map is no case; an element of a list may be
    null, which is one more thing it is not - a list has no absent element, so null there is not
    absence but a value of the wrong kind, except where the element is a value of any kind at all."""
    kind = t["type"]
    if kind != "one_of":
        for bad in defects(t):
            yield f"{name or kind} = {json.dumps(bad)}", bad
        if as_element and kind != "value":
            yield f"{name} null", None
    if kind == "list":
        for what, bad in broken(rng, t["of"], f"{name}[]", as_element=True):
            yield what, [valid(rng, t["of"], name), bad]
    elif kind == "open_map":
        if t["of"]["type"] != "value":
            for what, bad in broken(rng, t["of"], f"{name}.a"):
                yield what, {**valid_open_map(rng, t, name), "a": bad}
        for field, f in t.get("fields", {}).items():
            for what, bad in broken(rng, f, f"{name}.{field}"):
                yield what, {**valid_open_map(rng, t, name), field: bad}
        for key in t.get("forbidden", []):
            yield (
                f"{name}.{key} given",
                {
                    **valid_open_map(rng, t, name),
                    # null is absent, a forbidden key included; a value is what is refused
                    key: rng.choice(["haltrule/0", "x", num("1"), True]),
                },
            )
    elif kind == "map":
        yield from broken_map(rng, t, name)
    elif kind == "one_of":
        for case in sorted(t["cases"]):
            for what, bad in broken_map(rng, t["cases"][case], f"{name}<{case}>"):
                yield what, {t["by"]: case, **bad}


def broken_map(rng: random.Random, t: dict, name: str):
    for field, f in t["fields"].items():
        for what, bad in broken(rng, f, f"{name}.{field}"):
            yield what, {**valid_map(rng, t), field: bad}
        if f.get("required"):
            yield (
                f"{name}.{field} not given",
                {k: v for k, v in valid_map(rng, t).items() if k != field},
            )
            if not f.get("null_is_a_value"):
                yield f"{name}.{field} null", {**valid_map(rng, t), field: None}
    if t.get("closed", True):
        yield (
            f"{name}.no_such_field given",
            {**valid_map(rng, t), "no_such_field": num("1")},
        )
    for rule in t.get("rules", []):
        if "at_most" in rule:
            a, b = rule["at_most"]
            yield (
                f"{name}.{a} above {b}",
                {**valid_map(rng, t), a: num("3"), b: num("2")},
            )
        else:
            when, needed = rule["requires"]["when"], rule["requires"]["field"]
            good = {**valid_map(rng, t), **when}
            yield (
                f"{name} {when} without {needed}",
                {k: v for k, v in good.items() if k != needed},
            )


# ------------------------------------------------------------------ cases


def generate(seed: int = SEED, accepted_per_entry: int = 80) -> tuple[dict, dict]:
    """The generated document, in the fixtures' own shape, and its cells by case id. The document is
    a function of the seed alone, so every port and every run sees the same cases."""
    rng = random.Random(seed)
    contract = load()
    doc: dict = {"fixture_version": f"generated/{seed}"}
    cells: dict[str, Cell] = {}
    counters: dict[str, int] = {}

    def add(section: str, call: dict, cell: Cell, what: str) -> None:
        counters[section] = counters.get(section, 0) + 1
        case_id = f"{section}_{cell[0]}_{counters[section]}"
        doc.setdefault(section, []).append(
            {"id": case_id, **call, "expect": {"generated": what}}
        )
        cells[case_id] = cell

    for _, entry in contract["entry_points"].items():
        section = entry["section"]
        if "call" in entry:
            # The case's own map is the argument, as a caller would hand it over.
            for _ in range(accepted_per_entry):
                add(
                    section,
                    valid_map(rng, entry["call"]),
                    ("accepted",),
                    "inside the contract",
                )
            for _ in range(PLANTINGS):
                for what, bad in broken_map(rng, entry["call"], "call"):
                    add(section, bad, ("refused", None), what)
            continue
        arguments = entry["arguments"]

        def a_call(bare: bool) -> dict:
            # Every argument is written, null when absent: which key a case writes is the protocol's.
            return {
                argument: valid(rng, t, argument)
                if t.get("required") or (not bare and rng.random() < 0.7)
                else ([] if t.get("each_is_a_call") else None)
                for argument, t in arguments.items()
            }

        def plantings() -> list[dict]:
            return [a_call(bare=k == 0) for k in range(PLANTINGS)]

        for _ in range(accepted_per_entry):
            add(section, a_call(bare=False), ("accepted",), "inside the contract")

        for argument, t in arguments.items():
            if t.get("each_is_a_call"):
                # A charge, a report: the defect refuses that one call and leaves the batch as it was.
                for what, bad in broken(rng, t["of"], argument, as_element=True):
                    for k, base in enumerate(plantings()):
                        others = (
                            []
                            if k == 0
                            else [
                                valid(rng, t["of"], argument)
                                for _ in range(rng.choice([1, 2]))
                            ]
                        )
                        at = rng.randint(0, len(others))
                        call = {**base, argument: others[:at] + [bad] + others[at:]}
                        add(section, call, ("refused", (t["each_is_a_call"], at)), what)
                continue
            for what, bad in broken(rng, t, argument):
                for base in plantings():
                    add(section, {**base, argument: bad}, ("refused", None), what)
            if t.get("required"):
                for base in plantings():
                    add(
                        section,
                        {**base, argument: None},
                        ("refused", None),
                        f"{argument} null",
                    )
    return doc, cells


def write(path: Path, doc: dict) -> None:
    """Writes the document whole or not at all: several checks may run in one tree at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=1, ensure_ascii=True) + "\n"
    temporary = path.with_suffix(f".{random.randrange(1 << 30)}.tmp")
    temporary.write_text(text, "ascii")
    temporary.replace(path)


# ---------------------------------------------------------------- protocol


def protocol_problem(section: str, inputs: dict) -> str | None:
    """Why a case is not a call anyone can make: a one_of element that is not a map, or whose `by` key
    names no case. That key says which of the part's calls the element is; it is the case file's and
    not an argument, so no port is asked about it - the file is refused before any port reads it."""
    for entry in load()["entry_points"].values():
        if entry["section"] != section:
            continue
        for argument, t in entry.get("arguments", {}).items():
            if t.get("type") == "list" and t["of"].get("type") == "one_of":
                by, cases = t["of"]["by"], t["of"]["cases"]
                for index, element in enumerate(inputs.get(argument) or []):
                    if not isinstance(element, dict):
                        return f"{argument}[{index}] is not a map, so it names no call"
                    if element.get(by) not in cases:
                        return f"{argument}[{index}].{by} names no call: one of {sorted(cases)}"
    if section == "run":
        return run_script_problem(inputs.get("answers"))
    return None


OUTCOME_KINDS = ("failure", "skipped", "success")


def run_script_problem(answers) -> str | None:
    """Why a run case's `answers` is not a script `call` can follow. It is the case file's and not an
    argument: absent, every call answers success; given, one list per item of the outcomes that item's
    calls answer, in order, each a map whose `kind` is one of the loop's three outcomes, a failure
    carrying `failure_message`, a string, and `failure_class`, a string or null, and nothing else."""
    if answers is None:
        return None
    if not isinstance(answers, list):
        return (
            "answers is not a list of scripts, one per item, so it scripts no outcome"
        )
    for index, script in enumerate(answers):
        if not isinstance(script, list):
            return f"answers[{index}] is not a list, so it scripts no outcome"
        for at, outcome in enumerate(script):
            where = f"answers[{index}][{at}] is no outcome"
            if (
                not isinstance(outcome, dict)
                or outcome.get("kind") not in OUTCOME_KINDS
            ):
                return f"{where}: a map whose kind is one of {list(OUTCOME_KINDS)}"
            if outcome["kind"] != "failure":
                if set(outcome) != {"kind"}:
                    return (
                        f"{where}: a {outcome['kind']} carries nothing beside its kind"
                    )
                continue
            if (
                set(outcome) != {"kind", "failure_message", "failure_class"}
                or not isinstance(outcome["failure_message"], str)
                or not (
                    outcome["failure_class"] is None
                    or isinstance(outcome["failure_class"], str)
                )
            ):
                return (
                    f"{where}: a failure carries failure_message, a string, and"
                    " failure_class, a string or null, and nothing else"
                )
    return None


# ------------------------------------------------------------------ judging


def holds_a_refusal(node) -> bool:
    if isinstance(node, dict):
        return node == {"refused": True} or any(
            holds_a_refusal(v) for v in node.values()
        )
    return isinstance(node, list) and any(holds_a_refusal(v) for v in node)


def judge(cells: dict, lines: dict) -> list[str]:
    """What is wrong with a port's lines over the generated cases: `lines` maps a case id to its
    parsed result line. Missing lines and extra lines are the caller's to notice."""
    failures = []
    for case_id, cell in cells.items():
        line = lines.get(case_id)
        if line is None:
            continue
        if "raised" in line:
            failures.append(
                f"FAIL [{case_id}] field=generated.raised\n  actual:   {json.dumps(line)[:300]}"
            )
            continue
        if line.get("unbuildable"):
            failures.append(
                f"FAIL [{case_id}] field=generated.unbuildable — every generated input can be built"
            )
            continue
        actual = line.get("actual")
        if cell[0] == "refused":
            at = cell[1]
            found = actual
            if at is not None:
                field, index = at
                found = (
                    actual.get(field, [None] * (index + 1))[index]
                    if isinstance(actual, dict)
                    and isinstance(actual.get(field), list)
                    and index < len(actual[field])
                    else None
                )
            if found != {"refused": True}:
                where = "" if at is None else f" at {at[0]}[{at[1]}]"
                failures.append(
                    f"FAIL [{case_id}] field=generated.refusal — a call with one defect was not refused{where}\n  actual:   {json.dumps(actual)[:300]}"
                )
        elif holds_a_refusal(actual):
            failures.append(
                f"FAIL [{case_id}] field=generated.acceptance — a call inside the contract was refused\n  actual:   {json.dumps(actual)[:300]}"
            )
    return failures


def disagreements(cells: dict, outputs: dict[str, dict]) -> list[str]:
    """Where ports answer a generated call differently: `outputs` maps a port's name to its lines by
    case id. Every port's answer to a call inside the contract must be one line."""
    failures = []
    for case_id, cell in cells.items():
        if cell[0] != "accepted":
            continue
        answers: dict[str, list[str]] = {}
        for port, lines in outputs.items():
            line = lines.get(case_id)
            answers.setdefault(json.dumps(line, sort_keys=True), []).append(port)
        if len(answers) > 1:
            shown = "\n".join(
                f"  {', '.join(ports)}: {answer[:240]}"
                for answer, ports in answers.items()
            )
            failures.append(
                f"FAIL [{case_id}] field=generated.identity — the ports do not agree\n{shown}"
            )
    return failures
