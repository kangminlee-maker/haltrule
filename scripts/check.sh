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
fail() { printf '  FAIL  %s\n' "$1"; failed=$((failed + 1)); }
skip() { printf '  SKIP  %s (%s)\n' "$1" "$2"; skipped=$((skipped + 1)); }

FIXTURE_PATH="fixtures/breaker/v0.json"

echo "1. conformance — both implementations against the fixtures"
ts_full=$(node ts/run-fixtures.ts 2>&1)
ts_status=$?
ts_line=$(printf '%s\n' "$ts_full" | tail -1)
py_full=$(python3 py/run_fixtures.py 2>&1)
py_status=$?
py_line=$(printf '%s\n' "$py_full" | tail -1)
if [ $ts_status -eq 0 ]; then
  pass "typescript: $ts_line"
else
  fail "typescript runner exited $ts_status"
  printf '%s\n' "$ts_full"
fi
if [ $py_status -eq 0 ]; then
  pass "python: $py_line"
else
  fail "python runner exited $py_status"
  printf '%s\n' "$py_full"
fi

echo "2. instrument — a corrupted fixture must make both runners fail"

# Corrupts exactly one expected value in a fresh copy of the fixture and
# writes it to $2. $1 selects which field: every comparison path the runners
# make (classify.expect, backoff.expect_ms, and each of the four state.*
# fields) needs its own case here, because a runner that only ever gets
# exercised on one field (e.g. only ever state.tripped, as this gate used to
# do) has never had its other comparisons checked at all.
corrupt_fixture() {
  python3 - "$1" "$2" "$FIXTURE_PATH" <<'PY'
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
  local out
  out=$(mktemp -t haltrule.XXXXXX)

  corrupt_fixture "$target" "$out"
  local mutate_status=$?
  if [ $mutate_status -ne 0 ]; then
    fail "$desc: corruption script failed (exit $mutate_status) — cannot verify the instrument"
    rm -f "$out"
    return
  fi
  if cmp -s "$out" "$FIXTURE_PATH"; then
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
  pass "identical dump across $(wc -l < "$ts_dump_file" | tr -d ' ') cases"
else
  fail "runner output diverges — dumps are not byte-identical"
  diff "$py_dump_file" "$ts_dump_file" | head -20
fi
rm -f "$ts_dump_file" "$py_dump_file"

echo "4. purity — the policy files import nothing they should not"
# Broadened past a bare '^import': a dynamic `await import(...)`, a
# CommonJS `require(...)`, and a `export ... from "./somewhere"` re-export
# are all dependencies too, and none of them start a line with "import".
ts_import_hits=$(grep -nE '\bimport\b|\brequire[[:space:]]*\(|^[[:space:]]*export[[:space:]].*\bfrom\b' ts/breaker.ts || true)
if [ -z "$ts_import_hits" ]; then
  pass "ts/breaker.ts has no imports"
else
  fail "ts/breaker.ts has import(s)/require(s)/re-export(s); it must have none"
  printf '%s\n' "$ts_import_hits"
fi

py_purity_out=$(python3 - <<'PY'
import ast, sys
tree = ast.parse(open("py/haltrule/breaker.py").read())
modules = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        modules += [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        modules.append(node.module)
stdlib = set(sys.stdlib_module_names) | {"__future__"}
print(",".join(m for m in modules if m.split(".")[0] not in stdlib))
PY
)
py_purity_status=$?
if [ $py_purity_status -ne 0 ]; then
  fail "py/haltrule/breaker.py purity check crashed (exit $py_purity_status) — cannot verify import purity"
  printf '%s\n' "$py_purity_out"
elif [ -z "$py_purity_out" ]; then
  pass "py/haltrule/breaker.py imports only the standard library"
else
  fail "py/haltrule/breaker.py imports third-party modules: $py_purity_out"
fi

echo "5. determinism — no clock, timer, or I/O in the policy files"
# The spec requires these to stay pure (no I/O, no clock, no sleep). Checked
# only against the two policy files, never the runners, which legitimately
# read argv and the fixture file.
ts_clock_hits=$(grep -nE '\bDate\b|\bsetTimeout\b|\bsetInterval\b|\bprocess\b|\brequire[[:space:]]*\(|\bimport[[:space:]]*\(' ts/breaker.ts || true)
if [ -z "$ts_clock_hits" ]; then
  pass "ts/breaker.ts touches no clock, timer, or I/O API"
else
  fail "ts/breaker.ts references a clock/timer/I/O API"
  printf '%s\n' "$ts_clock_hits"
fi

py_clock_out=$(python3 - <<'PY'
import ast

FORBIDDEN_MODULES = {"time", "datetime", "random", "os"}
tree = ast.parse(open("py/haltrule/breaker.py").read())
hits = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                hits.append(f"line {node.lineno}: import {alias.name}")
    elif isinstance(node, ast.ImportFrom) and node.module:
        if node.module.split(".")[0] in FORBIDDEN_MODULES:
            hits.append(f"line {node.lineno}: from {node.module} import ...")
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    ):
        hits.append(f"line {node.lineno}: open(...)")
print("\n".join(hits))
PY
)
py_clock_status=$?
if [ $py_clock_status -ne 0 ]; then
  fail "py/haltrule/breaker.py clock/timer/I/O check crashed (exit $py_clock_status)"
  printf '%s\n' "$py_clock_out"
elif [ -z "$py_clock_out" ]; then
  pass "py/haltrule/breaker.py touches no clock, timer, or I/O API"
else
  fail "py/haltrule/breaker.py references a clock/timer/I/O API"
  printf '%s\n' "$py_clock_out"
fi

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

echo "7. lint and types"
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
