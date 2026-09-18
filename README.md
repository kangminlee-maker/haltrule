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

The spec is the product; the implementations are references that prove it is portable. Each implementation
passes the same conformance fixtures, and CI compares their output byte for byte. CI also runs
`scripts/mutants.py`, which breaks one promise at a time — in an implementation, a runner, a fixture, or the
gates themselves — and requires the gates to catch every one.

| Language | Status |
|---|---|
| TypeScript | breaker, checkpoint, budget, slot implemented |
| Python | breaker, checkpoint, budget, slot implemented |
| Go | planned, once the spec freezes |
| Rust | planned, once the spec freezes |

A fifth language will not need a port to interoperate: `canonicalize` is public and its rules are in the
spec, so anything that can run `sha256sum` computes the same digest. CI checks exactly that, with the
system's own `sha256sum` over every canonical form in the fixtures.

## License

MIT
