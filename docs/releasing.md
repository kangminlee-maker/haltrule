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

```
git switch main && git pull --ff-only
./scripts/check.sh                                   # the gates
python3 scripts/mutants.py                           # every planted defect is caught
for one in ts py go rust py-cli go-cli; do python3 scripts/survivors.py "$one"; done
./scripts/packages.sh                                # the four packages, built and used from outside
```

The last one is the release's own: it builds each package the way this document says to, installs it
somewhere this repository is not on the path, and makes a real call through it.

## 2. The version, in three files

| File | What carries the version |
|---|---|
| `pyproject.toml` | `version` under `[project]` |
| `ts/package.json` | `version` |
| `rust/Cargo.toml` | `version` under `[workspace.package]` |

Go has no fourth file: its version is a tag, made in step 5.

```
./scripts/packages.sh      # the first gate fails if the three disagree
git commit -am "haltrule 0.0.1"
git push origin main
```

## 3. npm

```
cd ts
npm run build                        # tsc -p tsconfig.build.json, into ts/dist
cp ../README.md ../LICENSE .         # npm reads both from the package directory and nowhere else
npm publish --access public          # asks who you are, if npm does not already know
rm README.md LICENSE
cd ..
```

Both copies are in `.gitignore`, so a forgotten one is never committed; `rm` them anyway, because the next
`npm pack` would otherwise ship a stale README.

## 4. PyPI

```
rm -rf build py/haltrule.egg-info dist
python3 -m pip install --quiet build twine      # in a venv, if the machine's python is managed
python3 -m build --wheel --sdist                # into dist/
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

No copy step here: `rust/haltrule/LICENSE` is a link to the one at the root, and cargo follows it and
ships an ordinary file. Publish from a checkout that makes links — on one that does not, that path is a
13-byte file with a path in it, and `scripts/packages.sh` says so.

## 6. Go, which is a tag

Go has no registry. `go get github.com/kangminlee-maker/haltrule/go@v0.0.1` reads the repository, and the
tag it looks for is the module's directory and the version, in that order:

```
git tag v0.0.1                       # the release
git tag go/v0.0.1                    # the Go module, which lives in go/
git push origin v0.0.1 go/v0.0.1
```

The module proxy fetches it the first time someone asks for it. To be the first to ask:

```
GOPROXY=https://proxy.golang.org go list -m github.com/kangminlee-maker/haltrule/go@v0.0.1
```

## 7. Afterwards

Each package is now installable, so the README's Install section no longer has to say it is not. Remove
the sentence that says the first release is not out, and the status banner at the top if the spec has been
frozen.
