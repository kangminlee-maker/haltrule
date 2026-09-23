# haltrule

A portable verdict spec for pipelines whose state lives in their artifacts. It halts only when the next
artifact would be contaminated, and warns about everything else.

> **Status: pre-alpha.** The spec is not frozen and no implementation is published yet. Nothing here is
> ready to depend on.

## The rule

A pipeline step asks one question before it acts: *would continuing contaminate what the next step reads?*

Only three answers mean **halt**:

1. **Stale input** — a checkpoint whose inputs no longer match, or an artifact whose status says it is not
   reusable (partial, failed, blocked).
2. **Invalid value recorded** — a judgment that failed its contract but is about to be written to a ledger.
3. **Half-written external state** — a write whose outcome is unknown, so a reader may see a partial one.

Everything else **warns**: an isolated item failure, a dispatch breaker tripping, an exhausted budget. The
work already done stays valid, the work not done is simply not done, and the next run picks it up.

Halting is cheap here because the state is in the artifacts, not in an engine's database. Nothing is lost by
stopping; something is lost by building on top of a bad input.

## What this is

Four pure decision functions. No I/O, no clock, no sleep, no retries executed for you. You pass facts in and
get a `Verdict` back; your loop decides what to do with it.

| Part | Decides |
|---|---|
| `breaker` | whether to keep dispatching, back off, dead-letter an item, or trip |
| `checkpoint` | whether an existing artifact may be reused, from an input digest and the artifact's own status |
| `budget` | whether a run has exhausted its turns, time, or tokens |
| `slot` | whether a value filled in by a person or a model satisfies its contract |

Every verdict is one shape, meant to be written straight into your artifact. A part that answers a
question instead — which failure class, how long to wait, the canonical form — answers with the answer.

```
Verdict { spec, verdict: ok | warning | halt, reason, message, resume }
```

Some reasons name facts of their own — which dependency moved, how many failures crossed the threshold —
and those sit beside the five in the same map.

## Install

Not yet: the first release is not out, and the status above says why. These are the names it will take,
and `docs/releasing.md` is the command that makes it.

| Language | Install | Imports as |
|---|---|---|
| TypeScript | `npm install haltrule` | `import { Budget } from "haltrule/budget"` |
| Python | `pip install haltrule` | `from haltrule.budget import Budget` |
| Go | `go get github.com/kangminlee-maker/haltrule/go` | `import haltrule "github.com/kangminlee-maker/haltrule/go"` |
| Rust | `cargo add haltrule` | `use haltrule::Budget;` |

A package is the library and nothing else: the adapters, the two shell programs and the fixtures stay in
the repository, where the driver needs them. Each part is its own module, as the table above shows, and
there is no root that re-exports them.

The version is the spec's. `spec` answers `haltrule/0`, which is a draft, so every package is `0.x` and
anything may change; the spec freezing at `haltrule/1` is what makes all four `1.0.0` on the same day.

## Try it

The Python port answers from a shell, one entry point of the contract per call: the arguments as a JSON
object under the names `spec/contract.json` gives them, the answer as one line, and the worst verdict in
it as the exit code — 0 ok, 1 warning, 2 halt; 3 when the arguments are outside the contract, 4 when no
call was made. What such a program takes and answers is the spec's, not this program's
(`spec/README.md`, "From a shell"), so a second one in another language is held to the same contract.

```
$ echo '{"message": "429 Too Many Requests"}' | python3 py/cli.py breaker.classify -
"rate_limit"
$ echo '{"args": {"stage_id": "draft", "artifact": null}}' | python3 py/cli.py checkpoint.evaluate -
[{"message":"draft: nothing was recorded","reason":"artifact_missing","resume":"draft","spec":"haltrule/0","stage_id":"draft","subject_ref":null,"verdict":"halt"}]
$ echo "exit $?"
exit 2
```

`python3 py/cli.py` alone lists the entry points and their arguments, read from the contract file;
`breaker.run` takes a function — the contract marks it — and is not among them. The fixtures that hold the
four ports hold this program too (`scripts/conform.py cli`), answer and exit code both.

There is a second one in Go, for a machine with no Python: `go build -C go -o ../.bin/go-cli ./cli` makes a
single file of three and a half megabytes that needs no runtime and carries the contract it lists. It answers the
same calls the same way — the same fixtures hold it — except where Go's strings cannot spell an input at
all, which it says by making no call, as its adapter answers `unbuildable`.

## What this is not

Not a workflow engine. There is no scheduler, no graph, no runner, no UI, no execution history. It is called
*inside* whatever you already run — Airflow, Temporal, LangGraph, Step Functions, a shell loop, your own
runner. If you need an engine, take one of those; this makes the one you have stop at the right moment.

Not a resilience executor either. Libraries like Polly, resilience4j, pybreaker and cockatiel call your
function, sleep for you, and read the wall clock themselves. This one reads nothing and calls nothing.

## Languages

The spec is the product; the implementations are references that prove it is portable. One driver,
`scripts/conform.py`, judges every language the same way: a port's adapter prints one line per fixture case,
and the port conforms when those lines are, byte for byte, the lines the fixtures expect — and when it keeps
the argument contract (`spec/contract.json`) on calls the driver generates from it: every defect refused, no
call inside the contract refused, and the same line from all four ports. CI also plants
defects and requires every one to be caught: a mainstream mutation tool per language plants them in the
implementations, and `scripts/mutants.py` plants the ones no tool makes — in an adapter, a fixture, the
driver itself, and the mistakes purity refuses.

| Language | Status |
|---|---|
| TypeScript | breaker, checkpoint, budget, slot implemented |
| Python | breaker, checkpoint, budget, slot implemented |
| Go | breaker, checkpoint, budget, slot implemented |
| Rust | breaker, checkpoint, budget, slot implemented |

A fifth language will not need a port to interoperate: `canonicalize` is public and its rules are in the
spec, so anything that can run `sha256sum` computes the same digest. The driver checks exactly that on the
fixtures themselves, with no implementation involved.

## Layout, and adding a port

```
spec/        the contract, in words, and contract.json: the argument contract as a rule the driver checks
fixtures/    examples of the spec's sentences: one JSON file per part, and the line format's own vectors
ts/ py/ go/ rust/  a port each: the modules, and an adapter that reads case files and prints one line per case;
             py/cli.py and go/cli are the spec's shell program, in two languages
scripts/     conform.py, the one driver that judges every port, and contract.py, which makes the generated
             calls it judges the contract by; check.sh, the gates; mutants.py, which
             plants the defects no tool makes and requires the gates to catch them; survivors.py, which
             runs a mainstream mutation tool per language and holds what survives to one short list;
             packages.sh, which builds what a release ships and uses each package from outside this tree
docs/        releasing.md: the commands that publish, run by hand, by a person logged in to each registry
```

A port is its modules plus an adapter. The adapter holds no expectation and compares nothing, so a new
language adds one line to `scripts/check.sh` for its adapter and one for its own mainstream purity tools.
A language whose types cannot hold some inputs at all — neither Go nor Rust has a string with an unpaired
surrogate — answers `unbuildable` for those cases; the driver knows from the input which they may be, and
counts them. Both of them sit out the same thirty-one of 539, and neither needed a case of its own.

Purity is whatever each language can be held to by construction rather than by a search through the text:
TypeScript compiles with no host types, Python is held to an import allowlist, a Go package can reach only
what it imports, and the Rust library is `#![no_std]`, which takes the filesystem, the clock and the
process out of the language it is written in. Its one dependency is SHA-256, which is what the spec allows
where a language has none of its own.

Whether the fixtures would notice a defect in a port is asked by that language's mainstream mutation tool -
StrykerJS for TypeScript, cosmic-ray for Python, gremlins for Go, cargo-mutants for Rust - with the shared
driver as its only test. The two shell programs are asked the same question in runs of their own
(`survivors.py py-cli`, `survivors.py go-cli`), whose test is the bridge beside each: it makes every call
the fixtures ask for in the tool's own process, hands the judging to the driver, and then asks the program,
as a process, what it promises beside answering a case - that it reads its arguments from a file and from
the standard input, says what it takes, makes no call it can neither read nor answer, and refuses a call the
contract has no case for. What survives must be, entry for entry, `scripts/survivors_accepted.json`, where
each entry says why no case can tell it apart. A message's wording is not part of conformance, so the code
that only words a message lives in a `messages` module of its own, which the tools leave alone. A new port
adds its tool to `scripts/survivors.py`.

```
pip install ruff                                     # and, in .venv or on the PATH: pip install cosmic-ray
npm install --no-save typescript@5 @types/node@24 eslint@10 @typescript-eslint/parser@8 @stryker-mutator/core@10
GOBIN="$PWD/.bin" go install github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0
curl https://sh.rustup.rs -sSf | sh                  # and then: cargo install cargo-mutants --locked
./scripts/check.sh                  # about three seconds
python3 scripts/mutants.py          # every planted defect must fail the gates: three minutes
python3 scripts/survivors.py ts     # about 530 mutants: ten seconds on a laptop
python3 scripts/survivors.py py     # about 800 mutants: two minutes
python3 scripts/survivors.py go     # about 290 mutants, one at a time: two and a half minutes
python3 scripts/survivors.py rust   # about 380 mutants, one at a time: five minutes
./scripts/packages.sh               # the four packages, built and used from outside: two minutes
```

`packages.sh` is not one of the gates. Every gate runs inside this tree, where the modules are files beside
each other, so none of them would notice a file left out of a manifest, an import the compiler rewrote to a
name that is not there, or a module path that is not where `go get` fetches it. It builds each package the
way `docs/releasing.md` says to, installs it where this repository is not on the path, and makes a real
call through it. It is minutes and `check.sh` runs 138 times in `mutants.py`, which is why it is apart.

## License

MIT
