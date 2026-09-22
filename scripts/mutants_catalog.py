"""The mutants: one planted defect each, and the evidence scripts/check.sh must fail with.

These are the defects no mutation tool makes: in the judges (the driver, the survivor comparison), in an
adapter, in a fixture file, and the mistakes purity refuses (an import, a debugging print, a helper file).
A defect in a port's own modules is the mainstream tools' to plant - scripts/survivors.py - not this list's.

scripts/mutants.py is the engine; this is the list. Evidence is matched against the FAIL lines of check.sh
only - a gate's own line, a case the driver reports (FAIL [id] field=section.expect), or a purity tool's
finding (FAIL [finding] ...).
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Edit:
    path: str
    old: str | None  # None creates `path`, which must not exist yet
    new: str


@dataclasses.dataclass(frozen=True)
class Mutant:
    id: str
    edits: tuple[Edit, ...]
    expect: tuple[str, ...]  # each must appear in some FAIL line
    forbid: tuple[str, ...] = ()  # none may appear in any FAIL line


def mutant(id: str, path: str, old: str, new: str, expect, forbid=()) -> Mutant:
    return Mutant(id, (Edit(path, old, new),), tuple(expect), tuple(forbid))


TS_FAILS = "  FAIL  typescript conforms"
PY_FAILS = "  FAIL  python conforms"
DRIVER_FAILS = "  FAIL  every corrupted expectation fails under its own id"
GO_FAILS = "  FAIL  go conforms"
SURVIVORS_FAIL = (
    "  FAIL  a survivor of the mutation tools that is not on the list fails"
)
TS_NO_HOST = "  FAIL  typescript modules compile with no host types"
RS_FAILS = "  FAIL  rust conforms"
RS_NO_HOST = "  FAIL  the rust library is no_std"
GO_NO_HOST = "  FAIL  go modules import only what the allowlist holds"
TS_NAMES = "  FAIL  typescript modules name no clock"
PY_NAMES = "  FAIL  python modules import and use only what the allowlists hold"
CLI_FAILS = "  FAIL  the python shell program answers every case a shell can make"
GO_CLI_FAILS = "  FAIL  the go shell program answers every case a shell can make"
GO_CONTRACT_FAILS = (
    "  FAIL  the contract compiled into the go shell program is spec/contract.json"
)

CATALOG: list[Mutant] = []

# --- the result line each adapter writes (fixtures/protocol/v0.json)
CATALOG += [
    mutant(
        "ts adapter: a verdict is compared without its message",
        "ts/adapter/adapter.ts",
        'function normative(result: Verdict): Omit<Verdict, "message"> {',
        'function normative(result: Verdict): Verdict {\n  return result;\n}\nfunction unusedNormative(result: Verdict): Omit<Verdict, "message"> {',
        [TS_FAILS, "FAIL [no_caps_never_exhausted] field=charge.expect"],
    ),
    mutant(
        "ts result line: keys are sorted, not left in the object's own order",
        "ts/adapter/adapter.ts",
        "    const members = keys\n      .sort()\n",
        "    const members = keys\n",
        [
            TS_FAILS,
            "FAIL [line_keys_numeric_looking_sort_as_text] field=result_line.expect",
        ],
    ),
    mutant(
        "ts result line: keys are sorted by code unit, not by locale",
        "ts/adapter/adapter.ts",
        "    const members = keys\n      .sort()\n",
        "    const members = keys\n      .sort((a, b) => a.localeCompare(b))\n",
        [TS_FAILS, "FAIL [line_keys_empty_case_and_prefix] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a fraction is refused",
        "ts/adapter/adapter.ts",
        "    if (!Number.isSafeInteger(value)) {\n",
        "    if (!Number.isFinite(value)) {\n",
        [TS_FAILS, "FAIL [line_fraction_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: an integer past the range is refused",
        "ts/adapter/adapter.ts",
        "    if (!Number.isSafeInteger(value)) {\n",
        "    if (!Number.isInteger(value)) {\n",
        [TS_FAILS, "FAIL [line_integer_past_range_refused] field=result_line.expect"],
        ["FAIL [line_fraction_refused]"],
    ),
    mutant(
        "ts result line: a bigint past the range is refused",
        "ts/adapter/adapter.ts",
        "    if (value > BigInt(Number.MAX_SAFE_INTEGER) || value < -BigInt(Number.MAX_SAFE_INTEGER)) {\n",
        "    if (false) {\n",
        [TS_FAILS, "FAIL [line_bigint_past_range_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a bigint within the range is an integer",
        "ts/adapter/adapter.ts",
        '  if (typeof value === "bigint") {\n    // An integer is an integer however the language holds it.\n',
        '  if (typeof value === "bigint" && value < 0n) {\n    // An integer is an integer however the language holds it.\n',
        [
            TS_FAILS,
            "FAIL [line_bigint_within_range_is_an_integer] field=result_line.expect",
        ],
    ),
    mutant(
        "ts result line: a map with a key that is not a string is refused",
        "ts/adapter/adapter.ts",
        "    if (Reflect.ownKeys(source).length !== keys.length) {\n",
        "    if (false) {\n",
        [TS_FAILS, "FAIL [line_non_string_key_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: a hole in a list is refused, not skipped",
        "ts/adapter/adapter.ts",
        "    for (let index = 0; index < value.length; index += 1) items.push(canonicalStringify(value[index]));\n",
        "    value.forEach((item) => items.push(canonicalStringify(item)));\n",
        [TS_FAILS, "FAIL [line_sparse_array_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: an instance is refused, not written as a map",
        "ts/adapter/adapter.ts",
        '  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {\n',
        '  if (typeof value === "object") {\n',
        [TS_FAILS, "FAIL [line_instance_refused] field=result_line.expect"],
    ),
    mutant(
        "ts result line: markup characters are written as themselves",
        "ts/adapter/adapter.ts",
        '  if (typeof value === "string") return JSON.stringify(value);\n',
        '  if (typeof value === "string") return JSON.stringify(value).replace(/</g, "\\\\u003c");\n',
        [
            TS_FAILS,
            "FAIL [line_string_markup_and_separators_are_not_escaped] field=result_line.expect",
        ],
    ),
    mutant(
        "py result line: keys are sorted by code unit, not by code point",
        "py/adapter.py",
        "            for key in sorted(value, key=_utf16_units)\n",
        "            for key in sorted(value)\n",
        [
            PY_FAILS,
            "FAIL [line_keys_sort_by_utf16_code_unit] field=result_line.expect",
            # the route a caller's keys take into a result
            "FAIL [validation_issue_detail_keys_beyond_the_plane] field=checkpoint.expect",
        ],
        ["FAIL [line_keys_numeric_looking_sort_as_text]"],
    ),
    mutant(
        "py result line: keys are sorted, not left in insertion order",
        "py/adapter.py",
        "            for key in sorted(value, key=_utf16_units)\n",
        "            for key in value\n",
        [
            PY_FAILS,
            "FAIL [line_keys_numeric_looking_sort_as_text] field=result_line.expect",
        ],
    ),
    mutant(
        "py result line: an integral float is written as an integer",
        "py/adapter.py",
        "        value = int(value)\n    if isinstance(value, int):\n",
        "        return repr(value)\n    if isinstance(value, int):\n",
        [
            PY_FAILS,
            "FAIL [line_integral_floats_are_written_as_integers] field=result_line.expect",
        ],
    ),
    mutant(
        "py result line: a fraction is refused, not truncated",
        "py/adapter.py",
        "        if not value.is_integer():  # false for NaN and the infinities too\n",
        "        if value != value or value in (float('inf'), float('-inf')):\n",
        [PY_FAILS, "FAIL [line_fraction_refused] field=result_line.expect"],
        ["FAIL [line_nan_refused]", "FAIL [line_infinity_refused]"],
    ),
    mutant(
        "py result line: an integer past the range is refused",
        "py/adapter.py",
        "        if abs(value) > _SAFE_INTEGER:\n",
        "        if False:\n",
        [PY_FAILS, "FAIL [line_integer_past_range_refused] field=result_line.expect"],
    ),
    mutant(
        "py result line: a boolean is not an integer",
        "py/adapter.py",
        '    if isinstance(value, bool):\n        return "true" if value else "false"\n    if isinstance(value, str):\n        return _result_string(value)\n',
        "    if isinstance(value, str):\n        return _result_string(value)\n",
        [PY_FAILS, "FAIL [line_booleans_and_null] field=result_line.expect"],
    ),
    mutant(
        "py result line: a map with a key that is not a string is refused",
        "py/adapter.py",
        "        if not all(isinstance(key, str) for key in value):\n",
        "        if False:\n",
        [PY_FAILS, "FAIL [line_non_string_key_refused] field=result_line"],
    ),
    mutant(
        "py result line: an unpaired surrogate is escaped",
        "py/adapter.py",
        '        f"\\\\u{ord(ch):04x}" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in escaped\n',
        "        ch for ch in escaped\n",
        [
            PY_FAILS,
            "FAIL [line_key_with_lone_surrogate] field=result_line.missing",
            "field=adapter.exit",
        ],
    ),
    mutant(
        "py result line: text beyond ASCII is written as itself",
        "py/adapter.py",
        "    escaped = json.dumps(text, ensure_ascii=False)\n",
        "    escaped = json.dumps(text)\n",
        [
            PY_FAILS,
            "FAIL [line_string_beyond_ascii_is_written_as_itself] field=result_line.expect",
        ],
    ),
]

# --- purity: the mistakes people make, planted; each is refused by structure or by an allowlist.
# If a setting were loosened - "types": [] given node's types, a name taken off the ESLint list, a module
# added to the Python allowlist - the mutants below would survive, and that is how the loosening shows.
CATALOG += [
    mutant(
        "ts purity: a file-system import does not exist for a policy module",
        "ts/checkpoint.ts",
        'import { createHash } from "node:crypto";',
        'import { createHash } from "node:crypto";\nimport { readFileSync } from "node:fs";\nvoid readFileSync;',
        [TS_NO_HOST, "Cannot find module 'node:fs'"],
    ),
    mutant(
        "ts purity: the hash module gives createHash and nothing else",
        "ts/checkpoint.ts",
        'import { createHash } from "node:crypto";',
        'import { createHash, randomBytes } from "node:crypto";\nvoid randomBytes;',
        [TS_NO_HOST, "has no exported member 'randomBytes'"],
    ),
    mutant(
        "ts purity: the host's clock does not exist for a policy module",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        "export const loadedAt = performance.now();\nexport function canonicalize(value: unknown)",
        [TS_NO_HOST, "Cannot find name 'performance'"],
    ),
    mutant(
        "ts purity: the process does not exist for a policy module",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        "export const stamp = process.env.HOME ?? String(process.pid);\nexport function canonicalize(value: unknown)",
        [TS_NO_HOST, "Cannot find name 'process'"],
    ),
    mutant(
        "ts purity: a debugging print does not exist for a policy module",
        "ts/budget.ts",
        '    const turns = ledger(charge.turns, "turns");\n',
        '    console.log(charge);\n    const turns = ledger(charge.turns, "turns");\n',
        [TS_NO_HOST, "Cannot find name 'console'"],
    ),
    mutant(
        "ts purity: a timer does not exist for a policy module",
        "ts/slot.ts",
        "export function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        "setTimeout(() => undefined, 0);\nexport function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        [TS_NO_HOST, "Cannot find name 'setTimeout'"],
    ),
    mutant(
        "ts purity: no clock read in a policy module",
        "ts/slot.ts",
        "export function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        "export const loadedAt = Date.now();\nexport function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        [TS_NAMES, "Unexpected use of 'Date'"],
    ),
    mutant(
        "ts purity: a comment cannot switch the rule off",
        "ts/slot.ts",
        "export function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        "// eslint-disable-next-line\nexport const loadedAt = Date.now();\nexport function validateSlot(spec: SlotSpec, value: unknown): Verdict {",
        [TS_NAMES, "Unexpected use of 'Date'"],
    ),
    mutant(
        "ts purity: Math.random reached by destructuring",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        "const { random } = Math;\nexport const SALT = random();\nexport function canonicalize(value: unknown)",
        [TS_NAMES, "'Math.random' is restricted"],
    ),
    mutant(
        "ts purity: no code built from text",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        'export const stamp = new Function("return 1")();\nexport function canonicalize(value: unknown)',
        [TS_NAMES, "The Function constructor is eval"],
    ),
    mutant(
        "ts purity: no way to every global through the global object",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        "export const reach = (globalThis as Record<string, unknown>).process;\nexport function canonicalize(value: unknown)",
        [TS_NAMES, "Unexpected use of 'globalThis'"],
    ),
    mutant(
        "ts purity: no comparison that follows the locale",
        "ts/checkpoint.ts",
        "export function canonicalize(value: unknown)",
        'export const order = "a".localeCompare("B");\nexport function canonicalize(value: unknown)',
        [TS_NAMES, "'localeCompare' is restricted"],
    ),
    Mutant(
        "ts purity: a helper in a subdirectory is a policy module too",
        (
            Edit("ts/helpers/clock.ts", None, "export const loadedAt = Date.now();\n"),
            Edit(
                "ts/slot.ts",
                'import { verdict, type Verdict } from "./verdict.ts";\n',
                'import { verdict, type Verdict } from "./verdict.ts";\nimport { loadedAt } from "./helpers/clock.ts";\nvoid loadedAt;\n',
            ),
        ),
        (TS_NAMES, "helpers/clock.ts", "Unexpected use of 'Date'"),
    ),
    mutant(
        "py purity: no clock import in a policy module",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\nimport time  # noqa: F401\n",
        [
            PY_NAMES,
            "py/haltrule/checkpoint.py",
            "imports time, which is not on the allowlist",
        ],
    ),
    mutant(
        "py purity: no clock import in the budget either",
        "py/haltrule/budget.py",
        "from haltrule.verdict import verdict\n",
        "from haltrule.verdict import verdict\nfrom datetime import datetime  # noqa: F401\n",
        [
            PY_NAMES,
            "py/haltrule/budget.py",
            "imports from datetime, which is not on the allowlist",
        ],
    ),
    mutant(
        "py purity: no file I/O through pathlib",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\nimport pathlib\n\n_SELF = pathlib.Path(__file__).read_text()\n",
        [
            PY_NAMES,
            "imports pathlib, which is not on the allowlist",
            "uses the builtin __file__",
        ],
    ),
    mutant(
        "py purity: open reached through an alias",
        "py/haltrule/checkpoint.py",
        "import hashlib\n",
        "import hashlib\n\n_read = open\n_SOURCE = _read(__file__).read()\n",
        [PY_NAMES, "uses the builtin open, which is not on the allowlist"],
    ),
    mutant(
        "py purity: no debugging print",
        "py/haltrule/budget.py",
        '        turns = _ledger(turns, "turns")\n',
        '        print(turns)\n        turns = _ledger(turns, "turns")\n',
        [PY_NAMES, "uses the builtin print, which is not on the allowlist"],
    ),
    mutant(
        "py purity: no hash() or id() draw",
        "py/haltrule/checkpoint.py",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n    _nd = hash(at) ^ id(value)  # noqa: F841\n",
        [PY_NAMES, "uses the builtin hash", "uses the builtin id"],
    ),
    mutant(
        "py purity: no repr() address draw",
        "py/haltrule/checkpoint.py",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n    _nd = repr(object())  # noqa: F841\n",
        [PY_NAMES, "uses the builtin repr", "uses the builtin object"],
    ),
    mutant(
        "py purity: a debugging leftover from an allowed module is still not allowed",
        "py/haltrule/slot.py",
        "from typing import Any, Optional\n",
        "from typing import Any, Optional, reveal_type  # noqa: F401\n",
        [PY_NAMES, "imports typing.reveal_type, which is not on the allowlist"],
    ),
    mutant(
        "py purity: no value read through an allowed module's own imports",
        "py/haltrule/budget.py",
        "from haltrule.verdict import verdict\n",
        'import typing\n\nfrom haltrule.verdict import verdict\n\n_stamp = typing.sys.modules["os"].getpid()\n',
        [PY_NAMES, "uses typing.sys, which is not on the allowlist"],
    ),
    mutant(
        "py purity: an allowed module is used by its listed names, never as a value",
        "py/haltrule/breaker.py",
        "import math\n",
        "import math\n\n_M = math\n",
        [PY_NAMES, "uses the module math as a value"],
    ),
    Mutant(
        "py purity: a helper in the package marker is a policy module too",
        (
            Edit(
                "py/haltrule/__init__.py",
                '"""haltrule: each part is a module of its own; the package holds nothing else."""\n',
                '"""haltrule: each part is a module of its own; the package holds nothing else."""\n\nimport time\n\n\ndef read_clock():\n    return time.time()\n',
            ),
        ),
        (
            PY_NAMES,
            "py/haltrule/__init__.py",
            "imports time, which is not on the allowlist",
        ),
    ),
    Mutant(
        "py purity: a module in a subpackage is not a sibling",
        (
            Edit("py/haltrule/sub/__init__.py", None, ""),
            Edit(
                "py/haltrule/sub/clock.py", None, "import time\n\nNOW = time.time()\n"
            ),
            Edit(
                "py/haltrule/budget.py",
                "from haltrule.verdict import verdict\n",
                "from haltrule.sub.clock import NOW  # noqa: F401\nfrom haltrule.verdict import verdict\n",
            ),
        ),
        (
            PY_NAMES,
            "imports from haltrule.sub.clock, which is not a file beside this one",
        ),
    ),
    mutant(
        "py purity: no reaching behind an object for its class, its globals, or the builtins",
        "py/haltrule/checkpoint.py",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n",
        "def _encode_value(value: Any, at: str, depth: int) -> str:\n    _kinds = ().__class__.__base__.__subclasses__()  # noqa: F841\n",
        [PY_NAMES, "uses the attribute __class__", "uses the attribute __subclasses__"],
    ),
    Mutant(
        "py adapter: a file beside it cannot stand in for the standard library",
        (
            Edit(
                "py/hashlib.py",
                None,
                'def sha256(data=b""):\n    raise SystemExit("py/hashlib.py was imported in place of the standard library")\n',
            ),
            Edit("py/adapter.py", "sys.path.append(sys.path.pop(0))\n", "pass\n"),
        ),
        (PY_FAILS, "field=adapter.exit"),
    ),
    mutant(
        "py determinism: no hash-seeded set order in a result",
        "py/haltrule/checkpoint.py",
        "for dependency_id in sorted(expected_dependencies, key=_utf16_key):",
        "for dependency_id in list(set(expected_dependencies)):",
        [PY_FAILS, "field=checkpoint.expect"],
    ),
]

# --- the driver: the one place that judges, so the one place a blind spot would hide every port's defects.
# Its self-test is a gate; each of these blinds the driver one way and the self-test must say which.
CATALOG += [
    mutant(
        "driver: a line that differs from the expected line fails",
        "scripts/conform.py",
        "        elif raw != want[key]:\n",
        "        elif False:\n",
        [DRIVER_FAILS, "corrupted expectation(s) passed"],
    ),
    mutant(
        "driver: a case with no line fails",
        "scripts/conform.py",
        "        if (section, case_id) not in seen:\n",
        "        if False:\n",
        [DRIVER_FAILS, "a missing line passed", "no output at all passed"],
    ),
    mutant(
        "driver: a line printed twice fails",
        "scripts/conform.py",
        "        elif key in seen:\n",
        "        elif False:\n",
        [DRIVER_FAILS, "a repeated line passed"],
    ),
    mutant(
        "driver: a line for no case fails",
        "scripts/conform.py",
        '            failures.append(\n                f"FAIL [{key[1]}] field={key[0]}.unexpected — no fixture holds this case"\n            )\n',
        "            continue\n",
        [DRIVER_FAILS, "a line for no case passed"],
    ),
    mutant(
        "driver: lines out of fixture order fail",
        "scripts/conform.py",
        "    if not failures and seen != list(want):\n",
        "    if False:\n",
        [DRIVER_FAILS, "lines out of order passed"],
    ),
    mutant(
        "driver: a raw number in an input is refused",
        "scripts/conform.py",
        "    if isinstance(node, (int, float)) and not isinstance(node, bool):\n",
        "    if False:\n",
        [
            DRIVER_FAILS,
            "a raw number in an input was accepted",
            "a raw 1.0 in an input was accepted",
        ],
    ),
    mutant(
        "driver: a $number literal is read by the whole grammar",
        "scripts/conform.py",
        "            if not NUMBER_LITERAL.fullmatch(literal):\n",
        "            if not NUMBER_LITERAL.match(literal):\n",
        [DRIVER_FAILS, "the $number literal '1_0' was accepted"],
        ["the $number literal 'nan' was accepted"],
    ),
    mutant(
        "driver: the $number grammar has no leading zeros",
        "scripts/conform.py",
        'NUMBER_LITERAL = re.compile(\n    r"-?(0|[1-9][0-9]*)',
        'NUMBER_LITERAL = re.compile(\n    r"-?([0-9]+)',
        [DRIVER_FAILS, "the $number literal '01' was accepted"],
    ),
    mutant(
        "driver: a $bigint literal is an integer by the grammar",
        "scripts/conform.py",
        "            if not INTEGER_LITERAL.fullmatch(literal):\n",
        "            if False:\n",
        [DRIVER_FAILS, "the $bigint literal '1.0' was accepted"],
    ),
    mutant(
        "driver: a $number the double reads back as another number is refused",
        "scripts/conform.py",
        "            if literal not in SPELLED_OUT and not _a_double_reads_it_back(literal):\n",
        "            if False:\n",
        [
            DRIVER_FAILS,
            "the $number 9007199254740993, which a double rounds was accepted",
            "the $number 1e300, an integer written and a different integer read was accepted",
            "the $number 9007199254740991.5, a fraction written and an integer read was accepted",
        ],
    ),
    mutant(
        "driver: an unknown $unsupported kind is refused",
        "scripts/conform.py",
        "            if literal not in UNSUPPORTED_KINDS:\n",
        "            if False:\n",
        [DRIVER_FAILS, "an unknown $unsupported kind was accepted"],
    ),
    mutant(
        "driver: a fixture file is ASCII",
        "scripts/conform.py",
        '        doc = json.loads(raw.decode("ascii"), object_pairs_hook=_no_duplicate_keys)\n',
        '        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)\n',
        [DRIVER_FAILS, "a non-ASCII byte was accepted"],
    ),
    mutant(
        "driver: an object that repeats a key is refused",
        "scripts/conform.py",
        '        doc = json.loads(raw.decode("ascii"), object_pairs_hook=_no_duplicate_keys)\n',
        '        doc = json.loads(raw.decode("ascii"))\n',
        [DRIVER_FAILS, "a repeated key was accepted"],
    ),
    mutant(
        "driver: a fixture file names itself by its path",
        "scripts/conform.py",
        '    if not isinstance(doc, dict) or doc.get("fixture_version") != name:\n',
        "    if not isinstance(doc, dict):\n",
        [DRIVER_FAILS, "a fixture_version that is not the file's path was accepted"],
    ),
    mutant(
        "driver: a digest is sha256 of its canonical form, checked with no port involved",
        "scripts/conform.py",
        '                if digest != expect["digest"]:\n',
        "                if False:\n",
        [
            DRIVER_FAILS,
            "a digest that is not sha256 of its canonical form was accepted",
        ],
    ),
    mutant(
        "driver: its own lines order keys by UTF-16 code unit",
        "scripts/conform.py",
        '        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))\n',
        "        keys = sorted(value)\n",
        [
            DRIVER_FAILS,
            "the driver writes line_keys_sort_by_utf16_code_unit differently from the vector",
        ],
    ),
    mutant(
        "driver: an adapter that exits non-zero has not conformed",
        "ts/adapter/adapter.ts",
        "  process.exit(1);\n});\n",
        "  process.exit(1);\n});\nprocess.exitCode = 3;\n",
        [TS_FAILS, "field=adapter.exit"],
        [PY_FAILS],
    ),
]

# --- the fixtures and the adapters
CATALOG += [
    mutant(
        "fixtures: every fixture file is ASCII",
        "fixtures/checkpoint/v0.json",
        '"id": "hangul_nfc"',
        '"id": "hangul_nfc_가"',
        ["FAIL [fixtures] checkpoint/v0: not ASCII"],
    ),
    mutant(
        "fixtures: a case id is used once across every file",
        "fixtures/slot/v0.json",
        '"id": "choice_first_candidate_accepted"',
        '"id": "no_caps_never_exhausted"',
        ["FAIL [fixtures] a case id is used twice: no_caps_never_exhausted"],
    ),
    Mutant(
        "fixtures: an added file that names another version is refused",
        (
            Edit(
                "fixtures/checkpoint/v1.json",
                None,
                '{"fixture_version": "checkpoint/v999", "canonicalize": [{"id": "added_file_null", "input": null, "expect": {"canonical": "WRONG", "digest": "sha256:WRONG"}}]}\n',
            ),
        ),
        ("FAIL [fixtures] checkpoint/v1: fixture_version must be 'checkpoint/v1'",),
    ),
    Mutant(
        "fixtures: a section no adapter has a function for cannot pass as zero cases",
        (
            Edit(
                "fixtures/extra/v0.json",
                None,
                '{"fixture_version": "extra/v0", "never_read": [{"id": "never_read_case", "expect": null}]}\n',
            ),
        ),
        (TS_FAILS, PY_FAILS, "FAIL [never_read_case] field=never_read.missing"),
    ),
    mutant(
        "py breaker: a delay of zero or less is not handed to sleep",
        "py/haltrule/breaker.py",
        """            sleep(
                dispatch_backoff_delay_ms(
                    attempt=attempt - 1, initial_ms=initial_ms, cap_ms=cap_ms
                )
            )
            continue""",
        """            _delay = dispatch_backoff_delay_ms(
                attempt=attempt - 1, initial_ms=initial_ms, cap_ms=cap_ms
            )
            if _delay > 0:
                sleep(_delay)
            continue""",
        [PY_FAILS, "FAIL [spec_run_a_delay_of_zero_is_still_a_sleep] field=run.expect"],
        [TS_FAILS],
    ),
    mutant(
        "ts breaker: the loop swallows what the caller's call raises",
        "ts/breaker.ts",
        "      const outcome: DispatchOutcome = await call(itemId);\n",
        """      let outcome: DispatchOutcome;
      try {
        outcome = await call(itemId);
      } catch (error) {
        outcome = { kind: "failure", failure_message: String(error), failure_class: null };
      }
""",
        [
            TS_FAILS,
            "FAIL [spec_run_a_call_that_raises_is_let_out] field=run.raised",
        ],
        [PY_FAILS],
    ),
    mutant(
        "driver: a case that was to raise passes on any line",
        "scripts/conform.py",
        """    return (
        set(parsed) == {"id", "raised", "section"}
        and isinstance(parsed.get("raised"), str)
        and RAISED_BY_THE_SCRIPT in parsed["raised"]
    )""",
        "    return True",
        [
            DRIVER_FAILS,
            "FAIL [self-test] a case that was to raise and answered instead passed",
        ],
    ),
    mutant(
        "ts breaker: the loop waits for its sleep before it calls again",
        "ts/breaker.ts",
        "        await sleep(dispatchBackoffDelayMs({ attempt: attempt - 1, initial_ms: initialMs, cap_ms: capMs }));\n",
        "        sleep(dispatchBackoffDelayMs({ attempt: attempt - 1, initial_ms: initialMs, cap_ms: capMs }));\n",
        [TS_FAILS, "FAIL [skipped_completes_and_proves_nothing] field=run.raised"],
        [PY_FAILS],
    ),
    mutant(
        "fixtures: a run script holding an answer no call asks for is reported by every adapter",
        "fixtures/breaker/v0.json",
        '      "answers": [],\n',
        '      "answers": [[{"kind": "success"}]],\n',
        [TS_FAILS, PY_FAILS, GO_FAILS, RS_FAILS, "FAIL [empty_batch] field=run.raised"],
    ),
    mutant(
        "fixtures: a run script with no answer for a call the loop makes is reported by every adapter",
        "fixtures/breaker/v0.json",
        '      "id": "no_answers_means_every_call_succeeds",\n',
        '      "id": "no_answers_means_every_call_succeeds",\n      "answers": [[{"kind": "success"}]],\n',
        [
            TS_FAILS,
            PY_FAILS,
            GO_FAILS,
            RS_FAILS,
            "FAIL [no_answers_means_every_call_succeeds] field=run.raised",
        ],
    ),
    mutant(
        "ts adapter: every section is run",
        "ts/adapter/adapter.ts",
        '      if (section === "fixture_version") continue;\n',
        '      if (section === "fixture_version" || section === "state") continue;\n',
        [TS_FAILS, "FAIL [trip_at_threshold_three_rate_limit] field=state.missing"],
        [PY_FAILS],
    ),
    mutant(
        "py adapter: every section is run",
        "py/adapter.py",
        '            if section == "fixture_version":\n',
        '            if section in ("fixture_version", "validate"):\n',
        [PY_FAILS, "FAIL [choice_first_candidate_accepted] field=validate.missing"],
        [TS_FAILS],
    ),
    mutant(
        "py adapter: a verdict is compared without its message",
        "py/adapter.py",
        '    return {key: value for key, value in result.items() if key != "message"}\n\n\ndef _normative_open',
        "    return dict(result)\n\n\ndef _normative_open",
        [PY_FAILS, "FAIL [no_caps_never_exhausted] field=charge.expect"],
    ),
]

# --- the cli: the answer is the port's, the exit code is the verdict
CATALOG += [
    mutant(
        "go cli: the contract it carries is not the one in spec/",
        "go/cli/contract.json",
        '"of": ["choice", "text", "score"]',
        '"of": ["choice", "text", "score", "guess"]',
        [GO_CONTRACT_FAILS],
    ),
    mutant(
        "go cli: a halt exits as ok",
        "go/cli/main.go",
        'levels := map[string]int{"ok": 0, "warning": 1, "halt": 2}',
        'levels := map[string]int{"ok": 0, "warning": 1, "halt": 0}',
        [GO_CLI_FAILS, "FAIL [artifact_missing] field=checkpoint.cli_exit"],
        [CLI_FAILS],
    ),
    mutant(
        "go cli: a refusal exits as an answer",
        "go/cli/main.go",
        '\t\tfmt.Fprintf(problems, "refused: %v\\n", err)\n\t\treturn exitRefused',
        '\t\tfmt.Fprintf(problems, "refused: %v\\n", err)\n\t\treturn 0',
        [GO_CLI_FAILS, "field=validate.cli_exit"],
        [CLI_FAILS],
    ),
    mutant(
        "driver: the cli is asked for a call that takes a function",
        "scripts/conform.py",
        '        if not entry.get("takes_a_function")\n',
        "        if True\n",
        [CLI_FAILS, "field=run.cli_output"],
    ),
    mutant(
        "driver: the cli judge lets a verdict reach a person without its message",
        "scripts/conform.py",
        "    if not messages_kept(answer):\n",
        "    if False:\n",
        [DRIVER_FAILS, "FAIL [self-test] a verdict without its message passed"],
    ),
    mutant(
        "driver: the cli judge accepts an exit code above the verdict",
        "scripts/conform.py",
        "    if code != worst_verdict(expect):\n",
        "    if code < worst_verdict(expect):\n",
        [
            DRIVER_FAILS,
            "FAIL [self-test] a cli exit code above the expected level passed",
        ],
    ),
    mutant(
        "driver: the cli judge strips a caller's own message",
        "scripts/conform.py",
        '            if not (key == "message" and node.get("verdict") in VERDICT_LEVELS)\n',
        '            if key != "message"\n',
        [DRIVER_FAILS, "FAIL [self-test] a caller's own message was stripped"],
    ),
    mutant(
        "cli: a halt exits as ok",
        "py/cli.py",
        'LEVELS = {"ok": 0, "warning": 1, "halt": 2}\n',
        'LEVELS = {"ok": 0, "warning": 1, "halt": 0}\n',
        [CLI_FAILS, "FAIL [artifact_missing] field=checkpoint.cli_exit"],
        [PY_FAILS],
    ),
    mutant(
        "cli: a refusal exits as an answer",
        "py/cli.py",
        '        sys.stderr.write(f"refused: {error}\\n")\n        return REFUSED\n',
        '        sys.stderr.write(f"refused: {error}\\n")\n        return 0\n',
        [CLI_FAILS, "field=validate.cli_exit"],
        [PY_FAILS],
    ),
    mutant(
        "go cli: a pipe is taken for a terminal",
        "go/cli/main.go",
        "stdin.Mode()&os.ModeCharDevice != 0",
        "stdin.Mode()&os.ModeCharDevice == 0",
        [GO_CLI_FAILS, "field=cli.from_the_standard_input"],
        [CLI_FAILS],
    ),
    mutant(
        "driver: a probe the shell programs are never asked",
        "scripts/conform.py",
        "    from_the_standard_input,\n",
        "",
        [DRIVER_FAILS, "a probe nobody calls: from_the_standard_input"],
    ),
    mutant(
        "cli: an entry point answers with another's function",
        "py/cli.py",
        '    "checkpoint.evaluate": evaluate,\n',
        '    "checkpoint.evaluate": canonical,\n',
        [CLI_FAILS, "FAIL [artifact_missing] field=checkpoint.cli_answer"],
        [PY_FAILS],
    ),
]

# --- the adapters answer a refusal as a refusal
CATALOG += [
    mutant(
        "ts adapter: the checkpoint's refusal is a refusal, not a crash",
        "ts/adapter/adapter.ts",
        "  const result = orRefused(() => evaluateCheckpointArtifact(tc.args as EvaluateCheckpointArtifactArgs));\n  return result === REFUSED ? { refused: true } : result.map(normativeOpen);",
        "  return evaluateCheckpointArtifact(tc.args as EvaluateCheckpointArtifactArgs).map(normativeOpen);",
        [TS_FAILS, "FAIL [refused_artifact_false] field=checkpoint.raised"],
    ),
    mutant(
        "py adapter: the checkpoint's refusal is a refusal, not a crash",
        "py/adapter.py",
        '    result = _or_refused(lambda: evaluate_checkpoint_artifact(**tc["args"]))\n    if result is _REFUSED:\n        return {"refused": True}\n    return [_normative_open(issue) for issue in result]\n',
        '    return [\n        _normative_open(issue)\n        for issue in evaluate_checkpoint_artifact(**tc["args"])\n    ]\n',
        [PY_FAILS, "FAIL [refused_artifact_false] field=checkpoint.raised"],
    ),
]

# --- the survivor comparison: the judge of the mutation tools' runs
CATALOG += [
    mutant(
        "survivors: a survivor that is not listed fails",
        "scripts/survivors.py",
        "sorted((have - want).elements())",
        "sorted((want - want).elements())",
        [SURVIVORS_FAIL, "FAIL [self-test] a new survivor fails"],
    ),
    mutant(
        "survivors: a listed survivor that is gone fails",
        "scripts/survivors.py",
        "sorted((want - have).elements())",
        "sorted((have - have).elements())",
        [SURVIVORS_FAIL, "FAIL [self-test] a stale entry fails"],
    ),
    mutant(
        "survivors: two survivors that look alike are two",
        "scripts/survivors.py",
        "    have, want = collections.Counter(found), collections.Counter(accepted)\n",
        "    have, want = collections.Counter(set(found)), collections.Counter(accepted)\n",
        [
            SURVIVORS_FAIL,
            "FAIL [self-test] a second survivor that looks the same fails",
        ],
    ),
    mutant(
        "survivors: a run of no mutants has shown nothing",
        "scripts/survivors.py",
        "    if total == 0:\n",
        "    if total < 0:\n",
        [SURVIVORS_FAIL, "FAIL [self-test] a run of nothing fails"],
    ),
    mutant(
        "survivors: a mutant the typescript run did not notice is a survivor",
        "scripts/survivors.py",
        '            if mutant["status"] not in ("Survived", "NoCoverage"):\n',
        '            if mutant["status"] != "NoCoverage":\n',
        [SURVIVORS_FAIL, "FAIL [self-test] a stryker report is read"],
    ),
    mutant(
        "survivors: a mutant the go run ran out of time on is a survivor",
        "scripts/survivors.py",
        '            if mutant["status"] in ("NOT VIABLE", "SKIPPED"):\n',
        '            if mutant["status"] in ("NOT VIABLE", "SKIPPED", "TIMED OUT"):\n',
        [SURVIVORS_FAIL, "FAIL [self-test] a gremlins report is read"],
    ),
    mutant(
        "survivors: a mutant the python run did not notice is a survivor",
        "scripts/survivors.py",
        '        if result["test_outcome"] != "survived":\n',
        '        if result["test_outcome"] != "survived" or True:\n',
        [SURVIVORS_FAIL, "FAIL [self-test] a cosmic-ray dump is read"],
    ),
]

# --- what not every language can hold: a port may sit out those cases, and only those
CATALOG += [
    mutant(
        "driver: unbuildable is taken only for an input not every language can hold",
        "scripts/conform.py",
        "            if key in may_be_unbuildable:\n",
        "            if key in want:\n",
        [
            DRIVER_FAILS,
            "FAIL [self-test] unbuildable, of an input every language holds passed",
        ],
    ),
    mutant(
        "driver: a port held to every input may not say unbuildable",
        "scripts/conform.py",
        "        if not every_language and not every_input and not answers_refused(expect)\n",
        "        if not every_language and not answers_refused(expect)\n",
        [
            DRIVER_FAILS,
            "FAIL [self-test] a port held to every input said unbuildable and passed",
        ],
    ),
    mutant(
        "driver: an unpaired surrogate in a key is beyond some languages too",
        "scripts/conform.py",
        "            every_language_holds(key) and every_language_holds(value)\n",
        "            every_language_holds(value)\n",
        [
            DRIVER_FAILS,
            "FAIL [self-test] an unpaired surrogate in a key was read as within every language",
        ],
    ),
    mutant(
        "driver: an $unsupported value is beyond some languages",
        "scripts/conform.py",
        '        if "$unsupported" in node:\n            return False\n',
        "",
        [
            DRIVER_FAILS,
            "FAIL [self-test] an $unsupported value was read as within every language",
        ],
    ),
    mutant(
        "ts adapter: a port that can build every input may not sit a case out",
        "ts/adapter/adapter.ts",
        "        let text: string;\n",
        '        if (section === "validate" && typeof tc.value === "string" && !tc.value.isWellFormed()) {\n          console.log(canonicalStringify({ id, section, unbuildable: true }));\n          continue;\n        }\n        let text: string;\n',
        [
            TS_FAILS,
            "FAIL [text_lone_high_surrogate_is_invalid] field=validate.unbuildable",
        ],
    ),
    mutant(
        "py adapter: a port that can build every input may not sit a case out",
        "py/adapter.py",
        '                line = {"id": case["id"], "section": section}\n',
        '                line = {"id": case["id"], "section": section}\n                if section == "canonicalize" and "$unsupported" in json.dumps(case):\n                    print(_canonical_json({**line, "unbuildable": True}))\n                    continue\n',
        [PY_FAILS, "FAIL [sparse_array] field=canonicalize.unbuildable"],
    ),
]

CATALOG += [
    mutant(
        "driver: an integer past a signed 64-bit one is beyond some languages",
        "scripts/conform.py",
        '            return -(2**63) <= int(node["$bigint"]) < 2**63\n',
        "            return True\n",
        [
            DRIVER_FAILS,
            "FAIL [self-test] a $bigint past it was read as within every language",
        ],
    ),
    mutant(
        "driver: where the answer is a refusal, nothing is sat out",
        "scripts/conform.py",
        "        if not every_language and not every_input and not answers_refused(expect)\n",
        "        if not every_language and not every_input\n",
        [
            DRIVER_FAILS,
            "FAIL [self-test] unbuildable, where a refusal is the answer, passed",
        ],
    ),
    mutant(
        "driver: a refusal counts wherever it sits in the answer",
        "scripts/conform.py",
        '        return expect == {"refused": True} or any(\n',
        '        return expect == {"refused": True} or all(\n',
        [
            DRIVER_FAILS,
            "FAIL [self-test] unbuildable, where a refusal is one of the answers, passed",
        ],
    ),
]

# --- the constants a mutation tool cannot reach: a const has no line to cover
CATALOG += [
    mutant(
        "go slot: the largest allowed bound is 2^53 - 1",
        "go/slot.go",
        "const boundMax = 1<<53 - 1",
        "const boundMax = 1 << 53",
        [GO_FAILS, "FAIL [refused_slot_bound_past_2_53_1] field=validate.expect"],
    ),
    mutant(
        "rust slot: a score's range ends at 2^53 - 1",
        "rust/haltrule/src/slot.rs",
        "const BOUND_MAX_AS_FLOAT: f64 = BOUND_MAX as f64;",
        "const BOUND_MAX_AS_FLOAT: f64 = (BOUND_MAX + 1) as f64;",
        [
            RS_FAILS,
            "FAIL [score_integer_past_the_shared_range_is_invalid] field=validate.expect",
            "FAIL [refused_score_integral_min_past_the_shared_range] field=validate.expect",
        ],
    ),
    mutant(
        "go canonicalize: the largest integer is 2^53 - 1",
        "go/checkpoint.go",
        "const maxSafeInteger = 1<<53 - 1",
        "const maxSafeInteger = 1 << 53",
        [
            GO_FAILS,
            "FAIL [spec_canon_two_pow_53_written_as_float] field=canonicalize.expect",
        ],
    ),
    mutant(
        "go result line: the largest integer a line carries is 2^53 - 1",
        "go/adapter/main.go",
        "const lineMaxInteger = 1<<53 - 1",
        "const lineMaxInteger = 1 << 53",
        [GO_FAILS, "FAIL [line_integer_past_range_refused] field=result_line.expect"],
    ),
]

# --- the go adapter: every case is run, and only what cannot be built is sat out
CATALOG += [
    mutant(
        "go adapter: every section is run",
        "go/adapter/main.go",
        "\t\tfor _, raw := range cases {\n",
        '\t\tfor _, raw := range cases {\n\t\t\tif sectionName == "backoff" {\n\t\t\t\tcontinue\n\t\t\t}\n',
        [GO_FAILS, "FAIL [attempt_zero_baseline] field=backoff.missing"],
    ),
    mutant(
        "go adapter: a case whose input it can build may not be sat out",
        "go/adapter/main.go",
        "\tif hasUnpairedSurrogateEscape(raw) {\n",
        '\tif hasUnpairedSurrogateEscape(raw) || sectionName == "classify" {\n',
        [GO_FAILS, "field=classify.unbuildable"],
    ),
    mutant(
        "go adapter: a value this port cannot hold is sat out, not answered",
        "go/adapter/decode.go",
        "\t\tif err != nil {\n\t\t\t// Wider than int64, which this language has no integer for.\n\t\t\treturn nil, errUnbuildable\n\t\t}\n",
        "\t\tif err != nil {\n\t\t\treturn haltrule.Int(0), nil\n\t\t}\n",
        [GO_FAILS, "FAIL [bigint_far_past_min] field=canonicalize.expect"],
    ),
]

# --- the breaker's argument contract: refusing what the other three parts refuse
CATALOG += [
    mutant(
        "go breaker: the largest integer the breaker takes is 2^53 - 1",
        "go/breaker.go",
        "const wholeMax = 1<<53 - 1",
        "const wholeMax = 1 << 53",
        [GO_FAILS, "FAIL [attempt_one_past_the_shared_range] field=backoff.expect"],
    ),
    mutant(
        "ts adapter: the breaker's refusal is a refusal, not an error the port raised",
        "ts/adapter/adapter.ts",
        "  const result = orRefused(() => classifySystemicDispatchFailure(tc.message as string | null));\n"
        "  return result === REFUSED ? { refused: true } : result;\n",
        "  return classifySystemicDispatchFailure(tc.message as string | null);\n",
        [TS_FAILS, "FAIL [message_is_a_number] field=classify.raised"],
    ),
    mutant(
        "py adapter: a refused report leaves the batch as it was, and the next is still read",
        "py/adapter.py",
        '        if result is _REFUSED:\n            returns.append({"refused": True})\n',
        '        if result is _REFUSED:\n            return {"refused": True}\n',
        [PY_FAILS, "FAIL [report_attempt_count_is_a_string] field=state.expect"],
    ),
    mutant(
        "go adapter: a field the contract does not name is not an argument",
        "go/adapter/main.go",
        '\tif err := only(from, "attempt", "initial_ms", "cap_ms"); err != nil {\n\t\treturn refused, nil\n\t}\n',
        "",
        [GO_FAILS, "FAIL [a_field_the_contract_does_not_name] field=backoff.expect"],
    ),
    mutant(
        "go adapter: a class that is not given is not a class that is null",
        "go/adapter/main.go",
        "\tif len(event.FailureClass) == 0 {\n\t\treturn refused\n\t}\n",
        "",
        [GO_FAILS, "FAIL [report_has_no_failure_class] field=state.expect"],
    ),
]

# --- the rust port: what the language keeps, and what a case keeps instead
CATALOG += [
    mutant(
        "rust purity: the library is written without the standard library in it",
        "rust/haltrule/src/lib.rs",
        "#![no_std]\n",
        "",
        [RS_NO_HOST, "does not say #![no_std]"],
    ),
    mutant(
        "rust purity: the one dependency is the hash, and a second one shows",
        "rust/haltrule/Cargo.toml",
        'sha2 = { version = "0.10", default-features = false }\n',
        'sha2 = { version = "0.10", default-features = false }\nserde_json = "1"\n',
        [RS_NO_HOST, "depends on serde_json"],
    ),
    mutant(
        "rust adapter: every section is run",
        "rust/adapter/src/run.rs",
        "        for raw in &cases {\n",
        '        for raw in &cases {\n            if section_name == "validate" {\n                continue;\n            }\n',
        [RS_FAILS, "field=validate.missing"],
    ),
    mutant(
        "rust adapter: a case whose input it can build may not be sat out",
        "rust/adapter/src/run.rs",
        "    let mut fields: Inputs = match serde_json::from_str(raw.get()) {\n",
        '    if section_name == "classify" {\n        parts.insert("unbuildable".to_string(), Value::Bool(true));\n        writeln!(out, "{}", line_for(&parts)).map_err(|err| err.to_string())?;\n        return Ok(());\n    }\n    let mut fields: Inputs = match serde_json::from_str(raw.get()) {\n',
        [RS_FAILS, "field=classify.unbuildable"],
    ),
    mutant(
        "rust adapter: a value this port cannot hold is sat out, not answered",
        "rust/adapter/src/decode.rs",
        "            // Wider than i64, which this language has no integer for.\n            Err(_) => Err(Refusal::Unbuildable),\n",
        "            Err(_) => Ok(Value::Int(0)),\n",
        [RS_FAILS, "FAIL [bigint_far_past_min] field=canonicalize.expect"],
    ),
]

# --- the fixture grammar: a literal must mean one number, in every port
CATALOG += [
    mutant(
        "driver: a written integer is one the double holds",
        "scripts/conform.py",
        "    if exact == exact.to_integral_value():\n        return held == exact\n",
        "    if exact == exact.to_integral_value():\n        return True\n",
        [
            DRIVER_FAILS,
            "the $number 1e300, an integer written and a different integer read was accepted",
            "the $number 9007199254740993, which a double rounds was accepted",
        ],
        ["a fraction written and an integer read was accepted"],
    ),
    mutant(
        "driver: a written fraction is still a fraction once read",
        "scripts/conform.py",
        "    return held != held.to_integral_value()\n",
        "    return True\n",
        [
            DRIVER_FAILS,
            "the $number 9007199254740991.5, a fraction written and an integer read was accepted",
            "the $number 1e-400, a fraction written and an integer read was accepted",
        ],
        ["an integer written and a different integer read was accepted"],
    ),
    mutant(
        "driver: a plain integer past a double is sent to $bigint, and nothing else is",
        "scripts/conform.py",
        "                    if INTEGER_LITERAL.fullmatch(literal)\n",
        "                    if True\n",
        [
            DRIVER_FAILS,
            "the $number 1e300, an integer written and a different integer read"
            " was refused for another reason",
        ],
    ),
]

# --- go purity: the import list is the whole of what a package can reach, so
# the mistakes people make show up in it and nowhere else.
CATALOG += [
    # The import is used, so the package still builds and the import list is
    # the only thing that says anything is wrong.
    mutant(
        "go purity: the host's clock is not on the allowlist",
        "go/checkpoint.go",
        '\t"unicode/utf8"\n)\n',
        '\t"time"\n\t"unicode/utf8"\n)\n\nvar _ = time.Now\n',
        [GO_NO_HOST, "imports time, which is not on the allowlist"],
    ),
    mutant(
        "go purity: the file system is not on the allowlist",
        "go/slot.go",
        '\t"fmt"\n\t"math"\n\t"strings"\n\t"unicode/utf8"\n)\n',
        '\t"fmt"\n\t"math"\n\t"os"\n\t"strings"\n\t"unicode/utf8"\n)\n\nvar _ = os.Getenv\n',
        [GO_NO_HOST, "imports os, which is not on the allowlist"],
    ),
]

# --- the verdict shape where a part hands it back as a map: the adapters check
# it there, the struct doing that job everywhere else.
CATALOG += [
    mutant(
        "ts adapter: a checkpoint verdict is compared without its message",
        "ts/adapter/adapter.ts",
        "  const { message, ...rest } = result;\n  void message;\n  return rest;\n}\n\nfunction classify",
        "  return result;\n}\n\nfunction classify",
        [TS_FAILS, "FAIL [artifact_missing] field=checkpoint.expect"],
    ),
    mutant(
        "py adapter: a checkpoint verdict is compared without its message",
        "py/adapter.py",
        '    return {key: value for key, value in result.items() if key != "message"}\n\n\ndef _to_plain',
        "    return dict(result)\n\n\ndef _to_plain",
        [PY_FAILS, "FAIL [artifact_missing] field=checkpoint.expect"],
    ),
    mutant(
        "go adapter: a checkpoint verdict is compared without its message",
        "go/adapter/main.go",
        '\tout := haltrule.Map{}\n\tfor key, value := range from {\n\t\tif key != "message" {\n\t\t\tout[key] = value\n\t\t}\n\t}\n\treturn out, nil\n',
        "\treturn from, nil\n",
        [GO_FAILS, "FAIL [artifact_missing] field=checkpoint.expect"],
    ),
    mutant(
        "rust adapter: a checkpoint verdict is compared without its message",
        "rust/adapter/src/sections.rs",
        '    let mut held = from;\n    held.remove("message");\n',
        "    let held = from;\n",
        [RS_FAILS, "FAIL [artifact_missing] field=checkpoint.expect"],
    ),
]

# --- canonicalize and the digest hand back the same verdict. Only one of the
# two is printed, so nothing but the adapters' own comparison can see the
# other; a case cannot make them disagree, and these make them disagree.
CATALOG += [
    mutant(
        "ts: the digest's halt says something canonicalize's does not",
        "ts/checkpoint.ts",
        '  const result = canonicalize(value);\n  if ("verdict" in result) return result;\n',
        '  const result = canonicalize(value);\n  if ("verdict" in result) return { ...result, resume: result.reason };\n',
        [TS_FAILS, "FAIL [fraction] field=canonicalize.raised"],
    ),
    mutant(
        "py: the digest's halt says something canonicalize's does not",
        "py/haltrule/checkpoint.py",
        '    result = canonicalize(value)\n    if "verdict" in result:\n        return result\n',
        '    result = canonicalize(value)\n    if "verdict" in result:\n        return {**result, "verdict": "warning"}\n',
        [PY_FAILS, "FAIL [fraction] field=canonicalize.raised"],
    ),
    mutant(
        "go: the digest's halt says something canonicalize's does not",
        "go/checkpoint.go",
        '\tcanonical, halt := Canonicalize(value)\n\tif halt != nil {\n\t\treturn "", halt\n\t}\n',
        "\tcanonical, halt := Canonicalize(value)\n\tif halt != nil {\n"
        '\t\tother := *halt\n\t\tother.Verdict = Warning\n\t\treturn "", &other\n\t}\n',
        [GO_FAILS, "FAIL [fraction] field=canonicalize.raised"],
    ),
    mutant(
        "rust: the digest's halt says something canonicalize's does not",
        "rust/haltrule/src/checkpoint.rs",
        "    let canonical = canonicalize(value)?;\n",
        "    let canonical = canonicalize(value).map_err(|mut halt| {\n"
        "        halt.verdict = Level::Warning;\n"
        "        halt\n"
        "    })?;\n",
        [RS_FAILS, "FAIL [fraction] field=canonicalize.raised"],
    ),
]
