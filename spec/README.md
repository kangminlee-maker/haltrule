# haltrule spec (draft)

The spec is the product. An implementation is conformant when it passes every fixture in `../fixtures` and
its output matches the other implementations' byte for byte — in the result-line format
`../fixtures/README.md` defines, so that "the same bytes" does not depend on anyone's JSON library.

The value model and canonicalization below govern digest inputs. They do not constrain the breaker,
`budget`, or `slot`, which take their arguments as ordinary typed values. An argument outside a part's
contract — a string where it expects a number — cannot be a fixture case, because a raise is not an
expectation a fixture can hold: `budget` and `slot` refuse theirs with an exception, as their bullets below
say and the gates probe directly in both languages; what the breaker does with one is not defined.

**Draft. Nothing below is frozen.** Sections marked TODO are decided but not yet written out.

## Value model

The input to a digest is deliberately narrow, so that four languages can canonicalize it the same way
without a JSON library:

- strings of Unicode scalar values (an unpaired surrogate is not one)
- integers, `|n| <= 2^53 - 1` — the limit is JavaScript's, and it is therefore everyone's
- booleans
- null
- lists
- maps with string keys

Lists and maps nest at most 100 deep — a bound every language reaches without exhausting its stack, so all of
them halt at the same depth instead of each crashing at its own.

A number is an integer when its value is integral, however the language stores it: `1.0` is `1`, because
JavaScript cannot tell them apart, and a JavaScript bigint in range is the same integer as a number. **Floats are rejected**, not coerced — a fractional value, NaN, or an
infinity halts with reason `digest_input_float`; an integral value outside the range halts with
`digest_input_int_range`. A float that matters to a digest is the caller's to render as a string, where the
rendering is explicit and reviewable. Anything else — an unpaired surrogate, a non-string map key, nesting
deeper than 100, a value of any other type — halts with `digest_input_unsupported`.

## Canonical form and digest

Canonical form is a subset of RFC 8785:

- no insignificant whitespace;
- map keys sorted by UTF-16 code unit, not by code point — the two disagree when a key above U+FFFF meets
  one in U+E000–U+FFFF;
- integers in decimal, with no exponent and no sign on zero;
- strings quoted, with exactly seven two-character escapes (`\"` `\\` `\b` `\f` `\n` `\r` `\t`), every other
  code point below U+0020 as `\u00xx` in lowercase hex, and everything else — `/`, `<>&`, U+007F, U+2028,
  U+2029, all non-ASCII — written as itself;
- no Unicode normalization: the NFC and NFD spellings of a string canonicalize differently.

The digest is `sha256:` followed by the lowercase hex SHA-256 of the canonical form's UTF-8 bytes, so
`printf '%s' "$canonical" | sha256sum` computes it without any implementation. A string is always a value:
its digest covers the quoted, escaped form. A digest of raw text is the caller's to compute.

## Verdict

Every entry point is to return one shape; `budget` and `slot` return it today. The breaker and `checkpoint`
still return the shapes they had in the pipelines they came from: `checkpoint` returns its issue list, and
`canonicalize` and the digest return either their result or `{halt, message}` with a `digest_input_*`
reason. None of them raises for a verdict; `message` is for people and is not part of conformance, so a
fixture's expected verdict omits it.

```
Verdict {
  spec:    string    # spec version that produced this verdict: "haltrule/0" while the spec is a draft
  verdict: "ok" | "warning" | "halt"
  reason:  string    # from the reason registry; additions only, never redefinitions
  message: string    # for a person reading the artifact
  resume:  string?   # where the next run should pick up, when that is knowable; null when it is not
}
```

It is JSON by construction, so it can be written into a spreadsheet cell, an artifact field, or a database
column without translation.

## Artifact status vocabulary

The library never reads your artifacts. It needs one thing from them: a status drawn from

```
complete | partial | failed | blocked
```

Your own values map onto these through a `status_map` you pass in, which replaces the default map of each
of the four onto itself. Reuse is permitted only for a status that maps to `complete`; a status the map does
not name is not reusable.

## Parts

- `breaker` — the policy of a dispatch loop's circuit breaker: which failures say the provider is down, how
  long to wait before a retry, and when a batch should stop. It holds no clock and does no waiting; the loop,
  the retries and what is persisted are the caller's. Its numbers are integers within ±(2^53 − 1). The three
  fixture sections are its three entry points.
  - **`classify`** takes a failure message and answers `rate_limit`, `auth`, `transport`, or null — null
    meaning the failure is the item's own and says nothing about the provider. A message that is not a
    non-empty string is null. Otherwise the message is lowercased by Unicode's full default case conversion
    (U+212A KELVIN SIGN becomes `k`; U+0130 becomes `i` followed by U+0307, so it is not the `i` inside a
    pattern) and the classes are tried in that order: the first with a pattern occurring anywhere in the
    lowercased message wins. A pattern is a plain substring — `429` matches inside `14290`.
    - `rate_limit`: `429`, `rate limit`, `limit reached`, `rate_limit`, `too many requests`, `overloaded`,
      `selected model is at capacity`, `session limit`, `usage limit`, `quota`, `retry-after`, `retry_after`
    - `auth`: `401`, `403`, `unauthorized`, `forbidden`, `invalid api key`, `invalid x-api-key`,
      `authentication`, `not logged in`
    - `transport`: `stream disconnected before completion`, `connection reset by peer`,
      `error sending request`, `failed to connect to websocket`, `transport channel closed`,
      `http/request failed`, `request failed after`, `timed out`, `timeout`, `econnrefused`, `econnreset`,
      `etimedout`, `socket hang up`, `fetch failed`
  - **`backoff`** is the delay in milliseconds before retry `attempt + 1`, with no jitter: `cap_ms` when
    `initial_ms` is zero or less, otherwise `min(cap_ms, initial_ms × 2^max(0, attempt))`. The doubling is
    exact. A port computes it in integers and stops at the cap, so a product past its integer type is the cap
    and never an overflow; the references compute it in doubles, where a power of two times an integer in
    range is exact until it is infinite, which is the same thing.
  - **`state`** is one batch. It is made from a policy — `enabled`, `systemic_threshold` (an integer of at
    least 1), `concurrent` (optional, off by default), and `per_call_max_attempts`, `backoff_initial_ms` and
    `backoff_cap_ms`, which are carried for the caller's loop and read by nothing here — and holds four
    things: the completed item ids, the dead-letter entries, the pending entries, and the trip, null until it
    is set. The caller reports each item's final outcome, after its own retries:
    - `success`: the id is completed. Then, unless the batch has tripped or `concurrent` is on, every pending
      entry moves to the dead letter in the order it became pending — the provider answered, so those
      failures were the items' own.
    - `skipped`: the id is completed and nothing else changes. An item that made no call proves nothing about
      the provider.
    - `failure`, with an entry of `item_id`, `failure_class`, `failure_message` and `attempt_count`: an entry
      whose class is null goes to the dead letter, every time it is reported. Any other entry becomes pending
      unless one with its `item_id` already is — the first is kept. Then, if the policy is enabled, the batch
      has not tripped, and the number pending has reached `systemic_threshold`, the batch trips: the trip is
      this entry's `failure_class`, the number pending as `consecutive_item_count`, and the `threshold`. That
      one report answers with the trip; every other report answers null.
    - After the trip nothing leaves the pending entries: a success no longer moves them and a failure still
      joins them. They are the outage's victims, to be dispatched again, not written off. With `concurrent`
      on the same holds before the trip, so which items end where does not depend on the order they finished
      in; the trip's `failure_class` is still that of whichever entry crossed the threshold.

    An id may be completed twice, and completed and dead-lettered both. What is neither at the end is
    incomplete; the caller works that out from its own list of items.
- `checkpoint` — `canonicalize` and the digest above, and a reuse verdict over one recorded artifact. The
  caller passes the artifact (or null when it does not exist), the stage it belongs to, what it expects
  now — contract revision, stage-config digest, dependency digests — and optionally its own validation
  issues and `status_map`. The result is a list of issues, never empty, in this order: status, contract
  revision, stage-config digest, dependency digests by UTF-16 key order, the caller's validation issues; with
  none, a single `checkpoint_valid`. "Absent" follows JavaScript falsiness (null, `false`, `0`, `""`), and
  values compare with JavaScript `===`. Issue ids and file reads stay with the caller.
- `budget` — a ledger of turns, milliseconds, and tokens against the caps `max_turns`, `time_budget_ms`, and
  `token_budget`, each optional. The caller charges what it measured; `charge` adds it and returns a verdict:
  `warning` naming the first exhausted resource in the order turns, time, tokens (`budget_turns`,
  `budget_time`, `budget_tokens`), or `ok` (`budget_ok`). A resource is exhausted when the amount used
  reaches its cap, so a cap of zero is exhausted before anything is charged, a charge of nothing reports the
  current state, and an exhausted budget stays exhausted. The ledger holds integers up to 2^63 − 1 and its
  additions saturate there. Caps and amounts are non-negative integers (an integral float is its integer);
  anything else — negative, fractional, NaN or an infinity, boolean, a string, past 2^63 − 1 — is refused with
  an exception. What to do when exhausted is the caller's.
- `slot` — whether a value a person or a model filled in satisfies its contract, a `SlotSpec` with `name`
  and `kind`. `choice` accepts a value equal to one of its `candidates`; `text` accepts a value whose length
  in Unicode scalar values lies within `min_length`..`max_length`, each optional and, when given, an integer in
  0..2^53 − 1 (the limit every language shares, as for digest inputs). A value of null, or a string
  that is empty or holds only ASCII whitespace, is missing: `warning`, `slot_missing`. A present value that
  fails its contract is `halt`, `slot_invalid`; one that meets it is `ok`, `slot_accepted`. Comparison is
  exact — no trimming, no case folding, no Unicode normalization; a string that is not made of Unicode scalar
  values (it holds a lone surrogate) is `slot_invalid`; a shape beyond length is the caller's to check first,
  as a float's rendering is in a digest. A reference to something that exists is a `choice` whose candidates
  are the known identifiers. A spec outside the contract — an unknown `kind`, a `choice` without
  `candidates`, `min_length` above `max_length`, a bound that is not an integer in 0..2^53 − 1 (a boolean, a
  string, NaN, or an infinity is not one), a spec without a string `name` — is refused with an exception.

## Reason registry

One table, additions only. A reason code that ships cannot change meaning, because it will already be
sitting in somebody's artifacts. The breaker's reasons are not registered yet.

| Reason | Part | Meaning |
|---|---|---|
| `digest_input_float` | checkpoint | a digest input holds a fractional, NaN, or infinite number |
| `digest_input_int_range` | checkpoint | a digest input holds an integer outside ±(2^53 − 1) |
| `digest_input_unsupported` | checkpoint | a digest input holds anything else outside the value model, or nests deeper than 100 |
| `artifact_missing` | checkpoint | the artifact does not exist |
| `artifact_status_missing` | checkpoint | the artifact has no status |
| `artifact_status_not_reusable` | checkpoint | the artifact's status does not map to `complete` |
| `contract_revision_missing` | checkpoint | a contract revision is expected and the artifact records none |
| `contract_revision_mismatch` | checkpoint | the recorded contract revision differs from the expected one |
| `stage_config_digest_mismatch` | checkpoint | the recorded stage-config digest differs from the expected one |
| `dependency_digest_mismatch` | checkpoint | a recorded dependency digest differs from the expected one, or is absent |
| `validation_issue` | checkpoint | a caller's validation issue that named no reason of its own |
| `checkpoint_valid` | checkpoint | nothing above applies; the artifact may be reused |
| `budget_ok` | budget | every capped resource is below its cap |
| `budget_turns` | budget | the turns used reached `max_turns` |
| `budget_time` | budget | the milliseconds used reached `time_budget_ms` |
| `budget_tokens` | budget | the tokens used reached `token_budget` |
| `slot_accepted` | slot | the value meets the slot's contract |
| `slot_missing` | slot | no value, or a blank one |
| `slot_invalid` | slot | a present value that fails the slot's contract |

## Conformance

A port is conformant when:

1. every fixture passes;
2. corrupting one expected value in a fixture makes the runner fail (the instrument is checked, not trusted);
3. its dependency list is empty, except SHA-256 where the standard library does not provide it;
4. its canonical output for the shared vectors is identical to the other implementations': its runner
   writes one result line per case, as `../fixtures/README.md` defines them, and passes that format's own
   vectors (`protocol/v0`).
