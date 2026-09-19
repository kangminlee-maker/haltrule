#!/usr/bin/env bash
# Deterministic gates. CI runs this file, and so should you before pushing.
#
# Every gate here answers a question that has one right answer, so a failure is
# a defect and never a judgment call. Gate 2 is the one that matters most: it
# checks the instrument rather than the code, because a conformance runner that
# cannot fail is worth less than no runner at all. Every helper script invoked
# below has its own exit code checked - a heredoc that dies half-done and
# leaves an empty or unchanged file must FAIL the gate it was building, never
# report a vacuous PASS.
set -uo pipefail

cd "$(dirname "$0")/.."
failed=0
skipped=0

pass() { printf '  PASS  %s\n' "$1"; }
# A FAIL line's wording is evidence: scripts/mutants.py credits a mutant only
# when the FAIL lines it names appear. Rewording one fails that suite loudly;
# update the mutant's evidence with it.
fail() { printf '  FAIL  %s\n' "$1"; failed=$((failed + 1)); }
skip() { printf '  SKIP  %s (%s)\n' "$1" "$2"; skipped=$((skipped + 1)); }

FIXTURE_PATH="fixtures/breaker/v0.json"
CHECKPOINT_FIXTURE_PATH="fixtures/checkpoint/v0.json"
BUDGET_FIXTURE_PATH="fixtures/budget/v0.json"
SLOT_FIXTURE_PATH="fixtures/slot/v0.json"

# Every fixture file, found the way both runners find theirs when given no path:
# each .json under fixtures/. Gates count against this inventory, never against
# a runner's own report of what it ran, so a runner that skips a file or a
# section cannot pass by agreeing with itself.
fixture_files=()
while IFS= read -r f; do fixture_files+=("$f"); done < <(find fixtures -type f -name '*.json' | LC_ALL=C sort)

# Prints the number of cases in the inventory: every list under a fixture
# file's top-level keys, one case per entry. Fails on an empty inventory.
fixture_case_count() {
  python3 - "${fixture_files[@]}" <<'PY'
import json, sys
total = sum(
    len(cases)
    for f in sys.argv[1:]
    for cases in json.load(open(f, encoding="utf-8")).values()
    if isinstance(cases, list)
)
if total == 0:
    sys.exit("the fixture inventory holds no cases")
ids = [
    (section, case["id"])
    for f in sys.argv[1:]
    for section, cases in json.load(open(f, encoding="utf-8")).items()
    if isinstance(cases, list)
    for case in cases
]
repeated = sorted({i for i in ids if ids.count(i) > 1})
if repeated:
    # Coverage is matched by section and id, so an id two files share would
    # let a runner that ran one file twice pass for having run both.
    sys.exit("case ids repeat across the fixture inventory: " + ", ".join(f"{s}/{i}" for s, i in repeated[:5]))
print(total)
PY
}

# Succeeds, printing the case count, when the --dump in $1 holds exactly one
# line per inventory case; otherwise prints what is missing, extra, or
# repeated and fails.
dump_covers_inventory() {
  python3 - "$1" "${fixture_files[@]}" <<'PY'
import collections, json, sys
want = collections.Counter()
for f in sys.argv[2:]:
    for section, cases in json.load(open(f, encoding="utf-8")).items():
        if isinstance(cases, list):
            want.update((section, case["id"]) for case in cases)
if not want:
    sys.exit("the fixture inventory holds no cases")
if max(want.values()) > 1:
    sys.exit("case ids repeat across the fixture inventory; coverage cannot be matched by id")
got = collections.Counter()
for line in open(sys.argv[1], encoding="utf-8"):
    row = json.loads(line)
    got[(row["section"], row["id"])] += 1
missing, extra = want - got, got - want
if missing or extra:
    def show(counter):
        return ", ".join(f"{s}/{i}" for s, i in sorted(counter)[:5]) + (" ..." if len(counter) > 5 else "")
    print(f"covers {sum((got & want).values())} of {sum(want.values())} fixture cases;"
          + (f" missing {sum(missing.values())}: {show(missing)}" if missing else "")
          + (f" extra or repeated {sum(extra.values())}: {show(extra)}" if extra else ""))
    sys.exit(1)
print(sum(want.values()))
PY
}

# The policy files: every module under ts/ and py/haltrule/ except the runner,
# hidden files and subdirectories included, so a new part is under gates 4
# and 5 the day it is added, with nothing to remember to list and nothing a
# name can hide. Python's package marker is a policy file too: the sealed run
# loads it, so the gates read it. Gate 4 then refuses any other file under
# py/ or ts/: what the gates cannot read must not be there.
ts_policy_files=()
while IFS= read -r f; do ts_policy_files+=("$f"); done < <(find ts -type f -name '*.ts' ! -path ts/run-fixtures.ts ! -path '*/node_modules/*' | LC_ALL=C sort)
py_policy_files=()
while IFS= read -r f; do py_policy_files+=("$f"); done < <(find py/haltrule -type f -name '*.py' | LC_ALL=C sort)

# The fixture file a corruption target lives in.
fixture_for_target() {
  case "$1" in
    canonicalize_*|checkpoint_*) printf '%s\n' "$CHECKPOINT_FIXTURE_PATH" ;;
    charge_*) printf '%s\n' "$BUDGET_FIXTURE_PATH" ;;
    validate_*) printf '%s\n' "$SLOT_FIXTURE_PATH" ;;
    *) printf '%s\n' "$FIXTURE_PATH" ;;
  esac
}

echo "1. conformance — both implementations against the fixtures"
ts_full=$(node ts/run-fixtures.ts 2>&1)
ts_status=$?
ts_line=$(printf '%s\n' "$ts_full" | tail -1)
py_full=$(python3 py/run_fixtures.py 2>&1)
py_status=$?
py_line=$(printf '%s\n' "$py_full" | tail -1)
inventory_cases=$(fixture_case_count 2>&1)
inventory_status=$?
if [ $inventory_status -ne 0 ] || ! [[ "$inventory_cases" =~ ^[0-9]+$ ]]; then
  fail "could not count the fixture inventory, so no runner count can be checked against it: $inventory_cases"
  inventory_cases=-1
fi
for lang in typescript python; do
  if [ "$lang" = typescript ]; then status=$ts_status line=$ts_line full=$ts_full; else status=$py_status line=$py_line full=$py_full; fi
  ran=$(printf '%s\n' "$line" | sed -nE 's/^OK: ([0-9]+) cases passed.*/\1/p')
  if [ "$status" -ne 0 ]; then
    fail "$lang runner exited $status"
    printf '%s\n' "$full"
  elif [ "$ran" != "$inventory_cases" ]; then
    fail "$lang runner reported ${ran:-no} cases; the fixtures hold $inventory_cases"
  else
    pass "$lang: $line"
  fi
done

echo "2. instrument — a corrupted fixture must make both runners fail"

# Corrupts exactly one expected value in a fresh copy of the fixture and
# writes it to $2. $1 selects which field: every comparison path the runners
# make (classify.expect, backoff.expect_ms, each of the four state.* fields,
# canonicalize.expect in each of its shapes, and checkpoint.expect) needs its
# own case here, because a runner that only ever gets exercised on one field
# (e.g. only ever state.tripped, as this gate used to do) has never had its
# other comparisons checked at all.
corrupt_fixture() {
  python3 - "$1" "$2" "$(fixture_for_target "$1")" <<'PY'
import json, sys

target, out_path, fixture_path = sys.argv[1], sys.argv[2], sys.argv[3]
fixtures = json.load(open(fixture_path))

if target == "classify":
    case = fixtures["classify"][0]
    case["expect"] = None if case["expect"] is not None else "rate_limit"
elif target == "backoff":
    case = fixtures["backoff"][0]
    case["expect_ms"] = case["expect_ms"] + 1
elif target == "state_returns":
    fixtures["state"][0]["expect"]["returns"].append("CORRUPT_MARKER")
elif target == "state_completed":
    fixtures["state"][0]["expect"]["completed"].append("CORRUPT_MARKER")
elif target == "state_dead_letter":
    fixtures["state"][0]["expect"]["dead_letter"].append(
        {
            "item_id": "CORRUPT_MARKER",
            "failure_class": "rate_limit",
            "failure_message": "CORRUPT_MARKER",
            "attempt_count": 999,
        }
    )
elif target == "state_tripped":
    case = fixtures["state"][0]
    case["expect"]["tripped"] = (
        None
        if case["expect"]["tripped"] is not None
        else {"failure_class": "rate_limit", "consecutive_item_count": 999, "threshold": 999}
    )
elif target == "canonicalize_canonical":
    case = next(c for c in fixtures["canonicalize"] if "canonical" in c["expect"])
    case["expect"]["canonical"] += "CORRUPT_MARKER"
elif target == "canonicalize_digest":
    case = next(c for c in fixtures["canonicalize"] if "digest" in c["expect"])
    digest = case["expect"]["digest"]
    case["expect"]["digest"] = digest[:-1] + ("1" if digest[-1] == "0" else "0")
elif target == "canonicalize_halt":
    case = next(c for c in fixtures["canonicalize"] if "halt" in c["expect"])
    case["expect"]["halt"] = (
        "digest_input_int_range" if case["expect"]["halt"] == "digest_input_float" else "digest_input_float"
    )
elif target == "canonicalize_halt_to_value":
    case = next(c for c in fixtures["canonicalize"] if "halt" in c["expect"])
    case["expect"] = {"canonical": "null", "digest": "sha256:" + "0" * 64}
elif target == "checkpoint_issue_field":
    fixtures["checkpoint"][0]["expect"][0]["reason"] += "_CORRUPT_MARKER"
elif target == "checkpoint_extra_issue":
    fixtures["checkpoint"][0]["expect"].append(dict(fixtures["checkpoint"][0]["expect"][0]))
elif target == "checkpoint_missing_issue":
    next(c for c in fixtures["checkpoint"] if len(c["expect"]) > 1)["expect"].pop()
elif target == "charge_verdict":
    fixtures["charge"][0]["expect"]["verdicts"][0]["reason"] += "_CORRUPT_MARKER"
elif target == "charge_missing_verdict":
    next(c for c in fixtures["charge"] if len(c["expect"]["verdicts"]) > 1)["expect"]["verdicts"].pop()
elif target == "charge_used":
    fixtures["charge"][0]["expect"]["used"]["turns"] += "1"
elif target == "validate_verdict":
    case = fixtures["validate"][0]
    case["expect"]["verdict"] = "halt" if case["expect"]["verdict"] != "halt" else "ok"
else:
    sys.exit(f"unknown corruption target: {target}")

json.dump(fixtures, open(out_path, "w"))
PY
}

# Runs both runners against a corrupted copy and requires BOTH a non-zero
# exit AND a "FAIL [...]" mismatch line in the output - a runner that dies
# for an unrelated reason (e.g. a crash) also exits non-zero, and crediting
# that as "caught the corruption" would let a broken runner pass this gate
# for the wrong reason.
corrupt_and_verify() {
  local target="$1" desc="$2"
  local out source
  out=$(mktemp -t haltrule.XXXXXX)
  source=$(fixture_for_target "$target")

  corrupt_fixture "$target" "$out"
  local mutate_status=$?
  if [ $mutate_status -ne 0 ]; then
    fail "$desc: corruption script failed (exit $mutate_status) — cannot verify the instrument"
    rm -f "$out"
    return
  fi
  if cmp -s "$out" "$source"; then
    fail "$desc: corrupted copy is byte-identical to the original fixture — nothing was mutated"
    rm -f "$out"
    return
  fi

  local ts_out ts_status py_out py_status
  ts_out=$(node ts/run-fixtures.ts "$out" 2>&1)
  ts_status=$?
  py_out=$(python3 py/run_fixtures.py "$out" 2>&1)
  py_status=$?

  if [ $ts_status -ne 0 ] && printf '%s' "$ts_out" | grep -q 'FAIL \['; then
    pass "typescript rejects $desc"
  else
    fail "typescript did not report a mismatch for $desc (exit=$ts_status)"
    printf '%s\n' "$ts_out"
  fi
  if [ $py_status -ne 0 ] && printf '%s' "$py_out" | grep -q 'FAIL \['; then
    pass "python rejects $desc"
  else
    fail "python did not report a mismatch for $desc (exit=$py_status)"
    printf '%s\n' "$py_out"
  fi
  rm -f "$out"
}

corrupt_and_verify classify           "a corrupted classify.expect"
corrupt_and_verify backoff            "a corrupted backoff.expect_ms"
corrupt_and_verify state_returns      "a corrupted state.returns"
corrupt_and_verify state_completed    "a corrupted state.completed"
corrupt_and_verify state_dead_letter  "a corrupted state.dead_letter"
corrupt_and_verify state_tripped      "a corrupted state.tripped"
corrupt_and_verify canonicalize_canonical     "a corrupted canonical form"
corrupt_and_verify canonicalize_digest        "a corrupted digest"
corrupt_and_verify canonicalize_halt          "a corrupted halt reason"
corrupt_and_verify canonicalize_halt_to_value "a halt case rewritten as a value"
corrupt_and_verify checkpoint_issue_field     "a corrupted checkpoint issue field"
corrupt_and_verify checkpoint_extra_issue     "an extra checkpoint issue"
corrupt_and_verify checkpoint_missing_issue   "a missing checkpoint issue"
corrupt_and_verify charge_verdict          "a corrupted charge verdict"
corrupt_and_verify charge_missing_verdict  "a missing charge verdict"
corrupt_and_verify charge_used             "a corrupted charge ledger"
corrupt_and_verify validate_verdict        "a corrupted validate verdict"

ts_missing_out=$(node ts/run-fixtures.ts /nonexistent/fixture.json 2>&1)
ts_missing_status=$?
if [ $ts_missing_status -ne 0 ]; then
  pass "typescript runner fails on a missing fixture"
else
  fail "typescript runner survived a missing fixture"
  printf '%s\n' "$ts_missing_out"
fi
py_missing_out=$(python3 py/run_fixtures.py /nonexistent/fixture.json 2>&1)
py_missing_status=$?
if [ $py_missing_status -ne 0 ]; then
  pass "python runner fails on a missing fixture"
else
  fail "python runner survived a missing fixture"
  printf '%s\n' "$py_missing_out"
fi

# Malformed fixture files each runner must refuse outright - with its own
# message, so a runner dying for an unrelated reason does not count. A raw
# JSON number decodes differently in the two languages (1.0, 2^53 + 1); an
# unknown fixture_version must never read as zero cases passed; a section no
# runner reads would pass with every case in it wrong; and a mistyped
# $unsupported kind would otherwise build some value that merely happens to halt.
malformed_fixture() {
  python3 - "$1" "$2" "$CHECKPOINT_FIXTURE_PATH" <<'PY'
import json, sys

kind, out_path, fixture_path = sys.argv[1], sys.argv[2], sys.argv[3]
fixtures = json.load(open(fixture_path))
if kind == "raw_number":
    fixtures["canonicalize"][0]["input"] = 7
elif kind == "unknown_version":
    fixtures["fixture_version"] = "checkpoint/v999"
elif kind == "unknown_kind":
    fixtures["canonicalize"][0]["input"] = {"$unsupported": "no_such_kind"}
elif kind == "wide_number":
    fixtures["canonicalize"][0]["input"] = {"$number": "9007199254740993"}
elif kind == "unknown_section":
    fixtures["canonicalize_extra"] = [
        {"id": "never_read", "input": None, "expect": {"canonical": "WRONG", "digest": "sha256:WRONG"}}
    ]
else:
    sys.exit(f"unknown malformation: {kind}")
json.dump(fixtures, open(out_path, "w"))
PY
}

refuse_and_verify() {
  local kind="$1" expect_text="$2" desc="$3"
  local out
  out=$(mktemp -t haltrule.XXXXXX)
  malformed_fixture "$kind" "$out"
  local malform_status=$?
  if [ $malform_status -ne 0 ] || cmp -s "$out" "$CHECKPOINT_FIXTURE_PATH"; then
    fail "$desc: could not build the malformed fixture (exit $malform_status)"
    rm -f "$out"
    return
  fi
  local ts_out ts_status py_out py_status
  ts_out=$(node ts/run-fixtures.ts "$out" 2>&1)
  ts_status=$?
  py_out=$(python3 py/run_fixtures.py "$out" 2>&1)
  py_status=$?
  if [ $ts_status -ne 0 ] && printf '%s' "$ts_out" | grep -qF "$expect_text"; then
    pass "typescript refuses $desc"
  else
    fail "typescript did not refuse $desc (exit=$ts_status)"
    printf '%s\n' "$ts_out" | tail -5
  fi
  if [ $py_status -ne 0 ] && printf '%s' "$py_out" | grep -qF "$expect_text"; then
    pass "python refuses $desc"
  else
    fail "python did not refuse $desc (exit=$py_status)"
    printf '%s\n' "$py_out" | tail -5
  fi
  rm -f "$out"
}

refuse_and_verify raw_number      "raw JSON number"          "a raw number in a fixture input"
refuse_and_verify unknown_version "unknown fixture_version"  "an unknown fixture_version"
refuse_and_verify unknown_section "unknown fixture section"  "a fixture section no runner reads"
refuse_and_verify unknown_kind    "unknown \$unsupported kind" "an \$unsupported kind no runner builds"
refuse_and_verify wide_number     "not exactly representable as a double" "a \$number integer literal a double cannot hold"

# Out-of-contract inputs the parts refuse, per the budget and slot bullets of
# the spec. A raise fails a fixture, so no fixture can carry these promises;
# each is called directly, in both languages, and must raise.
OUT_OF_CONTRACT_PROBES=8
OUT_OF_CONTRACT_LIST="slot bounds past 2^53 - 1 or negative, an unknown kind, a choice without candidates, min above max; budget amounts negative or fractional, a cap past 2^63 - 1"
ts_probe_out=$(node --input-type=module -e '
import { validateSlot } from "./ts/slot.ts";
import { Budget } from "./ts/budget.ts";
const probes = [
  ["slot bound past 2^53 - 1", () => validateSlot({ name: "t", kind: "text", max_length: 9007199254740992 }, "x")],
  ["slot negative bound", () => validateSlot({ name: "t", kind: "text", min_length: -1 }, "x")],
  ["slot unknown kind", () => validateSlot({ name: "t", kind: "number" }, "x")],
  ["slot choice without candidates", () => validateSlot({ name: "t", kind: "choice" }, "x")],
  ["slot min_length above max_length", () => validateSlot({ name: "t", kind: "text", min_length: 3, max_length: 2 }, "x")],
  ["budget negative charge", () => new Budget().charge({ turns: -1 })],
  ["budget fractional charge", () => new Budget().charge({ ms: 1.5 })],
  ["budget cap past 2^63 - 1", () => new Budget({ token_budget: 9223372036854775808n })],
];
for (const [label, probe] of probes) {
  try {
    probe();
    console.log(`accepted: ${label}`);
  } catch (error) {
    if (!(error instanceof TypeError || error instanceof RangeError)) throw error;
    console.log(`refused: ${label}`);
  }
}' 2>&1)
ts_probe_status=$?
ts_probe_refused=$(printf '%s\n' "$ts_probe_out" | grep -c '^refused: ')
if [ $ts_probe_status -ne 0 ]; then
  fail "typescript out-of-contract probe crashed (exit $ts_probe_status)"
  printf '%s\n' "$ts_probe_out" | tail -5
elif printf '%s\n' "$ts_probe_out" | grep -q '^accepted: '; then
  fail "typescript accepted an out-of-contract input: $(printf '%s\n' "$ts_probe_out" | sed -n 's/^accepted: //p' | head -1)"
elif [ "$ts_probe_refused" -ne "$OUT_OF_CONTRACT_PROBES" ]; then
  fail "typescript out-of-contract probe ran $ts_probe_refused of $OUT_OF_CONTRACT_PROBES probes"
else
  pass "typescript refuses $OUT_OF_CONTRACT_PROBES out-of-contract inputs: $OUT_OF_CONTRACT_LIST"
fi

py_probe_out=$(python3 - <<'PY' 2>&1
import sys

sys.path.insert(0, "py")
from haltrule.budget import Budget  # noqa: E402
from haltrule.slot import validate_slot  # noqa: E402

probes = [
    ("slot bound past 2^53 - 1", lambda: validate_slot({"name": "t", "kind": "text", "max_length": 2**53}, "x")),
    ("slot negative bound", lambda: validate_slot({"name": "t", "kind": "text", "min_length": -1}, "x")),
    ("slot unknown kind", lambda: validate_slot({"name": "t", "kind": "number"}, "x")),
    ("slot choice without candidates", lambda: validate_slot({"name": "t", "kind": "choice"}, "x")),
    ("slot min_length above max_length", lambda: validate_slot({"name": "t", "kind": "text", "min_length": 3, "max_length": 2}, "x")),
    ("budget negative charge", lambda: Budget().charge(turns=-1)),
    ("budget fractional charge", lambda: Budget().charge(ms=1.5)),
    ("budget cap past 2^63 - 1", lambda: Budget(token_budget=2**63)),
]
for label, probe in probes:
    try:
        probe()
        print(f"accepted: {label}")
    except (TypeError, ValueError):
        print(f"refused: {label}")
PY
)
py_probe_status=$?
py_probe_refused=$(printf '%s\n' "$py_probe_out" | grep -c '^refused: ')
if [ $py_probe_status -ne 0 ]; then
  fail "python out-of-contract probe crashed (exit $py_probe_status)"
  printf '%s\n' "$py_probe_out" | tail -5
elif printf '%s\n' "$py_probe_out" | grep -q '^accepted: '; then
  fail "python accepted an out-of-contract input: $(printf '%s\n' "$py_probe_out" | sed -n 's/^accepted: //p' | head -1)"
elif [ "$py_probe_refused" -ne "$OUT_OF_CONTRACT_PROBES" ]; then
  fail "python out-of-contract probe ran $py_probe_refused of $OUT_OF_CONTRACT_PROBES probes"
else
  pass "python refuses $OUT_OF_CONTRACT_PROBES out-of-contract inputs: $OUT_OF_CONTRACT_LIST"
fi

echo "3. parity — the two runners' actual output matches byte for byte"
# --dump prints each case's ACTUAL result (never the fixture's expectation)
# as one canonical JSON line. This is the check the README and spec promise
# ("CI compares their output byte for byte") - the two summary lines from
# gate 1 being equal only proves the case counts and pass/fail verdicts
# match, not that the two implementations computed the same values.
ts_dump_file=$(mktemp -t haltrule.XXXXXX)
py_dump_file=$(mktemp -t haltrule.XXXXXX)
node ts/run-fixtures.ts --dump > "$ts_dump_file" 2>&1
ts_dump_status=$?
python3 py/run_fixtures.py --dump > "$py_dump_file" 2>&1
py_dump_status=$?
if [ $ts_dump_status -ne 0 ] || [ $py_dump_status -ne 0 ]; then
  fail "--dump failed: typescript exit=$ts_dump_status python exit=$py_dump_status"
  echo "--- typescript --dump output ---"; cat "$ts_dump_file"
  echo "--- python --dump output ---"; cat "$py_dump_file"
elif cmp -s "$ts_dump_file" "$py_dump_file"; then
  # Identical is not enough: two dumps that skip the same section agree.
  if covered=$(dump_covers_inventory "$ts_dump_file" 2>&1); then
    pass "identical dump covering all $covered fixture cases"
  else
    fail "both --dump outputs are identical but not complete: $covered"
  fi
else
  fail "runner output diverges — dumps are not byte-identical"
  diff "$py_dump_file" "$ts_dump_file" | head -20
fi
rm -f "$ts_dump_file" "$py_dump_file"

echo "4. purity — the policy files import nothing they should not"
# Broadened past a bare '^import': a dynamic `await import(...)`, a
# CommonJS `require(...)`, and a `export ... from "./somewhere"` re-export
# are all dependencies too, and none of them start a line with "import".
# ts/checkpoint.ts may take SHA-256 from node:crypto and nothing else; the
# spec allows SHA-256 as the one dependency where it is not otherwise at hand.
TS_CRYPTO_IMPORT='^[0-9]+:import \{ createHash \} from "node:crypto";$'
# Every part but verdict.ts may import the Verdict shape from ./verdict.ts.
TS_VERDICT_IMPORT='^[0-9]+:import \{[^}]*\} from "\./verdict\.ts";$'
if [ "${#ts_policy_files[@]}" -lt 5 ] || [ "${#py_policy_files[@]}" -lt 5 ]; then
  fail "found ${#ts_policy_files[@]} TypeScript and ${#py_policy_files[@]} Python policy files; expected at least verdict, breaker, checkpoint, budget, slot"
fi
for ts_file in "${ts_policy_files[@]}"; do
  ts_import_hits=$(grep -nE '\bimport\b|\brequire[[:space:]]*\(|^[[:space:]]*export[[:space:]].*\bfrom\b' "$ts_file" || true)
  case "$ts_file" in
    ts/verdict.ts)
      allowed="its own file" ;;
    ts/checkpoint.ts)
      ts_import_hits=$(printf '%s\n' "$ts_import_hits" | grep -vE "$TS_CRYPTO_IMPORT|$TS_VERDICT_IMPORT" || true)
      allowed="node:crypto's createHash and ./verdict.ts" ;;
    *)
      ts_import_hits=$(printf '%s\n' "$ts_import_hits" | grep -vE "$TS_VERDICT_IMPORT" || true)
      allowed="./verdict.ts" ;;
  esac
  if [ -z "$ts_import_hits" ]; then
    pass "$ts_file: no import beyond $allowed"
  else
    fail "$ts_file has import(s)/require(s)/re-export(s) beyond $allowed"
    printf '%s\n' "$ts_import_hits"
  fi
done

for py_file in "${py_policy_files[@]}"; do
py_purity_out=$(python3 - "$py_file" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
modules = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        modules += [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        modules.append(node.module)
allowed = set(sys.stdlib_module_names) | {"__future__", "haltrule"}
print(",".join(m for m in modules if m.split(".")[0] not in allowed))
PY
)
py_purity_status=$?
if [ $py_purity_status -ne 0 ]; then
  fail "$py_file purity check crashed (exit $py_purity_status) — cannot verify import purity"
  printf '%s\n' "$py_purity_out"
elif [ -z "$py_purity_out" ]; then
  pass "$py_file imports only the standard library and haltrule"
else
  fail "$py_file imports third-party modules: $py_purity_out"
fi
done

# Nothing else lives under the policy directories. A file the gates cannot
# read - a compiled module, a sibling that shadows a standard-library name on
# the runners' path, a symlink - is a failure, not an invisible module. The
# bytecode caches are the one exception: Python never imports one without
# the source it was compiled from, and the source is in the inventory.
known_files=" py/run_fixtures.py ts/run-fixtures.ts ts/package.json ts/tsconfig.json ${ts_policy_files[*]} ${py_policy_files[*]} "
unknown_files=()
while IFS= read -r f; do
  case "$known_files" in
    *" $f "*) ;;
    *) unknown_files+=("$f") ;;
  esac
done < <(find py ts -mindepth 1 \( -type f -o -type l \) ! -path '*/__pycache__/*' ! -path '*/node_modules/*' | LC_ALL=C sort)
if [ "${#unknown_files[@]}" -eq 0 ]; then
  pass "py/ and ts/ hold only the runners, the policy files, and ts/package.json, ts/tsconfig.json"
else
  # An array, not a pipe into a loop: a subshell's fail would not count.
  for f in "${unknown_files[@]}"; do
    fail "$f is under the policy directories but the gates cannot read it: neither a runner, a policy file, nor a known config"
  done
fi

# The runners execute nothing but that inventory: a policy imported from
# anywhere else - a file the discovery above did not list - would run with
# gates 4 and 5 never having read it. The FAIL line carries the first hit,
# so each branch of the scan has evidence only it can produce.
ts_runner_out=$(python3 - "${ts_policy_files[@]}" <<'PY'
import os, re, sys
inventory = set(sys.argv[1:])
source = open("ts/run-fixtures.ts", encoding="utf-8").read()
hits = []
for match in re.finditer(r"(?:\bfrom|^\s*import)\s+[\x22\x27]([^\x22\x27]+)[\x22\x27]", source, re.MULTILINE):
    spec = match.group(1)
    if spec.startswith("node:"):
        continue
    if spec.startswith("./") or spec.startswith("../"):
        resolved = os.path.normpath(os.path.join("ts", spec))
        if resolved in inventory:
            continue
        hits.append(f"{spec} -> {resolved}, not in the policy inventory")
    else:
        hits.append(f"{spec}: neither a node: builtin nor a relative path")
if re.search(r"\bimport\s*\(|\brequire\s*\(", source):
    hits.append("a dynamic import() or require()")
print("\n".join(hits))
PY
)
ts_runner_status=$?
if [ $ts_runner_status -ne 0 ]; then
  fail "ts/run-fixtures.ts import check crashed (exit $ts_runner_status)"
  printf '%s\n' "$ts_runner_out"
elif [ -z "$ts_runner_out" ]; then
  pass "ts/run-fixtures.ts imports only node: builtins and files in the policy inventory"
else
  fail "ts/run-fixtures.ts imports outside the policy inventory: $(printf '%s\n' "$ts_runner_out" | head -1)"
  printf '%s\n' "$ts_runner_out" | tail -n +2
fi

py_runner_out=$(python3 - "${py_policy_files[@]}" <<'PY'
import ast, sys
inventory = set(sys.argv[1:])
tree = ast.parse(open("py/run_fixtures.py", encoding="utf-8").read())
hits = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            hits.append(f"line {node.lineno}: a relative import")
            continue
        modules = [node.module or ""]
    else:
        continue
    for module in modules:
        top = module.split(".")[0]
        if top == "haltrule":
            file = "py/" + module.replace(".", "/") + ".py"
            if module == "haltrule" or file in inventory:
                continue
            hits.append(f"line {node.lineno}: {module} -> {file}, not in the policy inventory")
        elif top not in sys.stdlib_module_names:
            hits.append(f"line {node.lineno}: {module}: not in the standard library")
        elif top == "importlib":
            hits.append(f"line {node.lineno}: importlib loads modules the inventory cannot name")
print("\n".join(hits))
PY
)
py_runner_status=$?
if [ $py_runner_status -ne 0 ]; then
  fail "py/run_fixtures.py import check crashed (exit $py_runner_status)"
  printf '%s\n' "$py_runner_out"
elif [ -z "$py_runner_out" ]; then
  pass "py/run_fixtures.py imports only the standard library and modules in the policy inventory"
else
  fail "py/run_fixtures.py imports outside the standard library and the policy inventory: $(printf '%s\n' "$py_runner_out" | head -1)"
  printf '%s\n' "$py_runner_out" | tail -n +2
fi

echo "5. determinism — no clock, timer, randomness, or I/O in the policy files"
# The spec requires these to stay pure (no I/O, no clock, no sleep). Checked
# only against the policy files, never the runners, which legitimately read
# argv and the fixture file. Two layers, because each has a blind spot the
# other covers: the static scan reads every line but only sees names, so an
# alias or a string-built call slips past it; the sealed run sees through any
# alias but only on the paths the fixtures execute. Each PASS line says which
# of the two it is.

# 5a. TypeScript, static: parsed with the TypeScript compiler, so words inside
# strings and comments never count and only real references do. Math is
# allowed only through its pure members.
# node reads a module only by its extension, so the temp file gets .mjs.
ts_static_js=$(mktemp -t haltrule.XXXXXX)
mv "$ts_static_js" "$ts_static_js.mjs"
ts_static_js="$ts_static_js.mjs"
cat > "$ts_static_js" <<'JS'
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
let ts;
try {
  ts = createRequire(`${process.cwd()}/`)("typescript");
} catch {
  console.log("SKIP");
  process.exit(0);
}
const FORBIDDEN = new Set([
  "Date", "performance", "setTimeout", "setInterval", "setImmediate", "queueMicrotask",
  "process", "fetch", "XMLHttpRequest", "WebSocket", "crypto", "Intl", "console",
  "globalThis", "global", "window", "self", "require", "eval", "Function", "Deno", "Bun",
]);
const PURE_MATH = new Set([
  "abs", "floor", "ceil", "round", "trunc", "sign", "max", "min", "pow", "sqrt", "cbrt",
  "hypot", "log", "log2", "log10", "exp", "imul", "clz32", "fround",
  "PI", "E", "LN2", "LN10", "LOG2E", "LOG10E", "SQRT2", "SQRT1_2",
]);
const file = process.argv[2];
const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const line = (node) => source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
// A name in one of these slots is a property or a declared name, not a
// reference to a global: `x.Date`, `{ Date: 1 }`, `import { x }`.
function isNameSlot(id) {
  const p = id.parent;
  if (ts.isPropertyAccessExpression(p) || ts.isQualifiedName(p)) return (p.name ?? p.right) === id;
  if (ts.isBindingElement(p)) return p.propertyName === id;
  if (ts.isImportSpecifier(p) || ts.isExportSpecifier(p)) return true;
  return (
    (ts.isPropertyAssignment(p) || ts.isPropertyDeclaration(p) || ts.isPropertySignature(p) ||
      ts.isMethodDeclaration(p) || ts.isMethodSignature(p) || ts.isGetAccessor(p) ||
      ts.isSetAccessor(p) || ts.isEnumMember(p)) && p.name === id
  );
}
const hits = [];
let identifiers = 0;
function visit(node) {
  if (ts.isImportDeclaration(node)) return; // gate 4 owns imports
  if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
    hits.push(`line ${line(node)}: dynamic import`);
  }
  if (ts.isIdentifier(node)) {
    identifiers += 1;
    if (!isNameSlot(node)) {
      if (FORBIDDEN.has(node.text)) hits.push(`line ${line(node)}: ${node.text}`);
      if (node.text === "Math") {
        const p = node.parent;
        if (!(ts.isPropertyAccessExpression(p) && p.expression === node && PURE_MATH.has(p.name.text))) {
          hits.push(`line ${line(node)}: Math used other than through a pure member`);
        }
      }
    }
  }
  ts.forEachChild(node, visit);
}
visit(source);
// A parse that saw nothing would report a clean file.
if (identifiers === 0) hits.push("no identifiers parsed at all");
console.log(hits.join("\n"));
JS
for ts_file in "${ts_policy_files[@]}"; do
  ts_static_out=$(node "$ts_static_js" "$ts_file" 2>&1)
  ts_static_status=$?
  if [ $ts_static_status -ne 0 ]; then
    fail "$ts_file static scan crashed (exit $ts_static_status)"
    printf '%s\n' "$ts_static_out"
  elif [ "$ts_static_out" = "SKIP" ]; then
    skip "$ts_file static scan" "typescript not installed locally; CI installs it"
  elif [ -z "$ts_static_out" ]; then
    pass "$ts_file (static) references no clock, timer, randomness, process, network, console, or eval global; Math only through pure members"
  else
    fail "$ts_file (static) references a clock, timer, randomness, or I/O global"
    printf '%s\n' "$ts_static_out"
  fi
done
rm -f "$ts_static_js"

# 5b. Python, static: an import allowlist, and no reference at all - called or
# not - to a builtin that does I/O, evaluates code, or reaches the builtins.
for py_file in "${py_policy_files[@]}"; do
py_clock_out=$(python3 - "$py_file" <<'PY'
import ast, sys

ALLOWED_MODULES = {"__future__", "dataclasses", "hashlib", "math", "typing", "haltrule"}
FORBIDDEN_NAMES = {
    "open", "print", "input", "exec", "eval", "compile", "__import__", "breakpoint",
    "__builtins__", "getattr", "setattr", "delattr", "globals", "vars", "locals",
    # process-random: string hashing is seeded per process; id() is an address,
    # and so is the default repr() of an object
    "hash", "id", "repr",
}
FORBIDDEN_ATTRIBUTES = {"__builtins__", "__globals__", "__subclasses__", "__import__", "__loader__"}
tree = ast.parse(open(sys.argv[1]).read())
hits = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] not in ALLOWED_MODULES:
                hits.append(f"line {node.lineno}: import {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        if (node.module or "").split(".")[0] not in ALLOWED_MODULES or node.level:
            hits.append(f"line {node.lineno}: from {'.' * node.level}{node.module or ''} import ...")
    elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
        hits.append(f"line {node.lineno}: {node.id}")
    elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
        hits.append(f"line {node.lineno}: .{node.attr}")
print("\n".join(hits))
PY
)
py_clock_status=$?
if [ $py_clock_status -ne 0 ]; then
  fail "$py_file static check crashed (exit $py_clock_status)"
  printf '%s\n' "$py_clock_out"
elif [ -z "$py_clock_out" ]; then
  pass "$py_file (static) imports only __future__/dataclasses/hashlib/math/typing/haltrule and names no I/O, eval, process-random, or builtins-reaching builtin"
else
  fail "$py_file (static) imports outside the allowlist or names an I/O, eval, process-random, or builtins-reaching builtin"
  printf '%s\n' "$py_clock_out"
fi
done

# 5c/5d. Sealed runs: every fixture again, with the policy code unable to read
# a clock, draw randomness, or do I/O - reached by any name. The seal checks
# itself inside the very process that runs the fixtures and prints a marker
# only when it holds, so a seal that failed to install, or a run that lost it,
# cannot pass as a clean run. The question here is only whether sealing
# changes the outcome - the same exit status and summary as gate 1's unsealed
# run - so a conformance failure is reported once, by gate 1, not again here.
SEAL_MARKER="haltrule-seal: installed"

ts_seal=$(mktemp -t haltrule.XXXXXX)
mv "$ts_seal" "$ts_seal.mjs"
ts_seal="$ts_seal.mjs"
cat > "$ts_seal" <<'JS'
import { writeSync } from "node:fs";
// Each refusal is recorded on stderr as it happens, before it throws: policy
// code that catches the throw must not erase the evidence. The self-check's
// own probes run before recording starts.
let recording = false;
const refuse = (what) => function sealed() {
  if (recording) writeSync(2, `haltrule-seal: policy code used ${what}\n`);
  throw new Error(`sealed: policy code used ${what}`);
};
const SealedDate = new Proxy(Date, {
  apply: refuse("Date()"),
  construct: refuse("new Date()"),
  get: (target, key) => (key === "now" ? refuse("Date.now") : Reflect.get(target, key)),
});
globalThis.Date = SealedDate;
globalThis.performance = { now: refuse("performance.now") };
Math.random = refuse("Math.random");
for (const name of ["setTimeout", "setInterval", "setImmediate", "queueMicrotask", "fetch"]) {
  globalThis[name] = refuse(name);
}
Object.defineProperty(globalThis, "crypto", {
  value: { getRandomValues: refuse("crypto.getRandomValues"), randomUUID: refuse("crypto.randomUUID") },
});
process.hrtime = Object.assign(refuse("process.hrtime"), { bigint: refuse("process.hrtime.bigint") });
process.uptime = refuse("process.uptime");
// Self-check in this process: every probe must throw before the marker prints.
const probes = [() => Date.now(), () => new Date(), () => Math.random(), () => performance.now(), () => setTimeout(() => {}, 0)];
if (probes.every((probe) => { try { probe(); return false; } catch { return true; } })) {
  recording = true;
  writeSync(2, "haltrule-seal: installed\n");
}
JS
ts_sealed_out=$(node --import "$ts_seal" ts/run-fixtures.ts 2>&1)
ts_sealed_status=$?
ts_sealed_line=$(printf '%s\n' "$ts_sealed_out" | tail -1)
ts_refused=$(printf '%s\n' "$ts_sealed_out" | sed -n 's/^haltrule-seal: policy code /&/p' | head -1)
if [ -n "$ts_refused" ]; then
  fail "typescript policy code reached a refused API under the seal: $ts_refused"
elif ! printf '%s\n' "$ts_sealed_out" | grep -qxF "$SEAL_MARKER"; then
  fail "typescript sealed run carries no seal marker — the seal did not install or did not hold"
  printf '%s\n' "$ts_sealed_out" | tail -3
elif [ $ts_sealed_status -ne $ts_status ] || [ "$ts_sealed_line" != "$ts_line" ]; then
  fail "typescript: sealing changed the outcome under the seal: exit=$ts_sealed_status '$ts_sealed_line' vs unsealed exit=$ts_status '$ts_line'"
  printf '%s\n' "$ts_sealed_out" | grep -m3 'sealed:' || printf '%s\n' "$ts_sealed_out" | tail -3
else
  pass "typescript (sealed run) same outcome as unsealed with Date, performance, Math.random, timers, fetch, Web Crypto randomness, and process clocks refusing"
fi
rm -f "$ts_seal"

py_seal=$(mktemp -t haltrule.XXXXXX)
cat > "$py_seal" <<'PY'
import builtins
import importlib.util
import os
import pathlib
import runpy
import sys

ALLOWED_MODULES = {"__future__", "dataclasses", "hashlib", "math", "typing", "haltrule"}
REFUSED = ("open", "print", "input", "exec", "eval", "compile", "breakpoint", "hash", "id", "repr")


# Each refusal is recorded on stderr as it happens, before it raises: policy
# code that catches the error must not erase the evidence. Recording covers
# module loading too; only the self-check's own probe is not recorded.
recording = True


def record(line):
    if recording:
        os.write(2, f"haltrule-seal: policy code {line}\n".encode())


def refuse(what):
    def sealed(*args, **kwargs):
        record(f"used {what}")
        raise RuntimeError(f"sealed: policy code used {what}")

    return sealed


real_import = builtins.__import__


def sealed_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED_MODULES:
        record(f"imported {name!r}")
        raise ImportError(f"sealed: policy code imported {name!r}")
    # A sibling the seal has not loaded yet would load outside it.
    if name.startswith("haltrule.") and name not in sys.modules:
        record(f"imported {name!r} before the seal loaded it")
        raise ImportError(f"sealed: policy code imported {name!r} before the seal loaded it")
    return real_import(name, globals, locals, fromlist, level)


# The policy modules get their own builtins; the runner keeps the real ones.
SEALED = dict(vars(builtins), __import__=sealed_import, **{n: refuse(n) for n in REFUSED})

sys.path.insert(0, "py")

# The package marker first - code there would otherwise run outside the seal -
# then every policy module, verdict first: the others import it.
parts = ["verdict"] + sorted(
    path.stem for path in pathlib.Path("py/haltrule").glob("*.py") if path.stem not in ("__init__", "verdict")
)
modules = [("haltrule", "py/haltrule/__init__.py")] + [(f"haltrule.{part}", f"py/haltrule/{part}.py") for part in parts]
sealed_modules = {}
for name, file in modules:
    spec = importlib.util.spec_from_file_location(
        name, file, submodule_search_locations=["py/haltrule"] if name == "haltrule" else None
    )
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__builtins__"] = SEALED
    sys.modules[name] = module
    if name != "haltrule":
        setattr(sys.modules["haltrule"], name.rpartition(".")[2], module)
    spec.loader.exec_module(module)
    sealed_modules[name] = module
    # Self-check in the module's own namespace: `open` there must refuse.
    recording = False
    try:
        eval("open('/dev/null')", dict(module.__dict__))
    except RuntimeError:
        pass
    else:
        sys.exit(f"{name} loaded without the seal")
    recording = True

print("haltrule-seal: installed", file=sys.stderr)
sys.argv = ["py/run_fixtures.py"]
try:
    runpy.run_path("py/run_fixtures.py", run_name="__main__")
finally:
    # Every policy module the run used - a haltrule name, or any module whose
    # file lies inside this tree, whatever it is called - must be the very
    # object the seal loaded, not one the runtime loaded on its own; what a
    # module says about its own builtins is not consulted.
    root = os.path.realpath(os.getcwd()) + os.sep
    for loaded_name, loaded in list(sys.modules.items()):
        file = getattr(loaded, "__file__", None) or ""
        inside = bool(file) and os.path.realpath(file).startswith(root)
        if loaded_name == "haltrule" or loaded_name.startswith("haltrule.") or inside:
            if sealed_modules.get(loaded_name) is loaded:
                continue
            record(f"module {loaded_name} loaded outside the seal")
PY
py_sealed_out=$(python3 "$py_seal" 2>&1)
py_sealed_status=$?
py_sealed_line=$(printf '%s\n' "$py_sealed_out" | tail -1)
py_refused=$(printf '%s\n' "$py_sealed_out" | sed -n 's/^haltrule-seal: policy code /&/p' | head -1)
if [ -n "$py_refused" ]; then
  fail "python policy code reached a refused API under the seal: $py_refused"
elif ! printf '%s\n' "$py_sealed_out" | grep -qxF "$SEAL_MARKER"; then
  fail "python sealed run carries no seal marker — the seal did not install or did not hold"
  printf '%s\n' "$py_sealed_out" | tail -3
elif [ $py_sealed_status -ne $py_status ] || [ "$py_sealed_line" != "$py_line" ]; then
  fail "python: sealing changed the outcome under the seal: exit=$py_sealed_status '$py_sealed_line' vs unsealed exit=$py_status '$py_line'"
  printf '%s\n' "$py_sealed_out" | grep -m3 'sealed:' || printf '%s\n' "$py_sealed_out" | tail -3
else
  pass "python (sealed run) same outcome as unsealed with open/print/input/exec/eval/compile/hash/id/repr refusing and imports held to the allowlist"
fi
rm -f "$py_seal"

# 5e. Python, behavioural: string hashing is seeded per process, so iterating
# a set, or anything else ordered by hash, can reorder output from one run to
# the next without naming a single forbidden builtin. The --dump must come out
# byte-identical under several fixed seeds.
seed_dir=$(mktemp -d -t haltrule.XXXXXX)
seed_bad=""
for seed in 0 1 2 3 4; do
  if ! PYTHONHASHSEED=$seed python3 py/run_fixtures.py --dump > "$seed_dir/$seed" 2>&1; then
    seed_bad="$seed_bad $seed(exit)"
  elif [ "$seed" != 0 ] && ! cmp -s "$seed_dir/0" "$seed_dir/$seed"; then
    seed_bad="$seed_bad $seed"
  fi
done
if [ -n "$seed_bad" ]; then
  fail "python --dump differs by hash seed (seeds:$seed_bad vs seed 0)"
  diff "$seed_dir/0" "$seed_dir/$(printf '%s' "$seed_bad" | awk '{print $1}' | tr -dc 0-9)" 2>/dev/null | head -6
elif ! covered=$(dump_covers_inventory "$seed_dir/0" 2>&1); then
  fail "python --dump under PYTHONHASHSEED=0 is not complete: $covered"
else
  pass "python --dump identical under PYTHONHASHSEED 0-4, covering all $covered fixture cases"
fi
rm -rf "$seed_dir"

echo "6. fixture coverage — enough cases to exercise the contract"
# A gate that only checks pass/fail wiring, never coverage, still passes
# with every section emptied out. Minimums read from the fixture file
# itself, never hardcoded, so this stays correct as more cases are added.
fixture_counts=$(python3 - "$FIXTURE_PATH" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["classify"]), len(d["backoff"]), len(d["state"]))
PY
)
fixture_counts_status=$?
if [ $fixture_counts_status -ne 0 ] || [ -z "$fixture_counts" ]; then
  fail "could not read fixture case counts from $FIXTURE_PATH (exit $fixture_counts_status)"
else
  read -r classify_n backoff_n state_n <<< "$fixture_counts"
  if [ "$classify_n" -ge 34 ]; then pass "classify: $classify_n cases (>= 34)"; else fail "classify: only $classify_n cases (need >= 34)"; fi
  if [ "$backoff_n" -ge 10 ]; then pass "backoff: $backoff_n cases (>= 10)"; else fail "backoff: only $backoff_n cases (need >= 10)"; fi
  if [ "$state_n" -ge 16 ]; then pass "state: $state_n cases (>= 16)"; else fail "state: only $state_n cases (need >= 16)"; fi
fi
checkpoint_counts=$(python3 - "$CHECKPOINT_FIXTURE_PATH" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["canonicalize"]), len(d["checkpoint"]))
PY
)
checkpoint_counts_status=$?
if [ $checkpoint_counts_status -ne 0 ] || [ -z "$checkpoint_counts" ]; then
  fail "could not read fixture case counts from $CHECKPOINT_FIXTURE_PATH (exit $checkpoint_counts_status)"
else
  read -r canonicalize_n checkpoint_n <<< "$checkpoint_counts"
  if [ "$canonicalize_n" -ge 30 ]; then pass "canonicalize: $canonicalize_n cases (>= 30)"; else fail "canonicalize: only $canonicalize_n cases (need >= 30)"; fi
  if [ "$checkpoint_n" -ge 20 ]; then pass "checkpoint: $checkpoint_n cases (>= 20)"; else fail "checkpoint: only $checkpoint_n cases (need >= 20)"; fi
fi

part_counts=$(python3 - "$BUDGET_FIXTURE_PATH" "$SLOT_FIXTURE_PATH" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1]))["charge"]), len(json.load(open(sys.argv[2]))["validate"]))
PY
)
part_counts_status=$?
if [ $part_counts_status -ne 0 ] || [ -z "$part_counts" ]; then
  fail "could not read fixture case counts from $BUDGET_FIXTURE_PATH and $SLOT_FIXTURE_PATH (exit $part_counts_status)"
else
  read -r charge_n validate_n <<< "$part_counts"
  if [ "$charge_n" -ge 30 ]; then pass "charge: $charge_n cases (>= 30)"; else fail "charge: only $charge_n cases (need >= 30)"; fi
  if [ "$validate_n" -ge 40 ]; then pass "validate: $validate_n cases (>= 40)"; else fail "validate: only $validate_n cases (need >= 40)"; fi
fi

# fixtures/README.md promises ASCII files, so no editor or transport can
# normalize a test value away (an NFD case silently becoming NFC).
if [ "${#fixture_files[@]}" -lt 2 ]; then
  fail "found ${#fixture_files[@]} fixture files; expected at least breaker and checkpoint"
else
  non_ascii=$(python3 - "${fixture_files[@]}" <<'PY'
import sys
print(" ".join(p for p in sys.argv[1:] if any(b > 0x7F for b in open(p, "rb").read())))
PY
)
  non_ascii_status=$?
  if [ $non_ascii_status -ne 0 ]; then
    fail "fixture ASCII check crashed (exit $non_ascii_status)"
  elif [ -z "$non_ascii" ]; then
    pass "all ${#fixture_files[@]} fixture files are ASCII"
  else
    fail "fixture files with non-ASCII bytes: $non_ascii"
  fi
fi

echo "7. digest — an independent sha256 over each canonical form gives its digest"
# The spec promises that anything able to run sha256sum computes the same
# digest from the canonical form. Checked with the system's own tool over the
# implementations' actual output (gate 3 proved both identical), never with
# the hashing either implementation uses.
if command -v sha256sum >/dev/null 2>&1; then
  sha256_tool="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  sha256_tool="shasum -a 256"
else
  sha256_tool=""
fi
digest_dir=$(mktemp -d -t haltrule.XXXXXX)
if [ -z "$sha256_tool" ]; then
  fail "neither sha256sum nor shasum is on PATH — cannot check digests independently"
elif ! node ts/run-fixtures.ts --dump > "$digest_dir/dump" 2>&1; then
  fail "typescript --dump failed; cannot check digests"
  tail -5 "$digest_dir/dump"
else
  digest_split=$(python3 - "$digest_dir" "${fixture_files[@]}" <<'PY'
import json, sys

out_dir, fixture_paths = sys.argv[1], sys.argv[2:]
want = sum(
    "canonical" in c["expect"]
    for path in fixture_paths
    for c in json.load(open(path, encoding="utf-8")).get("canonicalize", [])
)
written = 0
with open(f"{out_dir}/expect", "w") as expect:
    for line in open(f"{out_dir}/dump", encoding="utf-8"):
        row = json.loads(line)
        if row["section"] == "canonicalize" and "canonical" in row["actual"]:
            written += 1
            with open(f"{out_dir}/{written}.bin", "wb") as blob:
                blob.write(row["actual"]["canonical"].encode("utf-8"))
            expect.write(f"{written} {row['actual']['digest']}\n")
print(want, written)
PY
  )
  digest_split_status=$?
  read -r digest_want digest_written <<< "$digest_split"
  if [ $digest_split_status -ne 0 ] || [ -z "$digest_written" ]; then
    fail "could not split canonical forms out of the dump (exit $digest_split_status)"
  elif [ "$digest_written" -eq 0 ] || [ "$digest_written" -ne "$digest_want" ]; then
    fail "dump held $digest_written canonical forms; the fixture inventory has $digest_want"
  else
    digest_bad=0
    digest_checked=0
    while read -r n want_digest; do
      got_digest="sha256:$($sha256_tool < "$digest_dir/$n.bin" | cut -d' ' -f1)"
      digest_checked=$((digest_checked + 1))
      if [ "$got_digest" != "$want_digest" ]; then
        digest_bad=$((digest_bad + 1))
        printf '  mismatch #%s: %s gives %s, runner said %s\n' "$n" "$sha256_tool" "$got_digest" "$want_digest"
      fi
    done < "$digest_dir/expect"
    if [ "$digest_checked" -ne "$digest_written" ]; then
      fail "checked $digest_checked of $digest_written canonical forms"
    elif [ "$digest_bad" -eq 0 ]; then
      pass "$sha256_tool reproduces all $digest_checked digests"
    else
      fail "$digest_bad of $digest_checked digests differ from $sha256_tool"
    fi
  fi
fi
rm -rf "$digest_dir"

echo "8. lint and types"
if command -v ruff >/dev/null 2>&1; then
  ruff check py >/dev/null 2>&1 && pass "ruff check py" || { ruff check py; fail "ruff reported issues"; }
  ruff format --check py >/dev/null 2>&1 && pass "ruff format --check py" || fail "ruff format --check py"
else
  skip "ruff" "not installed"
fi
if npx --no-install tsc --version >/dev/null 2>&1; then
  npx --no-install tsc -p ts/tsconfig.json >/dev/null 2>&1 \
    && pass "tsc -p ts/tsconfig.json" || { npx --no-install tsc -p ts/tsconfig.json; fail "tsc reported errors"; }
else
  skip "tsc" "typescript not installed locally; CI installs it"
fi

echo
if [ $failed -eq 0 ]; then
  if [ $skipped -gt 0 ]; then
    echo "all gates passed ($skipped skipped)"
  else
    echo "all gates passed"
  fi
  exit 0
fi
echo "$failed gate(s) failed"
exit 1
