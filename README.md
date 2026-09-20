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

Every entry point will return one shape, meant to be written straight into your artifact. `budget` and
`slot` return it today; the breaker and `checkpoint` still return the shapes they had in the pipelines they
were lifted from.

```
Verdict { spec, verdict: ok | warning | halt, reason, message, resume }
```

## What this is not

Not a workflow engine. There is no scheduler, no graph, no runner, no UI, no execution history. It is called
*inside* whatever you already run — Airflow, Temporal, LangGraph, Step Functions, a shell loop, your own
runner. If you need an engine, take one of those; this makes the one you have stop at the right moment.

Not a resilience executor either. Libraries like Polly, resilience4j, pybreaker and cockatiel call your
function, sleep for you, and read the wall clock themselves. This one reads nothing and calls nothing.

## Languages

The spec is the product; the implementations are references that prove it is portable. One driver,
`scripts/conform.py`, judges every language the same way: a port's adapter prints one line per fixture case,
and the port conforms when those lines are, byte for byte, the lines the fixtures expect. CI also plants
defects and requires every one to be caught: a mainstream mutation tool per language plants them in the
implementations, and `scripts/mutants.py` plants the ones no tool makes — in an adapter, a fixture, the
driver itself, and the mistakes purity refuses.

| Language | Status |
|---|---|
| TypeScript | breaker, checkpoint, budget, slot implemented |
| Python | breaker, checkpoint, budget, slot implemented |
| Go | planned, once the spec freezes |
| Rust | planned, once the spec freezes |

A fifth language will not need a port to interoperate: `canonicalize` is public and its rules are in the
spec, so anything that can run `sha256sum` computes the same digest. The driver checks exactly that on the
fixtures themselves, with no implementation involved.

## Layout, and adding a port

```
spec/        the contract, in words
fixtures/    the contract, in cases: one JSON file per part, and the line format's own vectors
ts/  py/     a port each: the modules, and an adapter that reads the fixtures and prints one line per case
scripts/     conform.py, the one driver that judges every port; check.sh, the gates; mutants.py, which
             plants the defects no tool makes and requires the gates to catch them; survivors.py, which
             runs a mainstream mutation tool per language and holds what survives to one short list
```

A port is its modules plus an adapter. The adapter holds no expectation and compares nothing, so a new
language adds one line to `scripts/check.sh` for its adapter and one for its own mainstream purity tools.
A language whose types cannot hold some inputs at all — Rust has no string with an unpaired surrogate —
answers `unbuildable` for those cases; the driver knows from the input which they may be, and counts them.

Whether the fixtures would notice a defect in a port is asked by that language's mainstream mutation tool -
StrykerJS for TypeScript, cosmic-ray for Python - with the shared driver as its only test. What survives
must be, entry for entry, `scripts/survivors_accepted.json`, where each entry says why no case can tell it
apart. A message's wording is not part of conformance, so the code that only words a message lives in
`messages.ts` / `messages.py`, which the tools leave alone. A new port adds its tool to
`scripts/survivors.py`.

```
pip install ruff                                     # and, in .venv or on the PATH: pip install cosmic-ray
npm install --no-save typescript@5 @types/node@24 eslint@10 @typescript-eslint/parser@8 @stryker-mutator/core@10
./scripts/check.sh                  # about two seconds
python3 scripts/mutants.py          # every planted defect must fail the gates: half a minute
python3 scripts/survivors.py ts     # about 470 mutants: ten seconds on a laptop
python3 scripts/survivors.py py     # about 730 mutants: over a minute
```

## License

MIT
