# Releasing

A release is one version in four places. The version is the spec's: `spec` answers `haltrule/0`, which is
a draft, so every package is `0.x` and anything may change; the spec freezing at `haltrule/1` is what makes
all four `1.0.0`, on the same day. There is no release of one language on its own.

Every command here is run by hand, by a person who is logged in to that registry. Nothing in this
repository holds a token and no workflow publishes anything: the four accounts are a person's, and a
repository that could publish on its own would be a repository that could publish by accident.

A version number is spent once. npm, PyPI and crates.io all refuse a second upload of a version they
already have, and a tag that has been fetched is in the module proxy's cache for good. If a publish fails
halfway through, do not retry that number: raise the patch version, commit, and start again from the top.

## 1. Everything passes

Every block in this document is one `&&` chain on purpose. A step that fails must not let the next one
run, and from step 3 on the next one is an upload that cannot be taken back.

```
export PATH="$PATH:$HOME/.cargo/bin" &&
git switch main && git pull --ff-only &&
./scripts/check.sh &&
python3 scripts/mutants.py &&
(for one in ts py go rust py-cli go-cli; do python3 scripts/survivors.py "$one" || exit 1; done) &&
./scripts/packages.sh
```

`scripts/check.sh` and `scripts/packages.sh` put rustup's directory on their own `PATH` and the shell
keeps none of that, so the `export` is what makes `cargo` in step 5 the same cargo the gates used. Without
it, a machine with rustup but a bare `PATH` reaches step 5 and answers `cargo: command not found` — after
npm and PyPI have already taken the version.

The last command is the release's own: it builds each package the way this document says to, installs it
somewhere this repository is not on the path, and makes a real call through it.

## 2. The version, in three files

| File | What carries the version |
|---|---|
| `pyproject.toml` | `version` under `[project]` |
| `ts/package.json` | `version` |
| `rust/Cargo.toml` | `version` under `[workspace.package]` |

Go has no fourth file: its version is a tag, made in step 6.

```
./scripts/packages.sh &&                             # the first gate fails if the three disagree
version=$(python3 -c 'import tomllib, pathlib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])') &&
echo "$version" &&
git commit -am "haltrule $version" &&
git push origin main
```

Keep that shell for the rest of the document: step 6 tags `$version`, so a release that had to raise its
patch number tags the number it actually published.

## 3. npm

```
(cd ts &&
  npm run build &&                   # tsc -p tsconfig.build.json, into ts/dist
  cp ../README.md ../LICENSE . &&    # npm reads both from the package directory and nowhere else
  npm publish --access public) ;     # asks who you are, if npm does not already know
rm -f ts/README.md ts/LICENSE
```

The `rm` is after a `;` and not a `&&` because it has to run whether the publish worked or not.

Both copies are in `.gitignore`, so a forgotten one is never committed; `rm` them anyway, because the next
`npm pack` would otherwise ship a stale README.

## 4. PyPI

```
rm -rf build py/haltrule.egg-info dist &&
python3 -m pip install --quiet build twine &&   # in a venv, if the machine's python is managed
python3 -m build --wheel --sdist &&             # into dist/
python3 -m twine upload dist/*                  # asks for an API token
```

`build` and `py/haltrule.egg-info` are removed first because setuptools writes through them and does not
empty them: without the `rm`, a file the manifest no longer ships is in the wheel anyway, from the last
time it did.

## 5. crates.io

```
cargo publish -p haltrule --manifest-path rust/Cargo.toml
```

Only the library. `rust/adapter` says `publish = false`: it reads the fixtures and prints one line per
case, which is of no use to anyone who installs the library.

## 6. Go, which is a tag

Go has no registry. `go get github.com/kangminlee-maker/haltrule/go@v0.0.1` reads the repository, and the
tag it looks for is the module's directory and the version, in that order. `$version` is the one step 2
read out of the manifest, so these are the tags for what was actually published:

```
git tag "v$version" &&               # the release
git tag "go/v$version" &&            # the Go module, which lives in go/
git push origin "v$version" "go/v$version"
```

The module proxy fetches it the first time someone asks for it. To be the first to ask:

```
GOPROXY=https://proxy.golang.org go list -m "github.com/kangminlee-maker/haltrule/go@v$version"
```

## 7. Afterwards

Each package is now installable, so the README's Install section no longer has to say it is not. Remove
the sentence that says the first release is not out, and the status banner at the top if the spec has been
frozen.
