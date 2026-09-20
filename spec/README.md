# haltrule spec (draft)

The spec is the product. An implementation is conformant when it passes every fixture in `../fixtures` and
its output matches the other implementations' byte for byte — in the result-line format
`../fixtures/README.md` defines, so that "the same bytes" does not depend on anyone's JSON library.

The value model and canonicalization below govern digest inputs. Every other argument is an ordinary typed
value of the kinds JSON has — null, a boolean, a number, a string, a list, a map with string keys — and an
integer is an integer however the language holds it. What a language can hold beyond those (JavaScript's
`undefined`, a class instance) is no argument at all; only a digest input is asked about it.

Not every language can hold everything the fixtures hand a part. A string with an unpaired surrogate is an
ordinary value in JavaScript and Python and cannot exist in Rust; an integer wider than 64 bits needs a type
not every standard library has; `undefined` exists in one language only. A port whose types cannot build such
an input says so for that case (`unbuildable`, see `../fixtures/README.md`) and conforms on the rest: its
types have shut out, before it ran, what the others must answer for. Which cases those are is read off the
input itself — an unpaired surrogate in a string or a key, an integer outside a signed 64-bit one, or one of
the `$unsupported` values — so it is not a list anyone keeps, and a port cannot choose what to sit out. Where
the answer is a refusal there is nothing to sit out, because a port that cannot build an argument has refused
it already; that is the rule above, and it comes first. The driver accepts `unbuildable` for the rest, says
how many there were, and holds a port in a language that can build them all to every one.

**Null and not given are the same thing: absent.** A port whose types cannot tell the two apart loses
nothing. An absent field takes its default where it has one — no cap, no bound, nothing used, `concurrent`
off — and nothing else is a way to be absent: `false`, `0` and `""` are values.

An argument outside a part's contract — a string where it expects a number, a negative amount, a map
holding a field the contract does not name (`max_turn` for `max_turns` would otherwise be no cap at all) —
is **refused**: the call fails in the language's own way (an exception, an error value) and changes nothing, so
a refused charge leaves the ledger exactly as it was. A refusal is not a verdict and carries no reason. A
fixture writes it as `{"refused": true}` where the result would be, so every port is held to the same list of
refused arguments. A port whose types cannot hold such an argument at all has refused it before it ran: its
adapter answers `refused` for that case when it cannot build the argument. All four parts
refuse as their bullets below say, before anything else is looked at.

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

A value with more than one thing wrong halts with the first one met walking it in canonical order: depth
first, a list's items in order, a map's members by key as the canonical form sorts them, a key before its
value. `[1.5, 2^53]` is `digest_input_float`, and `{"b": 1.5, "a": 2^53}` is `digest_input_int_range`.

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
column without translation. `budget` and `slot` have nowhere to resume from: their `resume` is always null.

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

  Every argument of the breaker's is refused when it is outside the contract, as the other three parts'
  arguments are. A map argument — a policy, a failure entry — holds the fields its bullet names and no
  others; a number is an integer within ±(2^53 − 1), however the language holds it; an id, a message and a
  class are strings. Absent — null or not given — stands in two places only: a message, which is then no
  text to read, and a policy's `concurrent`, which is then off. Every other field must be there and hold
  its type, a failure entry's class included, where null is a value and means the item's own failure. A
  value of any other type is refused: a boolean where a count belongs, a number where a message belongs. A
  refused report leaves the batch exactly as it was, as a refused charge leaves the ledger.
  - **`classify`** takes a failure message and answers `rate_limit`, `auth`, `transport`, or null — null
    meaning the failure is the item's own and says nothing about the provider. An absent or empty message
    is null; a message that is not a string is refused, `false` and `0` included.

    It is the fallback, not the recommendation. Where a provider answers with fields — an error type, a
    status — classify on those and hand the class to `state` as `failure_class`, which it accepts and does
    not judge: a message's wording is nobody's contract. `classify` is for when a string is all there is,
    which across providers is most of the time.
    Otherwise the message is lowercased by Unicode's full default case conversion
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
  - **`backoff`** is the delay in milliseconds before retry `attempt + 1`: `cap_ms` when `initial_ms` is
    zero or less, otherwise `min(cap_ms, initial_ms × 2^max(0, attempt))`. There is no jitter, because
    randomness cannot live in an output compared byte for byte. This is the delay before the jitter, and
    the caller adds that. The doubling is exact. A port computes it in integers and stops at the cap, so a
    product past its integer type is the cap and never an overflow; the references compute it in doubles,
    where a power of two times an integer in range is exact until it is infinite, which is the same thing.
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
      whose class is null goes to the dead letter, every time it is reported. Any other entry — whatever its
      class says, the empty string included: the part does not judge it — becomes pending unless one with its
      `item_id` already is — the first is kept. Then, if the policy is enabled, the batch
      has not tripped, and the number pending has reached `systemic_threshold`, the batch trips: the trip is
      this entry's `failure_class`, the number pending as `consecutive_item_count`, and the `threshold`. That
      one report answers with the trip; every other report answers null.
    - After the trip nothing leaves the pending entries: a success no longer moves them and a failure still
      joins them. They are the outage's victims, to be dispatched again, not written off. With `concurrent`
      on the same holds before the trip, so which items end where does not depend on the order they finished
      in; the trip's `failure_class` is still that of whichever entry crossed the threshold.

    The completed ids are listed in the order they were reported, success and skipped alike, once per
    report, so an id may be completed twice, and completed and dead-lettered both. The dead letter is in the
    order entries arrived there: an item's own failure when it is reported, a pending entry when a success
    moves it. What is neither at the end is incomplete; the caller works that out from its own list of items.
- `checkpoint` — `canonicalize` and the digest above, and a reuse verdict over one recorded artifact. Its
  arguments, under the names every port and every fixture uses:
  - `stage_id`, a string, the only one required;
  - `artifact`, a map: what was recorded, absent when nothing was;
  - `subject_ref`, a string: how issues refer to the artifact;
  - `expected_contract_revision` and `expected_stage_config_digest`, strings, and
    `expected_dependency_digests`, a map of dependency id to string: what the caller expects now. An absent
    expectation is not checked; any string is compared, the empty one included;
  - `required_resume_from_stage`, a string: where a rerun must start, `stage_id` when absent;
  - `validation_issues`, a list of maps: what the caller's own validation found;
  - `status_map`, a map of the caller's status to one of the four above.

  An argument of another type is refused, and so is a `status_map` value outside the vocabulary. What the
  artifact *records* is data, written by whoever wrote it, and is read leniently instead. A recorded
  `status` or `contract_revision` is absent when it is falsy as JavaScript has it — null, `false`, `0`, NaN,
  `""`; an empty list or map is present. A recorded `dependency_digests` that is not a map records nothing.
  A recorded value equals an expectation only when it is the same string: a recorded `1` is not `"1"`. What
  is recorded beyond what is expected is not looked at.

  The result is a list of issues, never empty. An issue is a map of `stage_id`, `status`, `reason`,
  `required_resume_from_stage` and `subject_ref` (null when absent), and what its reason adds:

  | `status` | `reason` | adds |
  |---|---|---|
  | `missing` | `artifact_missing` | |
  | `invalid` | `artifact_status_missing` | |
  | `invalid` | `artifact_status_not_reusable` | `actual_status` |
  | `unknown_contract` | `contract_revision_missing` | `expected_contract_revision` |
  | `invalid` | `contract_revision_mismatch` | `expected_contract_revision`, `actual_contract_revision` |
  | `invalid` | `stage_config_digest_mismatch` | `expected_stage_config_digest`, `actual_stage_config_digest` |
  | `invalid` | `dependency_digest_mismatch` | `dependency_id`, `expected_digest`, `actual_digest` |
  | `invalid` | `validation_issue` | the caller's fields |
  | `valid` | `checkpoint_valid` | `required_resume_from_stage` is null |

  An `actual_` member is what is recorded, null when nothing is. An absent artifact is one `artifact_missing`
  and nothing else: the expectations and the caller's issues are not looked at. Otherwise every issue found,
  in this order: status, contract revision (missing when none is recorded, a mismatch otherwise),
  stage-config digest, dependency digests by UTF-16 code unit order of their ids, the caller's validation
  issues in the order given; with none, a single `checkpoint_valid`.

  A caller's validation issue becomes an `invalid` issue with reason `validation_issue` and the caller's own
  fields laid over those five defaults — any of them, `status` and `stage_id` included; the part does not
  judge them. A field that is null is absent: the default stands where there is one, and the field is left
  out where there is none. Issue ids and file reads stay with the caller.
- `budget` — a ledger of turns, milliseconds, and tokens against the caps `max_turns`, `time_budget_ms`, and
  `token_budget`, each optional. The caller charges what it measured; `charge` adds it and returns a verdict:
  `warning` naming the first exhausted resource in the order turns, time, tokens (`budget_turns`,
  `budget_time`, `budget_tokens`), or `ok` (`budget_ok`). A resource is exhausted when the amount used
  reaches its cap, so a cap of zero is exhausted before anything is charged, a charge of nothing reports the
  current state, and an exhausted budget stays exhausted. The ledger holds integers up to 2^63 − 1 and its
  additions saturate there. Caps and amounts are non-negative integers (an integral float is its integer);
  anything else — negative, fractional, NaN or an infinity, boolean, a string, past 2^63 − 1 — is refused. An
  absent cap is no cap and an absent amount is nothing used. The caps are a map of those three fields and a
  charge a map of `turns`, `ms` and `tokens`; one that is not a map, or holds any other field, is refused. A
  charge is refused whole: every amount is read before any is added. What to do when exhausted is the
  caller's.
- `slot` — whether a value a person or a model filled in satisfies its contract, a `SlotSpec` with `name`
  and `kind`. `choice` accepts a value equal to one of its `candidates`; `text` accepts a value whose length
  in Unicode scalar values lies within `min_length`..`max_length`, each optional and, when given, an integer in
  0..2^53 − 1 (the limit every language shares, as for digest inputs). A value of null, or a string
  that is empty or holds only ASCII whitespace — U+0020, U+0009, U+000A, U+000B, U+000C and U+000D, and no
  other: U+001C–U+001F, U+0085 and U+00A0 are characters like any — is missing: `warning`, `slot_missing`. A present value that
  fails its contract is `halt`, `slot_invalid`; one that meets it is `ok`, `slot_accepted`. Comparison is
  exact — no trimming, no case folding, no Unicode normalization; a string that is not made of Unicode scalar
  values (it holds a lone surrogate) is `slot_invalid`; a shape beyond length is the caller's to check first,
  as a float's rendering is in a digest. A reference to something that exists is a `choice` whose candidates
  are the known identifiers. A spec outside the contract — one that is not a map, a field beyond those five,
  an unknown `kind`, a `choice` without `candidates`, `candidates` that are not a list of strings,
  `min_length` above `max_length`, a bound that is not an integer in 0..2^53 − 1 (a boolean, a string, NaN,
  or an infinity is not one), a spec without a string `name` — is refused. A field that is given is held to
  its type whatever the `kind`: a `text` with `candidates` that are not strings is refused though it never
  reads them, as a `choice` with a bad bound is.

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
| `validation_issue` | checkpoint | a caller's validation issue whose own `reason` is absent |
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

1. the lines its adapter prints are, byte for byte, the lines the fixtures expect — one line per case, as
   `../fixtures/README.md` defines them, the format's own vectors (`protocol/v0`) included. One driver,
   `../scripts/conform.py`, decides that for every language, and the driver is checked, not trusted: every
   expectation in every fixture is corrupted in turn and must fail under its own id;
2. its dependency list is empty, except SHA-256 where the standard library does not provide it;
3. its modules cannot reach the host and name nothing that is not a function of its arguments — shown by how
   they are built where the language allows it (the TypeScript modules compile with no host types at all; a
   Go package can reach only what it imports, so its import list is the whole of what it can reach; the Rust
   library is `no_std`, which takes the filesystem, the clock and the process out of the language it is
   written in), by the language's mainstream linter for what is left, and by an allowlist where none of that
   exists (Python).

An adapter is the only code a port writes for conformance: it reads the fixtures, calls the port, prints the
lines. It holds no comparison and no expectation.

The fixtures are held to account in turn. A mainstream mutation tool plants defects in each port's modules,
and one that no case notices is either a missing case or code that changes nothing; what is left is listed,
each with its reason, in `../scripts/survivors_accepted.json`.
