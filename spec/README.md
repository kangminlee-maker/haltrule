# haltrule spec (draft)

The spec is the product. An implementation is conformant when it passes every fixture in `../fixtures` and
its output matches the other implementations' byte for byte.

The value model and canonicalization below govern digest inputs, once `checkpoint` exists. They do not
constrain the breaker, which takes its arguments as ordinary typed values; passing it a string where it
expects a number is out of contract and its behavior there is not defined by any fixture.

**Draft. Nothing below is frozen.** Sections marked TODO are decided but not yet written out.

## Value model

The input to a digest is deliberately narrow, so that four languages can canonicalize it the same way
without a JSON library:

- strings (UTF-8)
- integers, `|n| <= 2^53 - 1` — the limit is JavaScript's, and it is therefore everyone's
- booleans
- null
- lists
- maps with string keys

**Floats are rejected**, not coerced: `halt`, reason `digest_input_float`. An integer outside the range is
rejected the same way. A float that matters to a digest is the caller's to render as a string, where the
rendering is explicit and reviewable.

Canonical form is a subset of RFC 8785: keys sorted by UTF-16 code unit, no insignificant whitespace, the
escape set fixed by fixture.

## Verdict

**Planned.** Every entry point is to return one shape. The breaker currently returns the shapes it had in
the pipeline it came from, and will be adapted once the other parts exist.

```
Verdict {
  spec:    string    # spec version that produced this verdict
  verdict: "ok" | "warning" | "halt"
  reason:  string    # from the reason registry; additions only, never redefinitions
  message: string    # for a person reading the artifact
  resume:  string?   # where the next run should pick up, when that is knowable
}
```

It is JSON by construction, so it can be written into a spreadsheet cell, an artifact field, or a database
column without translation.

## Artifact status vocabulary

**Planned; nothing below is implemented yet.** The library never reads your artifacts. It needs one thing
from them: a status drawn from

```
complete | partial | failed | blocked
```

Your own values map onto these through a `status_map` you pass in. Reuse is permitted only for statuses you
declare reusable — by default, `complete` alone.

## Parts

- `breaker` — TODO: policy fields, classification contract, backoff schedule, dead-letter entry
- `checkpoint` — TODO: digest inputs, reuse verdict, issue list
- `budget` — TODO: counters, exhaustion kinds
- `slot` — TODO: slot kinds (`choice`, `ref`, `text`), validation outcomes

## Reason registry

TODO. One table, additions only. A reason code that ships cannot change meaning, because it will already be
sitting in somebody's artifacts.

## Conformance

A port is conformant when:

1. every fixture passes;
2. corrupting one expected value in a fixture makes the runner fail (the instrument is checked, not trusted);
3. its dependency list is empty, except SHA-256 where the standard library does not provide it;
4. its canonical output for the shared vectors is identical to the other implementations'.
