#!/usr/bin/env bash
# The gates. Each is one command that passes or fails, and none of them knows a
# port from the inside: scripts/conform.py judges every language by the lines
# its adapter prints, and purity is a property of how the modules are built
# (what the compiler is given, what a few allowlists hold), not a search through
# their text. A new port adds its adapter to gate 2 and its own mainstream
# tools to gate 3; a shell program of its own goes beside the others in gate 2.
#
# Every gate that decides conformance or purity has been seen to fail:
# scripts/mutants.py plants a defect for each and requires this script to fail
# with that gate's evidence. The build, lint and format gates below are their
# tools' own and are not planted for: they fail when their tool does. Whether the
# fixtures notice a defect in a port's own modules is asked by the mainstream
# mutation tools instead: scripts/survivors.py ts, py, go and rust.
set -uo pipefail
cd "$(dirname "$0")/.."
# Where rustup puts cargo, for a shell that has not been told about it. An
# installation already on the PATH still wins.
PATH="$PATH:$HOME/.cargo/bin"

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

echo "1. the judges can fail"
gate "every corrupted expectation fails under its own id; malformed fixtures and malformed output are refused" \
  python3 scripts/conform.py self-test
gate "a survivor of the mutation tools that is not on the list fails, and so does a listed one that is gone" \
  python3 scripts/survivors.py self-test

echo "2. conformance — each adapter's lines are, byte for byte, the lines the fixtures expect, and each port keeps the contract on generated calls"
# Go is compiled: the build is the adapter's own gate, and the binary is what runs.
gate "the go adapter builds" go build -C go -o ../.bin/go-adapter ./adapter
gate "the rust adapter builds" cargo build --quiet --manifest-path rust/Cargo.toml
# The go shell program carries the contract it lists, so the copy it carries is held to the original.
gate "the go shell program builds" go build -C go -o ../.bin/go-cli ./cli
gate "the contract compiled into the go shell program is spec/contract.json, byte for byte" \
  cmp spec/contract.json go/cli/contract.json
# Both languages here can build every input, so neither may answer "unbuildable".
gate "typescript conforms" python3 scripts/conform.py check --every-input node ts/adapter/adapter.ts
# String hashing is seeded per process; a result that follows set order moves with the seed.
for seed in 0 1 2; do
  gate "python conforms (hash seed $seed)" env PYTHONHASHSEED="$seed" python3 scripts/conform.py check --every-input python3 py/adapter.py
done
# Go's strings are UTF-8 and its integers are 64 bits wide, so it answers
# "unbuildable" for the inputs it cannot be handed; the driver says which.
gate "go conforms" python3 scripts/conform.py check .bin/go-adapter
# Rust's strings are UTF-8, its integers are 64 bits wide, and it has no
# undefined, so it answers "unbuildable" for the same inputs Go cannot be handed.
gate "rust conforms" python3 scripts/conform.py check rust/target/debug/rust-adapter
# The third property of the contract needs every port at once: one line, whoever answers.
gate "the four ports give the same line on every generated call inside the contract" \
  python3 scripts/conform.py identity node ts/adapter/adapter.ts -- python3 py/adapter.py -- .bin/go-adapter -- rust/target/debug/rust-adapter
# A program of the Python port: the same cases through a shell, the answer with its message, the verdict as the exit code.
gate "the python shell program answers every case a shell can make as the fixtures expect, and exits with the worst verdict" \
  python3 scripts/conform.py cli --every-input python3 py/cli.py
# Go's strings cannot spell an unpaired surrogate, so it sits those cases out as its adapter does.
gate "the go shell program answers every case a shell can make as the fixtures expect, and exits with the worst verdict" \
  python3 scripts/conform.py cli .bin/go-cli

echo "3. purity — the modules cannot reach the host, and name nothing that is not a function of its arguments"
gate "typescript modules compile with no host types: ts/tsconfig.json, and ts/host.d.ts is all the host there is" \
  findings node_modules/.bin/tsc -p ts/tsconfig.json
gate "typescript modules name no clock, randomness, locale, code from text, or global object: eslint.config.mjs" \
  findings node_modules/.bin/eslint --max-warnings 0 ts
gate "python modules import and use only what the allowlists hold: scripts/py_purity.py" \
  findings python3 scripts/py_purity.py py/haltrule/*.py
gate "go modules import only what the allowlist holds, which is all a go package can reach: scripts/go_purity.py" \
  findings python3 scripts/go_purity.py
gate "the rust library is no_std, names no std, and declares one dependency: scripts/rs_purity.py" \
  findings python3 scripts/rs_purity.py

echo "4. lint and types"
gate "ruff check" findings ruff check --quiet py scripts
gate "ruff format --check" findings ruff format --check --quiet py scripts
gate "the typescript adapter type-checks" findings node_modules/.bin/tsc -p ts/adapter/tsconfig.json
gate "go vet" findings go vet -C go ./...
gate "gofmt -l" findings gofmt -l go
gate "cargo clippy" findings cargo clippy --quiet --manifest-path rust/Cargo.toml --all-targets -- -D warnings
gate "cargo fmt --check" findings cargo fmt --manifest-path rust/Cargo.toml --all --check

echo
if [ "$failed" -eq 0 ]; then
  echo "all gates passed"
  exit 0
fi
echo "$failed gate(s) failed"
exit 1
