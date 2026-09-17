#!/usr/bin/env bash
# Deterministic gates. CI runs this file, and so should you before pushing.
#
# Every gate here answers a question that has one right answer, so a failure is
# a defect and never a judgment call. Gate 2 is the one that matters most: it
# checks the instrument rather than the code, because a conformance runner that
# cannot fail is worth less than no runner at all.
set -uo pipefail

cd "$(dirname "$0")/.."
failed=0
skipped=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failed=$((failed + 1)); }
skip() { printf '  SKIP  %s (%s)\n' "$1" "$2"; skipped=$((skipped + 1)); }

echo "1. conformance — both implementations against the fixtures"
ts_line=$(node ts/run-fixtures.ts 2>/dev/null | tail -1)
ts_status=$?
py_line=$(python3 py/run_fixtures.py 2>/dev/null | tail -1)
py_status=$?
[ $ts_status -eq 0 ] && pass "typescript: $ts_line" || fail "typescript runner exited $ts_status"
[ $py_status -eq 0 ] && pass "python: $py_line" || fail "python runner exited $py_status"

echo "2. instrument — a corrupted fixture must make both runners fail"
corrupt=$(mktemp -t haltrule-corrupt).json
python3 - "$corrupt" <<'PY'
import json, sys
fixtures = json.load(open("fixtures/breaker/v0.json"))
fixtures["state"][0]["expect"]["tripped"]["threshold"] = 10_000
json.dump(fixtures, open(sys.argv[1], "w"))
PY
node ts/run-fixtures.ts "$corrupt" >/dev/null 2>&1
[ $? -ne 0 ] && pass "typescript runner rejects a corrupted fixture" \
             || fail "typescript runner PASSED a corrupted fixture — it is not reading what it was given"
python3 py/run_fixtures.py "$corrupt" >/dev/null 2>&1
[ $? -ne 0 ] && pass "python runner rejects a corrupted fixture" \
             || fail "python runner PASSED a corrupted fixture — it is not reading what it was given"
node ts/run-fixtures.ts /nonexistent/fixture.json >/dev/null 2>&1
[ $? -ne 0 ] && pass "typescript runner fails on a missing fixture" \
             || fail "typescript runner survived a missing fixture"
python3 py/run_fixtures.py /nonexistent/fixture.json >/dev/null 2>&1
[ $? -ne 0 ] && pass "python runner fails on a missing fixture" \
             || fail "python runner survived a missing fixture"
rm -f "$corrupt"

echo "3. parity — the two runners agree, verbatim"
if [ "$ts_line" = "$py_line" ] && [ -n "$ts_line" ]; then
  pass "identical final line"
else
  fail "final lines differ: [$ts_line] vs [$py_line]"
fi

echo "4. purity — the policy files import nothing they should not"
ts_imports=$(grep -c '^import' ts/breaker.ts)
[ "$ts_imports" -eq 0 ] && pass "ts/breaker.ts has no imports" \
                        || fail "ts/breaker.ts has $ts_imports import(s); it must have none"
py_third_party=$(python3 - <<'PY'
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
[ -z "$py_third_party" ] && pass "py/haltrule/breaker.py imports only the standard library" \
                         || fail "py/haltrule/breaker.py imports third-party modules: $py_third_party"

echo "5. lint and types"
if command -v ruff >/dev/null 2>&1; then
  ruff check py >/dev/null 2>&1 && pass "ruff check py" || { ruff check py; fail "ruff reported issues"; }
  ruff format --check py >/dev/null 2>&1 && pass "ruff format --check py" || fail "ruff format --check py"
else
  skip "ruff" "not installed"
fi
if npx --no-install tsc --version >/dev/null 2>&1; then
  npx --no-install tsc --noEmit --strict --target es2022 --module preserve --moduleResolution bundler \
      ts/breaker.ts ts/run-fixtures.ts >/dev/null 2>&1 \
    && pass "tsc --noEmit --strict" || { npx --no-install tsc --noEmit --strict --target es2022 \
      --module preserve --moduleResolution bundler ts/breaker.ts ts/run-fixtures.ts; fail "tsc reported errors"; }
else
  skip "tsc" "typescript not installed locally; CI installs it"
fi

echo
if [ $failed -eq 0 ]; then
  echo "all gates passed${skipped:+ ($skipped skipped)}"
  exit 0
fi
echo "$failed gate(s) failed"
exit 1
