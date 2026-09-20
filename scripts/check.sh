#!/usr/bin/env bash
# The gates. Each is one command that passes or fails, and none of them knows a
# port from the inside: scripts/conform.py judges every language by the lines
# its adapter prints, and purity is a property of how the modules are built
# (what the compiler is given, what a few allowlists hold), not a search through
# their text. A new port adds its adapter to gate 2 and its own mainstream
# tools to gate 3.
#
# Every gate here has been seen to fail: scripts/mutants.py plants a defect for
# each and requires this script to fail with that gate's evidence.
set -uo pipefail
cd "$(dirname "$0")/.."

failed=0
# gate <what a pass shows> <command...>
gate() {
  local what="$1" out
  shift
  if out=$("$@" 2>&1); then
    printf '  PASS  %s\n' "$what"
  else
    printf '  FAIL  %s\n' "$what"
    printf '%s\n' "$out"
    failed=$((failed + 1))
  fi
}
# A tool's findings, each marked as the finding it is. sed reads to the end, so
# no reader leaves a pipe early (see the note on pipefail in the repository's history).
findings() {
  "$@" 2>&1 | sed -e '/^[[:space:]]*$/d' -e 's/^/FAIL [finding] /'
  return "${PIPESTATUS[0]}"
}

echo "1. the driver — the one judge can fail"
gate "every corrupted expectation fails under its own id; malformed fixtures and malformed output are refused" \
  python3 scripts/conform.py self-test

echo "2. conformance — each adapter's lines are, byte for byte, the lines the fixtures expect"
gate "typescript conforms" python3 scripts/conform.py check node ts/adapter/adapter.ts
# String hashing is seeded per process; a result that follows set order moves with the seed.
for seed in 0 1 2; do
  gate "python conforms (hash seed $seed)" env PYTHONHASHSEED="$seed" python3 scripts/conform.py check python3 py/adapter.py
done

echo "3. purity — the modules cannot reach the host, and name nothing that is not a function of its arguments"
gate "typescript modules compile with no host types: ts/tsconfig.json, and ts/host.d.ts is all the host there is" \
  findings node_modules/.bin/tsc -p ts/tsconfig.json
gate "typescript modules name no clock, randomness, locale, code from text, or global object: eslint.config.mjs" \
  findings node_modules/.bin/eslint --max-warnings 0 ts
gate "python modules import and use only what the allowlists hold: scripts/py_purity.py" \
  findings python3 scripts/py_purity.py py/haltrule/*.py

echo "4. lint and types"
gate "ruff check" findings ruff check --quiet py scripts
gate "ruff format --check" findings ruff format --check --quiet py scripts
gate "the typescript adapter type-checks" findings node_modules/.bin/tsc -p ts/adapter/tsconfig.json

echo
if [ "$failed" -eq 0 ]; then
  echo "all gates passed"
  exit 0
fi
echo "$failed gate(s) failed"
exit 1
