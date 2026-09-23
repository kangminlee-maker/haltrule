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
trap 'rm -rf "$work"; rm -rf ts/dist; rm -f ts/README.md ts/LICENSE' EXIT

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
# What README.md's Install table tells a caller to type. A package that goes up
# under another name is a package nobody asked for.
NAME = "haltrule"

python = tomllib.loads(Path("pyproject.toml").read_text())["project"]
npm = json.loads(Path("ts/package.json").read_text())
workspace = tomllib.loads(Path("rust/Cargo.toml").read_text())["workspace"]["package"]
crate = tomllib.loads(Path("rust/haltrule/Cargo.toml").read_text())["package"]

findings: list[str] = []

# The crate's own version is what cargo publishes. `version.workspace = true`
# reads as {"workspace": True} and means the workspace's; anything else in that
# key overrides it, and then the workspace number is not the one that ships.
crate_version = crate.get("version")
if isinstance(crate_version, dict) and crate_version.get("workspace"):
    crate_version = workspace.get("version")

versions = {
    "pyproject.toml": python.get("version"),
    "ts/package.json": npm.get("version"),
    "rust/haltrule/Cargo.toml": crate_version,
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

named = {
    "pyproject.toml": python.get("name"),
    "ts/package.json": npm.get("name"),
    "rust/haltrule/Cargo.toml": crate.get("name"),
}
for where, name in sorted(named.items()):
    if name != NAME:
        findings.append(f"{where} would publish as {name!r}, and the Install table says {NAME!r}")

# The licence a package carries has to be the licence this repository is under,
# and the only way to see that is to read both.
licences = {
    "rust/haltrule/LICENSE": Path("rust/haltrule/LICENSE"),
}
root = Path("LICENSE").read_bytes()
for where, beside in sorted(licences.items()):
    if not beside.exists():
        findings.append(f"{where} is missing, so that package would ship without the licence text")
    elif beside.read_bytes() != root:
        findings.append(f"{where} is not LICENSE: the two have drifted")

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
  "$work/py-build/bin/pip" install --quiet build twine || return 1
  # Both of what PyPI takes. The sdist is the one that can be missing a file:
  # it is built from the tree by rules of its own, and pip builds the wheel
  # again out of whatever it holds.
  "$work/py-build/bin/python" -m build --wheel --sdist --outdir "$work/py-dist" . >/dev/null || return 1
  # What PyPI refuses on sight, before it looks at the code: metadata it cannot
  # read and a description it cannot render. An upload is the only other place
  # this is ever said, and by then the version is spent.
  "$work/py-build/bin/twine" check --strict "$work"/py-dist/* || return 1
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
  # Through the package's own script, because that is what docs/releasing.md
  # runs. Compiling here with the same arguments would leave `npm run build`
  # the one step of the release that nothing has ever executed.
  (cd ts && npm run --silent build) || return 1
  # The two copies docs/releasing.md makes before publishing, made here for the
  # same reason: npm reads a README and a LICENSE from the package directory
  # and from nowhere else, and neither of those lives there. This is the only
  # thing that asks whether that step in the document still works.
  cp README.md LICENSE ts/ || return 1
  (cd ts && npm pack --silent --pack-destination "$work") >/dev/null || return 1
  rm -f ts/README.md ts/LICENSE || return 1
  # The listing is read to the end before anything looks at it: `grep -q` leaves
  # a pipe as soon as it matches, tar dies writing into the closed one, and
  # pipefail then fails the pipeline that had just found what it was looking
  # for. It passed here and failed on CI, which is the only place it was seen.
  tar tzf "$work"/haltrule-*.tgz >"$work/tarball-listing" || return 1
  for carried in package/README.md package/LICENSE; do
    if ! grep -qxF "$carried" "$work/tarball-listing"; then
      echo "the tarball has no $carried, so npm would show the package without one"
      return 1
    fi
  done
  # And nothing else. npm carries a README, a LICENSE and a package.json of its
  # own accord, whatever `files` says - and it reads "a README" loosely enough
  # that ts/README.local.md, which .gitignore holds back precisely because it
  # must not be published, goes out with the package. Measured.
  while read -r member; do
    case "$member" in
      package/dist/* | package/package.json | package/README.md | package/LICENSE) ;;
      *)
        echo "the tarball carries $member, which is not this package"
        return 1
        ;;
    esac
  done <"$work/tarball-listing"
  mkdir -p "$work/ts-use" || return 1
  printf '{"name":"use","private":true,"type":"module"}\n' >"$work/ts-use/package.json"
  cat >"$work/ts-use/use.ts" <<'TS'
import { classifySystemicDispatchFailure } from "haltrule/breaker";
import { Budget } from "haltrule/budget";
import { canonicalize } from "haltrule/checkpoint";
import { validateSlot } from "haltrule/slot";
import { SPEC, type Verdict } from "haltrule/verdict";

// Every subpath the manifest offers, because one left out of the exports map
// resolves to nothing and no other gate here would reach it.
if (classifySystemicDispatchFailure("429 Too Many Requests") !== "rate_limit") throw new Error("breaker");
if (validateSlot({ name: "title", kind: "text" }, "x").verdict !== "ok") throw new Error("slot");
// A value and not only a type: a type import is gone by the time this runs, so
// the verdict subpath's runtime half would answer to nothing without this line.
if (SPEC !== "haltrule/0") throw new Error(SPEC);

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
  # rust/haltrule/LICENSE is a second copy of the one at the root, kept in step
  # by the manifest gate above. npm takes its copy at publish time and cargo
  # cannot: a file cargo ships has to be one git tracks, and a link in its place
  # is refused by the suite that copies the tree (scripts/mutants.py).
  if ! cargo package --list --allow-dirty -p haltrule --manifest-path rust/Cargo.toml \
    >"$work/crate-listing"; then
    echo "cargo would not list the crate's files"
    return 1
  fi
  if ! grep -qxF "LICENSE" "$work/crate-listing"; then
    echo "the crate would ship without a LICENSE"
    return 1
  fi
  # `cargo package`, not `cargo publish --dry-run`: the two build and verify the
  # same tarball, and only one of them is one flag away from an upload. What the
  # dry run checked beyond this - the fields crates.io requires - the manifest
  # gate above reads for itself, which the dry run only warned about anyway.
  # --allow-dirty because this runs on a tree with work in it.
  cargo package --quiet --allow-dirty -p haltrule --manifest-path rust/Cargo.toml
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
