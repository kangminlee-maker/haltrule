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
PROTOCOL_FIXTURE_PATH="fixtures/protocol/v0.json"

# Every fixture file, found the way both runners find theirs when given no path:
# each .json under fixtures/. Gates count against this inventory, never against
# a runner's own report of what it ran, so a runner that skips a file or a
# section cannot pass by agreeing with itself.
#
# list_files NAME find-arguments...: fills the array NAME, sorted. The listing
# goes through a file so find's exit status survives: a find that failed has
# listed part of what is there, and a partial inventory agrees with itself.
listing_failures=""
list_files() {
  local into=$1 listing find_status f
  shift
  if ! listing=$(mktemp -t haltrule.XXXXXX); then
    listing_failures="$listing_failures $into (mktemp failed)"
    return
  fi
  find "$@" -print0 > "$listing"
  find_status=$?
  if [ $find_status -ne 0 ]; then
    listing_failures="$listing_failures $into (find exit $find_status)"
  elif ! LC_ALL=C sort -z -o "$listing" "$listing"; then
    listing_failures="$listing_failures $into (sort failed)"
  fi
  while IFS= read -r -d '' f; do eval "$into+=(\"\$f\")"; done < "$listing"
  rm -f "$listing"
}
fixture_files=()
list_files fixture_files fixtures -type f -name '*.json'

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
list_files ts_policy_files ts -type f -name '*.ts' ! -path ts/run-fixtures.ts ! -path '*/node_modules/*'
py_policy_files=()
list_files py_policy_files py/haltrule -type f -name '*.py'

# The fixture file a corruption target lives in.
fixture_for_target() {
  case "$1" in
    canonicalize_*|checkpoint_*) printf '%s\n' "$CHECKPOINT_FIXTURE_PATH" ;;
    charge_*) printf '%s\n' "$BUDGET_FIXTURE_PATH" ;;
    validate_*) printf '%s\n' "$SLOT_FIXTURE_PATH" ;;
    result_line_*) printf '%s\n' "$PROTOCOL_FIXTURE_PATH" ;;
    *) printf '%s\n' "$FIXTURE_PATH" ;;
  esac
}

echo "0. inventory — the files every gate below counts against"
if [ -n "$listing_failures" ]; then
  fail "could not list every file:$listing_failures — the inventories below would be partial"
else
  pass "listed ${#fixture_files[@]} fixture files, ${#ts_policy_files[@]} typescript and ${#py_policy_files[@]} python policy files; every listing succeeded"
fi

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
elif target == "charge_refused_to_verdict":
    case = next(c for c in fixtures["charge"] if c["id"] == "refused_budget_negative_charge")
    case["expect"]["verdicts"][0] = {"spec": "haltrule/0", "verdict": "ok", "reason": "budget_ok", "resume": None}
elif target == "charge_verdict_to_refused":
    fixtures["charge"][0]["expect"]["verdicts"][0] = {"refused": True}
elif target == "charge_refused_budget_to_verdicts":
    case = next(c for c in fixtures["charge"] if c["expect"] == {"refused": True})
    case["expect"] = {"verdicts": [], "used": {"turns": "0", "ms": "0", "tokens": "0"}}
elif target == "charge_used_after_refusal":
    # the ledger a refused charge would leave if its first amount were kept
    next(c for c in fixtures["charge"] if c["id"] == "refused_charge_changes_nothing")["expect"]["used"]["turns"] = "5"
elif target == "validate_refused_to_verdict":
    case = next(c for c in fixtures["validate"] if c["expect"] == {"refused": True})
    case["expect"] = {"spec": "haltrule/0", "verdict": "halt", "reason": "slot_invalid", "resume": None}
elif target == "validate_verdict_to_refused":
    fixtures["validate"][0]["expect"] = {"refused": True}
elif target == "result_line_text":
    # The smallest wrong line there is: two members of a map, swapped.
    case = next(c for c in fixtures["result_line"] if c["expect"].get("line") == '{"1":"one","10":"ten","2":"two"}')
    case["expect"]["line"] = '{"1":"one","2":"two","10":"ten"}'
elif target == "result_line_refused_to_line":
    next(c for c in fixtures["result_line"] if "refused" in c["expect"])["expect"] = {"line": "1.5"}
elif target == "result_line_line_to_refused":
    next(c for c in fixtures["result_line"] if "line" in c["expect"])["expect"] = {"refused": True}
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
corrupt_and_verify charge_refused_to_verdict         "a refused charge rewritten as a verdict"
corrupt_and_verify charge_verdict_to_refused         "a charge verdict rewritten as a refusal"
corrupt_and_verify charge_refused_budget_to_verdicts "a refused budget rewritten as one that ran"
corrupt_and_verify charge_used_after_refusal         "a ledger that kept part of a refused charge"
corrupt_and_verify validate_refused_to_verdict       "a refused slot spec rewritten as a verdict"
corrupt_and_verify validate_verdict_to_refused       "a validate verdict rewritten as a refusal"
corrupt_and_verify result_line_text            "a result line with two members swapped"
corrupt_and_verify result_line_refused_to_line "a refused result rewritten as a line"
corrupt_and_verify result_line_line_to_refused "a result line rewritten as a refusal"

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
  python3 - "$1" "$2" "$CHECKPOINT_FIXTURE_PATH" "$BUDGET_FIXTURE_PATH" <<'PY'
import json, sys

kind, out_path, fixture_path, budget_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
fixtures = json.load(open(fixture_path))
if kind == "raw_number":
    fixtures["canonicalize"][0]["input"] = 7
elif kind == "unknown_version":
    fixtures["fixture_version"] = "checkpoint/v999"
elif kind == "unknown_kind":
    fixtures["canonicalize"][0]["input"] = {"$unsupported": "no_such_kind"}
elif kind == "wide_number":
    fixtures["canonicalize"][0]["input"] = {"$number": "9007199254740993"}
elif kind == "wide_number_negative":
    fixtures["canonicalize"][0]["input"] = {"$number": "-9007199254740993"}
elif kind.startswith("number_literal:"):
    fixtures["canonicalize"][0]["input"] = {"$number": kind.split(":", 1)[1]}
elif kind.startswith("refused_charge_literal:"):
    # a malformed literal exactly where the case expects a refusal
    fixtures = json.load(open(budget_path))
    case = next(c for c in fixtures["charge"] if c["id"] == "refused_budget_negative_charge")
    case["charges"][0]["turns"] = {"$number": kind.split(":", 1)[1]}
elif kind.startswith("bigint_literal:"):
    fixtures["canonicalize"][0]["input"] = {"$bigint": kind.split(":", 1)[1]}
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
refuse_and_verify wide_number_negative "not exactly representable as a double" "a negative \$number integer literal a double cannot hold"
# A literal is read by the grammar in fixtures/README.md, not by whatever a
# language's own number parser takes. Each literal here is one a parser reads
# and the grammar does not: Python's float reads "1_0" (as 10), "nan" and
# " 7 "; JavaScript's Number reads "0x10" (as 16), "" (as 0) and " 7 "; both
# read "01". Python's int reads "1_0", JavaScript's BigInt reads "0x10" and
# "" - and "1.0" is a number, not an integer.
for loose_literal in "1_0" "0x10" "" " 7 " "nan" "01"; do
  refuse_and_verify "number_literal:$loose_literal" "is outside the fixture grammar" "the \$number literal '$loose_literal'"
done
# Where a case expects a refusal, a malformed fixture must still be the runner's
# error and not the refusal the case was waiting for.
refuse_and_verify "refused_charge_literal:-1_0" "is outside the fixture grammar" "a malformed literal where a refusal is expected"
for loose_literal in "1_0" "0x10" "" "1.0"; do
  refuse_and_verify "bigint_literal:$loose_literal" "is outside the fixture grammar" "the \$bigint literal '$loose_literal'"
done

# An argument outside a part's contract is refused, and that is a fixture
# outcome ({"refused": true}) every port shares - not a probe written once per
# language here. Gate 6 holds the list to its size.

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
known_files=(py/run_fixtures.py ts/run-fixtures.ts ts/package.json ts/tsconfig.json "${ts_policy_files[@]}" "${py_policy_files[@]}")
# Exact paths, one by one: joined into a string, a file named after two
# neighbours in it would count as known.
is_known_file() {
  local candidate="$1" known
  for known in "${known_files[@]}"; do [ "$known" = "$candidate" ] && return 0; done
  return 1
}
# The listing goes to a file so that find's own status is seen; a loop fed by
# a process substitution never learns that the listing failed, and an empty
# listing has no unknown files in it.
policy_listing=$(mktemp -t haltrule.XXXXXX)
find py ts -mindepth 1 \( -type f -o -type l \) ! -path '*/__pycache__/*' ! -path '*/node_modules/*' -print0 > "$policy_listing"
policy_listing_status=$?
unknown_files=()
listed=0
runners_listed=0
while IFS= read -r -d '' f; do
  listed=$((listed + 1))
  case "$f" in py/run_fixtures.py | ts/run-fixtures.ts) runners_listed=$((runners_listed + 1)) ;; esac
  is_known_file "$f" || unknown_files+=("$f")
done < "$policy_listing"
rm -f "$policy_listing"
if [ $policy_listing_status -ne 0 ] || [ "$runners_listed" -ne 2 ]; then
  fail "could not list py/ and ts/ (find exit $policy_listing_status; $listed entries, $runners_listed of the 2 runners among them): the unknown-file rule checked nothing"
elif [ "${#unknown_files[@]}" -eq 0 ]; then
  pass "py/ and ts/ hold only the runners, the policy files, and ts/package.json, ts/tsconfig.json ($listed entries)"
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
# The allowlist is written once, here, and handed to this scan and to the seal.
PY_ALLOWED_MODULES="__future__ dataclasses hashlib math typing haltrule"
for py_file in "${py_policy_files[@]}"; do
py_clock_out=$(python3 - "$py_file" "$PY_ALLOWED_MODULES" <<'PY'
import ast, sys

ALLOWED_MODULES = set(sys.argv[2].split())
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
# A seal inside the process it watches is not a sandbox: it closes the routes
# named here and found in review, and code written to escape it may find
# another. It is one of three layers. The static scans (5a, 5b) refuse what
# can be read off the source: imports, forbidden names, and the attributes
# that lead from an object back to the runtime. The seal refuses at run time
# what a name alone cannot show: a global reached without being written, code
# built from a string, a module swapped under its name. The behavioural check
# (5e) catches an output that moves with the hash seed, whatever produced it.
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
// Code built from a string runs with every global in reach, whatever the
// static scan allowed by name. It is the capability that is sealed, by every
// way to it: the four constructors where any function finds them -
// `f.constructor` - and the global bindings, which the global object hands to
// whoever reaches it without writing its name.
for (const sample of [function () {}, async function () {}, function* () {}, async function* () {}]) {
  Object.defineProperty(Object.getPrototypeOf(sample), "constructor", { value: refuse("a function constructor") });
}
globalThis.Function = refuse("a function constructor");
globalThis.eval = refuse("eval");
// What differs from one process to the next, beyond the clocks above.
for (const name of ["pid", "ppid"]) Object.defineProperty(process, name, { get: refuse(`process.${name}`) });
for (const name of ["memoryUsage", "cpuUsage", "resourceUsage"]) process[name] = refuse(`process.${name}`);
// And the doors from the process object to every built-in module - the file
// system, the operating system - that gate 4 closes to an import statement.
for (const name of ["getBuiltinModule", "binding", "_linkedBinding", "dlopen"]) process[name] = refuse(`process.${name}`);
// node's own module loader reads the environment for every module it loads,
// so the environment answers node and refuses everyone else: the caller is
// the frame above the trap, and only a node:internal frame is let through.
const realEnv = process.env;
const readByNodeItself = () => (String(new Error().stack).split("\n")[3] ?? "").includes("node:internal/");
const envTrap = (answer) => (...args) => (readByNodeItself() ? answer(...args) : refuse("process.env")());
Object.defineProperty(process, "env", {
  value: new Proxy(realEnv, { get: envTrap(Reflect.get), has: envTrap(Reflect.has), ownKeys: envTrap(Reflect.ownKeys) }),
});
// Self-check in this process: every probe must throw before the marker prints.
const probes = [
  () => Date.now(), () => new Date(), () => Math.random(), () => performance.now(), () => setTimeout(() => {}, 0),
  () => (() => 0).constructor("return 0"), () => (async () => 0).constructor("return 0"),
  () => globalThis.Function("return 0"), () => process.pid, () => process.env.HOME, () => process.memoryUsage(),
  () => process.getBuiltinModule("node:fs"),
];
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
  pass "typescript (sealed run) same outcome as unsealed with Date, performance, Math.random, timers, fetch, Web Crypto randomness, process clocks and per-process state, and code built from strings refusing"
fi
rm -f "$ts_seal"

py_seal=$(mktemp -t haltrule.XXXXXX)
cat > "$py_seal" <<'PY'
import builtins
import importlib.util
import os
import runpy
import sys
import sysconfig
import types

# argv: the module allowlist, then the policy inventory gate 0 listed - the
# seal builds exactly those files, so no gate reads one set and runs another.
ALLOWED_MODULES = set(sys.argv[1].split())
POLICY_FILES = sys.argv[2:]
REFUSED = ("open", "print", "input", "exec", "eval", "compile", "breakpoint", "hash", "id", "repr")
# What policy code may take from each standard-library module: names, not
# modules, and only ones that evaluate nothing. A public function can carry
# the whole runtime with it - typing.get_type_hints evaluates a string with
# the real builtins - so a name is absent until someone adds it here on
# purpose. Attribute reach past these names is recorded and refused; reach
# through a listed object's own attributes (__globals__ and the like) is the
# static scan's to refuse, by FORBIDDEN_ATTRIBUTES.
SURFACE_NAMES = {
    "__future__": {"annotations"},
    "dataclasses": {"dataclass", "field"},
    "hashlib": {"sha256"},
    "math": {"ceil", "floor", "trunc", "isfinite", "isinf", "isnan", "inf", "nan"},
    "typing": {"Any", "Final", "Iterable", "Literal", "Mapping", "Optional", "Sequence", "Tuple", "Union"},
}
if set(SURFACE_NAMES) != ALLOWED_MODULES - {"haltrule"}:
    sys.exit(f"the seal's surfaces {sorted(SURFACE_NAMES)} do not match the allowlist {sorted(ALLOWED_MODULES)}")


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


def sealed_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED_MODULES:
        record(f"imported {name!r}")
        raise ImportError(f"sealed: policy code imported {name!r}")
    # A standard-library name answers with the surface pinned below, never
    # with whatever sys.modules holds under that name by now.
    if name in surfaces:
        return surfaces[name]
    if name not in sealed_modules:
        record(f"imported {name!r}, which the seal did not build")
        raise ImportError(f"sealed: policy code imported {name!r}, which the seal did not build")
    # A sibling runs the first time anything imports it, so no policy module
    # has to sort before the ones that import it.
    run_sealed(name)
    for part in fromlist or ():
        if f"{name}.{part}" in sealed_modules:
            run_sealed(f"{name}.{part}")
        elif hasattr(sealed_modules[name], "__path__") and not hasattr(sealed_modules[name], part):
            record(f"imported '{name}.{part}', which the seal did not build")
            raise ImportError(f"sealed: policy code imported '{name}.{part}', which the seal did not build")
    return sealed_modules[name if fromlist else name.partition(".")[0]]


# The policy modules get their own builtins; the runner keeps the real ones.
SEALED = dict(vars(builtins), __import__=sealed_import, **{n: refuse(n) for n in REFUSED})

sys.path.insert(0, "py")

# An allowlisted standard-library name must resolve to the interpreter's own
# file. The finders are asked directly, before any policy code runs: what a
# loaded module says about itself - its __file__, its __spec__ - is its own
# to rewrite, and sys.modules would answer with exactly that.
stdlib_roots = tuple(os.path.realpath(sysconfig.get_paths()[key]) + os.sep for key in ("stdlib", "platstdlib"))


def resolved_spec(module_name):
    for finder in sys.meta_path:
        find_spec = getattr(finder, "find_spec", None)
        found = find_spec(module_name, None) if find_spec else None
        if found is not None:
            return found
    return None


def surface_of(module_name, module):
    """What policy code gets for a standard-library import: the listed names
    and nothing else. The values are the module's own objects, shared, not
    copies."""
    surface = types.ModuleType(module_name)
    for attribute in sorted(SURFACE_NAMES[module_name]):
        if attribute in vars(module):
            setattr(surface, attribute, vars(module)[attribute])

    def beyond_the_surface(attribute):
        record(f"reached {module_name}.{attribute}, which is not on the seal's list for {module_name}")
        raise AttributeError(f"sealed: {module_name}.{attribute} is not on the seal's list for {module_name}")

    surface.__getattr__ = beyond_the_surface
    return surface


# The object policy code receives is built here, from the spec just checked:
# checking a name and then trusting whatever is registered under it - before
# this point or after policy code has run - checks one thing and uses another.
pinned = {}
surfaces = {}
for module_name in sorted(ALLOWED_MODULES - {"haltrule"}):
    spec = resolved_spec(module_name)
    origin = spec.origin if spec is not None else None
    if origin not in ("built-in", "frozen"):
        if origin is None or not os.path.realpath(origin).startswith(stdlib_roots):
            record(f"module {module_name} is not the standard library's: {origin}")
            sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    pinned[module_name] = module
    surfaces[module_name] = surface_of(module_name, module)

# No haltrule module may exist before the seal builds them.
for loaded_name in list(sys.modules):
    if loaded_name == "haltrule" or loaded_name.startswith("haltrule."):
        record(f"module {loaded_name} loaded outside the seal")
        sys.exit(1)

# Every file of the policy inventory, by dotted name, a package before what it
# holds - code in a package marker would otherwise run outside the seal; one
# that another imports runs when that import happens.
def dotted(file):
    name = file[len("py/") : -len(".py")].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


modules = sorted((dotted(file), file) for file in POLICY_FILES)
if not modules or modules[0][0] != "haltrule":
    sys.exit(f"the policy inventory does not start with the haltrule package: {POLICY_FILES[:3]}")
# Every module object exists, sealed and registered, before any of them runs:
# a policy that imports a sibling at load time can then only reach a sealed
# object, never one the import machinery would build with the real builtins
# and the importer would keep after the seal replaced it in sys.modules.
sealed_modules = {}
specs = {}
started = set()
finished = set()


def run_sealed(name):
    """Execute a sealed module once. A module met again while it is still
    running - an import cycle - is handed over as it stands, as Python does."""
    global recording
    if name in started:
        return
    started.add(name)
    parent = name.rpartition(".")[0]
    if parent in sealed_modules:
        run_sealed(parent)
    module = sealed_modules[name]
    try:
        specs[name].loader.exec_module(module)
    except BaseException as error:
        # An importer that catches this would go on with half a module that
        # never met the self-check below; the record outlives the catch.
        record(f"module {name} failed while loading: {type(error).__name__}")
        raise
    # Self-check in the module's own namespace: `open` there must refuse.
    recording = False
    try:
        eval("open('/dev/null')", dict(module.__dict__))
    except RuntimeError:
        pass
    else:
        sys.exit(f"{name} loaded without the seal")
    recording = True
    finished.add(name)


for name, file in modules:
    spec = importlib.util.spec_from_file_location(
        name, file, submodule_search_locations=[os.path.dirname(file)] if file.endswith("/__init__.py") else None
    )
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__builtins__"] = SEALED
    sys.modules[name] = module
    parent = name.rpartition(".")[0]
    if parent:
        if parent not in sealed_modules:
            sys.exit(f"{file} lies in a directory that is not a package of the policy inventory")
        setattr(sealed_modules[parent], name.rpartition(".")[2], module)
    sealed_modules[name] = module
    specs[name] = spec
for name in sealed_modules:
    run_sealed(name)
if finished != set(sealed_modules):
    sys.exit(f"sealed modules that never finished loading: {sorted(set(sealed_modules) - finished)}")

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
        real = os.path.realpath(file) if file else ""
        if loaded_name == "haltrule" or loaded_name.startswith("haltrule.") or real.startswith(root):
            if sealed_modules.get(loaded_name) is loaded:
                continue
            record(f"module {loaded_name} loaded outside the seal")
    # A standard-library name must still answer with the object pinned before
    # any policy code ran.
    for module_name, module in pinned.items():
        if sys.modules.get(module_name) is not module:
            record(f"module {module_name} replaced under the seal")
    # And every module object a policy module holds must be a sealed module or
    # a surface pinned above - identities fixed before policy code ran, not
    # read back from a sys.modules that policy code could have written to.
    trusted = {id(module) for module in sealed_modules.values()}
    trusted |= {id(surface) for surface in surfaces.values()}
    whole = {id(module): module_name for module_name, module in pinned.items()}
    for holder_name, holder in sealed_modules.items():
        for held in list(vars(holder).values()):
            if not isinstance(held, types.ModuleType) or id(held) in trusted:
                continue
            if id(held) in whole:
                record(f"{holder_name} holds module {whole[id(held)]} itself, not the names the seal hands out")
            else:
                held_name = vars(held).get("__name__", "?")
                record(f"module {held_name} held by {holder_name} loaded outside the seal")
PY
py_sealed_out=$(python3 "$py_seal" "$PY_ALLOWED_MODULES" "${py_policy_files[@]}" 2>&1)
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

part_counts=$(python3 - "$BUDGET_FIXTURE_PATH" "$SLOT_FIXTURE_PATH" "$PROTOCOL_FIXTURE_PATH" <<'PY'
import json, sys
charge, validate = json.load(open(sys.argv[1]))["charge"], json.load(open(sys.argv[2]))["validate"]
refused = {"refused": True}
print(
    len(charge),
    len(validate),
    len(json.load(open(sys.argv[3]))["result_line"]),
    # a refused budget, or a charge case holding a refused charge
    sum(1 for c in charge if c["expect"] == refused or refused in c["expect"].get("verdicts", [])),
    sum(1 for c in validate if c["expect"] == refused),
)
PY
)
part_counts_status=$?
if [ $part_counts_status -ne 0 ] || [ -z "$part_counts" ]; then
  fail "could not read fixture case counts from $BUDGET_FIXTURE_PATH, $SLOT_FIXTURE_PATH and $PROTOCOL_FIXTURE_PATH (exit $part_counts_status)"
else
  read -r charge_n validate_n result_line_n charge_refused_n validate_refused_n <<< "$part_counts"
  if [ "$charge_n" -ge 30 ]; then pass "charge: $charge_n cases (>= 30)"; else fail "charge: only $charge_n cases (need >= 30)"; fi
  if [ "$validate_n" -ge 40 ]; then pass "validate: $validate_n cases (>= 40)"; else fail "validate: only $validate_n cases (need >= 40)"; fi
  # The arguments each part refuses were 29 probes in this script until they
  # became cases; the list may grow, and must not quietly shrink.
  if [ "$charge_refused_n" -ge 20 ]; then pass "charge: $charge_refused_n cases hold a refusal (>= 20)"; else fail "charge: only $charge_refused_n cases hold a refusal (need >= 20)"; fi
  if [ "$validate_refused_n" -ge 13 ]; then pass "validate: $validate_refused_n cases are refusals (>= 13)"; else fail "validate: only $validate_refused_n cases are refusals (need >= 13)"; fi
  if [ "$result_line_n" -ge 30 ]; then pass "result_line: $result_line_n cases (>= 30)"; else fail "result_line: only $result_line_n cases (need >= 30)"; fi
fi

# fixtures/README.md promises ASCII files, so no editor or transport can
# normalize a test value away (an NFD case silently becoming NFC).
# The inventory must hold each file the gates name: a listing that lost one
# would otherwise shrink every count taken against it without a word.
missing_fixtures=""
for named_fixture in "$FIXTURE_PATH" "$CHECKPOINT_FIXTURE_PATH" "$BUDGET_FIXTURE_PATH" "$SLOT_FIXTURE_PATH" "$PROTOCOL_FIXTURE_PATH"; do
  in_inventory=0
  if [ "${#fixture_files[@]}" -gt 0 ]; then
    for f in "${fixture_files[@]}"; do
      if [ "$f" = "$named_fixture" ]; then in_inventory=1; fi
    done
  fi
  if [ $in_inventory -eq 0 ]; then missing_fixtures="$missing_fixtures $named_fixture"; fi
done
if [ -n "$missing_fixtures" ]; then
  fail "the fixture inventory (${#fixture_files[@]} files) is missing:$missing_fixtures"
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

echo "9. mutation shards — the workflow names slices 1/N..N/N, and those slices hold every catalog mutant once"
# Each CI job runs one slice and sees nothing of the others, so this job reads
# the workflow. The mutation job is read as a block: its matrix must have the
# one key shard, naming every slice once, handed to --shard as it is; nothing
# in the job may drop a slice or excuse its failure (exclude, include, if,
# continue-on-error); and no other job may run the suite. mutants.py owns the
# I/N grammar and proves the named slices hold the catalog. Whether GitHub
# then ran the jobs is not something a script in the repository can see.
ci_shards_out=$(python3 - .github/workflows/check.yml <<'PY' 2>&1
import re, sys

JOB = "gates-detect-a-defect"
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
starts = [i for i, line in enumerate(lines) if line == f"  {JOB}:"]
if len(starts) != 1:
    sys.exit(f"found {len(starts)} jobs named {JOB}, not 1")
first = starts[0]
last = next((i for i in range(first + 1, len(lines)) if re.match(r"^  [^\s#]", lines[i])), len(lines))
code = lambda rows: [row for row in rows if not row.lstrip().startswith("#")]
block, outside = code(lines[first:last]), code(lines[:first] + lines[last:])
if any("mutants.py" in row for row in outside):
    sys.exit(f"mutants.py is run outside the {JOB} job")
for key in ("exclude", "include", "if", "continue-on-error"):
    if any(re.match(rf"^\s*(- )?{key}\s*:", row) for row in block):
        sys.exit(f"the {JOB} job uses {key}:, which can drop a slice or excuse its failure")
matrix = [i for i, row in enumerate(block) if re.match(r"^\s*matrix:\s*$", row)]
if len(matrix) != 1:
    sys.exit(f"found {len(matrix)} matrix blocks in the {JOB} job, not 1")
depth = len(block[matrix[0]]) - len(block[matrix[0]].lstrip())
keys = []
for row in block[matrix[0] + 1 :]:
    if row.strip() and len(row) - len(row.lstrip()) <= depth:
        break
    found = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:", row)
    if found and len(row) - len(row.lstrip()) == depth + 2:
        keys.append(found[1])
if keys != ["shard"]:
    sys.exit(f"the matrix of the {JOB} job has the keys {keys}, not just shard")
rows = [found[1] for row in block if (found := re.match(r"^\s*shard:\s*\[(.*)\]\s*$", row))]
if len(rows) != 1:
    sys.exit(f"found {len(rows)} one-line shard lists in the {JOB} job, not 1")
runs = [found[1] for row in block if (found := re.match(r"^\s*run:\s*python3 scripts/mutants\.py\s+(.*?)\s*$", row))]
if runs != ["--shard ${{ matrix.shard }}"]:
    sys.exit(f"the mutation step does not hand each matrix entry to --shard as it is: {runs}")
print(",".join(entry.strip().strip("\x22\x27") for entry in rows[0].split(",")))
PY
)
ci_shards_status=$?
if [ $ci_shards_status -ne 0 ] || [ -z "$ci_shards_out" ]; then
  fail "the workflow does not name every mutation slice once in one job: $ci_shards_out"
else
  shards_control_out=$(python3 scripts/mutants.py --shards-control "$ci_shards_out" 2>&1)
  shards_control_status=$?
  if [ $shards_control_status -ne 0 ]; then
    fail "the shards the workflow names ($ci_shards_out) do not hold every catalog mutant exactly once: $(printf '%s\n' "$shards_control_out" | tail -1)"
  else
    pass "the workflow names $ci_shards_out in one job with nothing that drops or excuses a slice, and $(printf '%s\n' "$shards_control_out" | tail -1 | sed 's/^mutants: //')"
  fi
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
