#!/usr/bin/env bash
# What a release ships, built and then used from outside the repository. Each
# gate below builds one language's package the way `docs/releasing.md` says to,
# installs it somewhere the repository is not on the path, and makes a real
# call through it. A port that conforms in place can still ship a package that
# imports nothing - a file left out of the manifest, an import the compiler
# rewrote to a name that is not there - and no gate in scripts/check.sh would
# see it, because every one of those runs inside the tree.
#
# This is not in scripts/check.sh: that script runs once per planted defect,
# 138 times in scripts/mutants.py, and none of those defects is in a manifest.
# This one builds four packages and is minutes, so it runs on its own and in CI.
#
#   scripts/packages.sh
set -uo pipefail
cd "$(dirname "$0")/.."
# Where rustup puts cargo, for a shell that has not been told about it.
PATH="$PATH:$HOME/.cargo/bin"

work="$(mktemp -d)"
trap 'rm -rf "$work"; rm -rf ts/dist' EXIT

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

# What a registry reads before it takes a package, and what `go get` resolves.
# The version is the spec's, so it is the same number in every manifest that
# carries one; Go carries none, because its version is a tag on the repository.
the_manifests() {
  python3 - <<'PY'
import json
import sys
import tomllib
from pathlib import Path

REPOSITORY = "https://github.com/kangminlee-maker/haltrule"

python = tomllib.loads(Path("pyproject.toml").read_text())["project"]
npm = json.loads(Path("ts/package.json").read_text())
workspace = tomllib.loads(Path("rust/Cargo.toml").read_text())["workspace"]["package"]
crate = tomllib.loads(Path("rust/haltrule/Cargo.toml").read_text())["package"]

findings: list[str] = []

versions = {
    "pyproject.toml": python.get("version"),
    "ts/package.json": npm.get("version"),
    "rust/Cargo.toml": workspace.get("version"),
}
if None in versions.values() or len(set(versions.values())) != 1:
    for name, version in sorted(versions.items()):
        findings.append(f"{name} says version {version!r}")

said = {
    "pyproject.toml description": python.get("description"),
    "pyproject.toml license": python.get("license"),
    "ts/package.json description": npm.get("description"),
    "ts/package.json license": npm.get("license"),
    "rust/haltrule/Cargo.toml description": crate.get("description"),
    "rust/Cargo.toml license": workspace.get("license"),
}
for what, value in sorted(said.items()):
    if not value:
        findings.append(f"{what} is missing, and a registry will not take a package without it")

points = {
    "pyproject.toml": (python.get("urls") or {}).get("Repository"),
    "ts/package.json": (npm.get("repository") or {}).get("url"),
    "rust/Cargo.toml": workspace.get("repository"),
}
for name, where in sorted(points.items()):
    if not where or REPOSITORY not in where:
        findings.append(f"{name} points at {where!r} and not at {REPOSITORY}")

# `go get <import path>` is the whole of Go's publishing, so the import path is
# the one thing that has to be where the module is fetched from. It is read
# from the first line and nowhere else, because gremlins reads only that line:
# a comment above it left the tool with a sentence for a module path, and it
# then called all 313 of the Go mutants ones no run reaches.
first, _, _ = Path("go/go.mod").read_text().partition("\n")
fetched = REPOSITORY.removeprefix("https://") + "/go"
if not first.startswith("module "):
    findings.append(f"go/go.mod starts with {first!r} and not with its module line")
elif first[len("module ") :].strip() != fetched:
    findings.append(f"go/go.mod declares {first!r}, but `go get` fetches it as {fetched!r}")

for finding in findings:
    print(finding)
sys.exit(1 if findings else 0)
PY
}

python_package() {
  # setuptools builds through build/ and py/haltrule.egg-info/ and does not
  # empty them, so a file the manifest no longer ships is still in the wheel
  # from the last time it did. Measured: py.typed, deleted from the tree, came
  # out of the wheel anyway and this gate called the package fine.
  rm -rf build py/haltrule.egg-info || return 1
  # A venv of its own, because the wheel is built by `build` and read by `pip`,
  # and neither is something this repository asks a machine to already have.
  python3 -m venv "$work/py-build" || return 1
  "$work/py-build/bin/pip" install --quiet build || return 1
  # Both of what PyPI takes. The sdist is the one that can be missing a file:
  # it is built from the tree by rules of its own, and pip builds the wheel
  # again out of whatever it holds.
  "$work/py-build/bin/python" -m build --wheel --sdist --outdir "$work/py-dist" . >/dev/null || return 1
  for built in "$work"/py-dist/*.whl "$work"/py-dist/*.tar.gz; do
    rm -rf "$work/py-venv" || return 1
    python3 -m venv "$work/py-venv" || return 1
    "$work/py-venv/bin/pip" install --quiet "$built" || return 1
    "$work/py-venv/bin/python" - <<'PY' || return 1
from pathlib import Path

from haltrule import budget as where
from haltrule.budget import Budget
from haltrule.checkpoint import canonicalize
from haltrule.slot import validate_slot

assert (Path(where.__file__).parent / "py.typed").exists(), "py.typed is not in the package"
answer = Budget(max_turns=1).charge(turns=1)
assert answer["reason"] == "budget_turns", answer
assert validate_slot("x", name="title", kind="text")["verdict"] == "ok"
assert canonicalize({"b": 1, "a": [1, 2]})["canonical"] == '{"a":[1,2],"b":1}'
PY
  done
}

npm_tarball() {
  local here="$PWD"
  rm -rf ts/dist
  node_modules/.bin/tsc -p ts/tsconfig.build.json || return 1
  (cd ts && npm pack --silent --pack-destination "$work") >/dev/null || return 1
  mkdir -p "$work/ts-use" || return 1
  printf '{"name":"use","private":true,"type":"module"}\n' >"$work/ts-use/package.json"
  cat >"$work/ts-use/use.ts" <<'TS'
import { classifySystemicDispatchFailure } from "haltrule/breaker";
import { Budget } from "haltrule/budget";
import { canonicalize } from "haltrule/checkpoint";
import { validateSlot } from "haltrule/slot";
import type { Verdict } from "haltrule/verdict";

// Every subpath the manifest offers, because one left out of the exports map
// resolves to nothing and no other gate here would reach it.
if (classifySystemicDispatchFailure("429 Too Many Requests") !== "rate_limit") throw new Error("breaker");
if (validateSlot({ name: "title", kind: "text" }, "x").verdict !== "ok") throw new Error("slot");

const answer: Verdict = new Budget({ max_turns: 1 }).charge({ turns: 1 });
if (answer.reason !== "budget_turns") throw new Error(JSON.stringify(answer));
const canonical = canonicalize({ b: 1, a: [1, 2] });
if (!("canonical" in canonical) || canonical.canonical !== '{"a":[1,2],"b":1}') {
  throw new Error(JSON.stringify(canonical));
}
TS
  # nodenext is how a package outside this repository resolves "haltrule/budget":
  # through the exports map in ts/package.json, types and all.
  printf '{"compilerOptions":{"module":"nodenext","moduleResolution":"nodenext","strict":true,"noEmit":true,"types":[]},"include":["use.ts"]}\n' >"$work/ts-use/tsconfig.json"
  (
    cd "$work/ts-use" || exit 1
    npm install --silent --no-audit --no-fund "$work"/haltrule-*.tgz || exit 1
    "$here/node_modules/.bin/tsc" -p tsconfig.json || exit 1
    node use.ts
  )
}

go_module() {
  mkdir -p "$work/go-use" || return 1
  cat >"$work/go-use/go.mod" <<MOD
module use

go 1.22

require github.com/kangminlee-maker/haltrule/go v0.0.0

replace github.com/kangminlee-maker/haltrule/go => $PWD/go
MOD
  cat >"$work/go-use/main.go" <<'GO'
package main

import (
	"fmt"
	"os"

	haltrule "github.com/kangminlee-maker/haltrule/go"
)

func main() {
	classified := haltrule.ClassifySystemicDispatchFailure("429 Too Many Requests")
	if classified == nil || *classified != "rate_limit" {
		fmt.Fprintln(os.Stderr, "the module answered something else")
		os.Exit(1)
	}
}
GO
  (cd "$work/go-use" && go build -o use ./... && ./use)
}

rust_crate() {
  # --dry-run is the whole of what crates.io would refuse: the metadata it
  # requires, the files the manifest would ship, and a build of those files
  # alone. --allow-dirty because this runs on a tree with work in it.
  cargo publish --dry-run --allow-dirty -p haltrule --manifest-path rust/Cargo.toml
}

echo "== the packages =="
gate "the manifests say what a registry reads, and one version, which is the spec's" the_manifests
gate "the python wheel and sdist each install into a bare venv and answer" python_package
gate "the npm tarball installs into a bare project, type-checks and answers" npm_tarball
gate "the go module builds from its import path outside this tree and answers" go_module
gate "the rust crate is what crates.io would take" rust_crate

if [ "$failed" -eq 0 ]; then
  echo "every package builds and was used from outside"
  exit 0
fi
echo "$failed package(s) failed"
exit 1
