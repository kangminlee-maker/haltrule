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
fixture writes it as `{"refused": true}` where the result would be. A port whose types cannot hold such an
argument at all has refused it before it ran: its adapter answers `refused` for that case when it cannot
build the argument. Every part refuses before anything else is looked at.

What is inside each part's contract — which arguments a call takes, what each must be, which combinations
are refused — is written once, in `contract.json`, and the bullets below say what a part does with a call
inside it. A rule over every call is not held by a list of examples, so the driver checks the contract as a
rule: from `contract.json` and a fixed seed it makes calls inside the contract and calls with one defect
each, and every port must refuse each defect, refuse no call inside the contract, and give every such call
the same line as the other three. The fixtures are examples of the sentences here, with answers written by
hand; they are not the proof of the contract, and they are not added to for its sake.

**Draft. Nothing below is frozen.** Sections marked TODO are decided but not yet written out.

## Value model

The input to a digest is deliberately narrow, so that four languages can canonicalize it the same way
without a JSON library:

- strings of Unicode scalar values (an unpaired surrogate is not one)
- integers, `|n| <= 2^53 - 1` — the widest run of whole numbers an IEEE 754 double holds with none
  missing, which is the range RFC 8785 asks of a number meant to be read as an integer
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
rendering is explicit and reviewable. So is an integer past the range, and RFC 8785 asks for the same
thing: "numbers that do not have a natural place in the current JSON ecosystem MUST be wrapped using the
JSON string type". That is how a 64-bit integer already travels wherever it has to cross languages — a
nanosecond time and a 64-bit identifier alike. A string of digits is a value like any other here: it
canonicalizes to itself quoted, and nothing downstream can round it. Anything else — an unpaired
surrogate, a non-string map key, nesting deeper than 100, a value of any other type — halts with
`digest_input_unsupported`.

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

Either answers with a canonical form, or with a digest, or with the `halt` verdict below; the halt adds
nothing to the five fields, and where in the value the trouble is sits in its message, because a path is
not a thing two languages spell alike.

## Verdict

Every verdict a part reaches is one shape. A part that answers a question instead of reaching a verdict
answers with the answer — `classify` with a class, `backoff` with a number, `canonicalize` with the
canonical form — and one that reaches no verdict this time answers null, as the breaker's `state` does for
a report that does not trip the batch. None of them raises for a verdict; `message` is for people and is
not part of conformance, so a fixture's expected verdict omits it.

```
Verdict {
  spec:    string    # spec version that produced this verdict: "haltrule/0" while the spec is a draft
  verdict: "ok" | "warning" | "halt"
  reason:  string    # from the reason registry; additions only, never redefinitions
  message: string    # for a person reading the artifact
  resume:  string?   # where the next run should pick up, when that is knowable; null when it is not
}
```

Some reasons name facts of their own — which dependency moved, what the artifact records, how many
failures crossed the threshold — and those sit beside the five in the same map, under the names the part's
own table gives them. A reader who knows nothing of the part still reads the five.

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
  long to wait before a retry, and when a batch should stop — and `run`, the sequential loop that asks those
  three in that order. None of it holds a clock or waits: `run` is handed its `sleep`, and what is persisted
  is the caller's. Its numbers are integers within ±(2^53 − 1). The four fixture sections are its four entry
  points.

  Its arguments are in `contract.json`. Two of its fields mean something by being absent — `classify`'s
  message, which is then no text to read, and a policy's `concurrent`, which is then off — and one by being
  null: a failure entry's class, where null means the item's own failure. A failure entry's message is a
  string, the empty one when there is nothing to say. A refused report leaves the batch exactly as it was,
  as a refused charge leaves the ledger.
  - **`classify`** takes a failure message and answers `rate_limit`, `auth`, `transport`, or null — null
    meaning the failure is the item's own and says nothing about the provider. An absent or empty message
    is null.

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
  - **`state`** is one batch. It is made from a policy — `enabled`, `systemic_threshold`, `concurrent`, and
    `per_call_max_attempts`, `backoff_initial_ms` and `backoff_cap_ms`, which `state` does not read: they
    are `run`'s — and holds four things: the completed item ids, the dead-letter entries, the pending entries, and the trip, null until it
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
      has not tripped, and the number pending has reached `systemic_threshold`, the batch trips. The trip is a
      `warning` verdict, reason `breaker_tripped` — a `warning` and not a `halt`, because nothing it leaves
      behind is contaminated: what completed stays valid and what is pending is dispatched again — adding this entry's `failure_class`, the number pending as
      `consecutive_item_count`, and the `threshold`; its `resume` is null, because what a next run picks up
      is the pending entries and not a place. That one report answers with the trip; every other report
      answers null.
    - After the trip nothing leaves the pending entries: a success no longer moves them and a failure still
      joins them. They are the outage's victims, to be dispatched again, not written off. With `concurrent`
      on the same holds before the trip, so which items end where does not depend on the order they finished
      in; the trip's `failure_class` is still that of whichever entry crossed the threshold.

    The completed ids are listed in the order they were reported, success and skipped alike, once per
    report, so an id may be completed twice, and completed and dead-lettered both. The dead letter is in the
    order entries arrived there: an item's own failure when it is reported, a pending entry when a success
    moves it. What is neither at the end is incomplete: `run` lists it from the items it was given, and a
    caller with a loop of its own works it out from its own list.
  - **`run`** is the loop, sequential: one batch made from the policy, and the item ids in the order given,
    each through the caller's `call`, which answers `success`, `skipped`, or `failure` with a message and a
    class — the provider's when it answers with fields, `classify`'s when a string is all there is, null for
    the item's own failure. A success or a skip is reported as such and the next item begins. A failure
    counts one call; while the calls made are fewer than `per_call_max_attempts` and the class is not null,
    the loop waits `sleep(backoff(calls − 1, backoff_initial_ms, backoff_cap_ms))` and calls again — so a
    `per_call_max_attempts` of one or less is one call, and an item's own failure is final at once.
    Otherwise the failure is reported with the calls made as its `attempt_count`. Before each item the loop
    asks whether the batch has tripped, and stops there when it has: the trip is the answer, and nothing
    after it is called. A policy or an item outside the contract — `items` is a list of strings — is refused
    before any call is made. What `run` leaves is the batch's completed ids and dead letter, its trip or
    null, and `incomplete`: the ids given, in their order, that are neither completed nor dead-lettered —
    the outage's victims and what was never dispatched, to be dispatched again. A delay of zero or less is
    still handed to `sleep`: what a delay that is no delay means is the caller's. A `call` that raises is the
    caller's bug and propagates; it is not a failure the batch should hear about. A concurrent pool is not
    this loop: it drives `state` with `concurrent` on and its own lock, an idiom of thirty lines the spec
    does not own.
- `checkpoint` — `canonicalize` and the digest above, and a reuse verdict over one recorded artifact. Its
  arguments (`contract.json`), under the names every port and every fixture uses:
  - `stage_id`, the only one required;
  - `artifact`: what was recorded, absent when nothing was;
  - `subject_ref`: how issues refer to the artifact;
  - `expected_contract_revision`, `expected_stage_config_digest` and `expected_dependency_digests`, by
    dependency id: what the caller expects now. An absent expectation is not checked; any string is
    compared, the empty one included;
  - `required_resume_from_stage`: where a rerun must start, `stage_id` when absent;
  - `validation_issues`: what the caller's own validation found;
  - `status_map`: the caller's statuses, each mapped to one of the four above.

  What the artifact *records* is data, written by whoever wrote it, and is read leniently. A recorded
  `status` or `contract_revision` is absent when it is falsy as JavaScript has it — null, `false`, `0`, NaN,
  `""`; an empty list or map is present. A recorded `dependency_digests` that is not a map records nothing.
  A recorded value equals an expectation only when it is the same string: a recorded `1` is not `"1"`. What
  is recorded beyond what is expected is not looked at.

  The result is a list of verdicts, never empty. Each holds the five fields of a verdict, with
  `resume` carrying where a rerun must start and `subject_ref` and `stage_id` beside them
  (`subject_ref` is null when absent), and what its reason adds:

  | `verdict` | `reason` | adds |
  |---|---|---|
  | `halt` | `artifact_missing` | |
  | `halt` | `artifact_status_missing` | |
  | `halt` | `artifact_status_not_reusable` | `actual_status` |
  | `halt` | `contract_revision_missing` | `expected_contract_revision` |
  | `halt` | `contract_revision_mismatch` | `expected_contract_revision`, `actual_contract_revision` |
  | `halt` | `stage_config_digest_mismatch` | `expected_stage_config_digest`, `actual_stage_config_digest` |
  | `halt` | `dependency_digest_mismatch` | `dependency_id`, `expected_digest`, `actual_digest` |
  | `halt` | `validation_issue` | the caller's fields |
  | `ok` | `checkpoint_valid` | |

  An `ok` verdict has no resume, so `resume` is null wherever the verdict is `ok`, whoever set it. What
  checkpoint reaches on its own is `halt` or `ok`: whether the artifact may be reused is the whole
  question, and what is wrong with it is the reason's to say — the four statuses this list used to carry
  said the same thing twice. A caller's own issue may also be `warning`, which is that caller's reading of
  what it found and not the part's.

  An `actual_` member is what is recorded, null when nothing is. An absent artifact is one `artifact_missing`
  and nothing else: the expectations and the caller's issues are not judged — refused first where they are
  outside the contract, as every argument is, and not read otherwise. Otherwise every issue found,
  in this order: status, contract revision (missing when none is recorded, a mismatch otherwise),
  stage-config digest, dependency digests by UTF-16 code unit order of their ids, the caller's validation
  issues in the order given; with none, a single `checkpoint_valid`.

  A caller's validation issue becomes a `halt` verdict with reason `validation_issue` and the caller's own
  fields laid over those defaults. A field the verdict itself names is held to what the verdict promises
  for it, and `spec` is refused outright: a caller may disagree with a verdict, not sign one. Every other
  field is the caller's own and is not judged — a slot name, a count, a list. A field that is null is
  absent: the default stands where there is one, and the field is left out where there is none. Issue ids
  and file reads stay with the caller.
- `budget` — a ledger of turns, milliseconds, and tokens against the caps `max_turns`, `time_budget_ms`, and
  `token_budget`, each optional. The caller charges what it measured; `charge` adds it and returns a verdict:
  `warning` naming the first exhausted resource in the order turns, time, tokens (`budget_turns`,
  `budget_time`, `budget_tokens`), or `ok` (`budget_ok`). A resource is exhausted when the amount used
  reaches its cap, so a cap of zero is exhausted before anything is charged, a charge of nothing reports the
  current state, and an exhausted budget stays exhausted. The ledger holds integers up to 2^63 − 1 and its
  additions saturate there. The caps and a charge (`turns`, `ms`, `tokens`) are in `contract.json`; an
  absent cap is no cap and an absent amount is nothing used. A charge is refused whole: every amount is
  read before any is added. What to do when exhausted is the caller's.
- `slot` — whether a value a person or a model filled in satisfies its contract, a `SlotSpec` with `name`
  and `kind` (`contract.json`). `choice` accepts a value equal to one of its `candidates`; `text` accepts a
  value whose length in Unicode scalar values lies within `min_length`..`max_length`, each optional; `score`
  accepts a number within `min`..`max`, both included, each optional. A number here is a finite double, and
  an integral one is an integer within ±(2^53 − 1), as every other number in this spec is; every double
  past 2^53 − 1 is integral, so the two tests are one, a double within ±(2^53 − 1). A value that is not one
  — a string that is not blank, a boolean, NaN, an infinity, an integer past that range — fails its
  contract. The part does nothing to a score but compare it. Parsing and comparing a double come out the
  same in every language, and rounding and arithmetic do not, so a caller who rounds, sums or weights a
  score does it first, where it can be reviewed. A value of null, or a string
  that is empty or holds only ASCII whitespace — U+0020, U+0009, U+000A, U+000B, U+000C and U+000D, and no
  other: U+001C–U+001F, U+0085 and U+00A0 are characters like any — is missing: `warning`, `slot_missing`. A present value that
  fails its contract is `halt`, `slot_invalid`; one that meets it is `ok`, `slot_accepted`. Comparison is
  exact — no trimming, no case folding, no Unicode normalization; a string that is not made of Unicode scalar
  values (it holds a lone surrogate) is `slot_invalid`; a shape beyond length is the caller's to check first,
  as a float's rendering is in a digest. A reference to something that exists is a `choice` whose candidates
  are the known identifiers. A field that is given is held to its type whatever the `kind`: a `text` with
  `candidates` that are not strings is refused though it never reads them, as a `choice` with a bad bound
  is.

## Reason registry

One table, additions only. A reason code that ships cannot change meaning, because it will already be
sitting in somebody's artifacts.

| Reason | Part | Meaning |
|---|---|---|
| `breaker_tripped` | breaker | enough items failed in a row that the provider, and not the items, is the likely cause |
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

## From a shell

A program may offer the parts to a caller that has no library — a shell, a Make recipe, a step written in a
language with no port. What such a program takes and answers is fixed here, so that two of them agree the
way two ports do. It is called with the name of an entry point and where its arguments are: a file, or `-`
for the standard input, which is where they are read from when neither is named.

```
<program> <entry point> [<arguments file> | -]
```

The entry points are the ones `contract.json` names — `checkpoint.evaluate`, `breaker.classify`, and the
rest — except those it marks `takes_a_function`, which no caller without a function can make at all:
`breaker.run` is the one. The arguments are one JSON object whose members are that entry point's arguments
under the contract's own names, a member left out being absent as null is; where the contract names a
`call` rather than arguments, as `backoff` does, the object itself is that call. There is no second
vocabulary: no flag renames an argument and no name is shortened, so what a caller reads in `contract.json`
is what it writes.

The answer is one line of JSON on the standard output, and it is what the fixtures write for that entry
point (`../fixtures/README.md`, "Expectations") with each verdict's `message` kept, because a person is
reading. It is read as JSON and not compared byte for byte, so the order of its keys is the program's own
business. Nothing else is written there: a refusal's reason, and anything else meant for a person, goes to
the standard error.

The exit code is the worst verdict anywhere in the answer — `0` for `ok`, `1` for `warning`, `2` for
`halt`, and `0` for an answer that holds no verdict at all, as `classify` and `backoff` do not. A call the
part refuses, its arguments being outside the contract, answers nothing and exits `3`. A refused call
inside a batch is not that: it is answered in its place, the batch is left as it was, and the verdicts
around it decide the code. A program that made no call exits `4` — an entry point it does not have, more
arguments than a call takes, arguments it could not read, an answer it could not write, or a caller asking
what it takes. An argument its language cannot hold at all — an integer wider than its integers, a string
its strings cannot spell, a nesting deeper than its reader goes — is an argument it could not read, and the
languages that cannot hold one are the same here as for a port. Asked for nothing it says what it takes,
read from `contract.json`; what that listing looks like is for a person and is not part of this.

## Conformance

A port is conformant when:

1. the lines its adapter prints are, byte for byte, the lines the fixtures expect — one line per case, as
   `../fixtures/README.md` defines them, the format's own vectors (`protocol/v0`) included, except for a
   case whose script raises, where what the loop let out is compared and not the words the language puts on
   a bug — and it keeps `contract.json` on the calls the driver generates from it: every call with one
   defect refused, no call inside the contract refused, and every call inside it answered with the same
   line as the other ports.
   One driver, `../scripts/conform.py`, decides that for every language, and the driver is checked, not
   trusted: every expectation in every fixture is corrupted in turn and must fail under its own id, and so
   must a wrong answer to a generated call and a line one port prints differently;
2. its dependency list is empty, except SHA-256 where the standard library does not provide it;
3. its modules cannot reach the host and name nothing that is not a function of its arguments — shown by how
   they are built where the language allows it (the TypeScript modules compile with no host types at all; a
   Go package can reach only what it imports, so its import list is the whole of what it can reach; the Rust
   library is `no_std`, which takes the filesystem, the clock and the process out of the language it is
   written in), by the language's mainstream linter for what is left, and by an allowlist where none of that
   exists (Python).

An adapter is the only code a port writes for conformance: it reads the fixtures, calls the port, prints the
lines. It holds no comparison and no expectation.

A program that offers the parts from a shell is not a port, and what decides it is the section above, which
is mostly one thing: every fixture case a shell can make — every section whose entry point does not take a
function, over every input JSON text can carry — answered as that case expects, `message` aside, with each
verdict's message there, and exited with that case's worst verdict. `../scripts/conform.py cli <command>`
runs them, one process per case. A case whose input not every language can hold, and whose answer is not a
refusal, may instead be the call that was not made: nothing written and `4`. A program in a language that
can hold every input says so with `--every-input`, and may not sit a case out at all. The same command
asks for the rest of the section where a fixture cannot go: the arguments in a file and in the standard
input, more arguments than a call takes, a call whose arguments it could not read or whose answer it could
not write, and a call the contract has no case for.

The checks are held to account in turn. A mainstream mutation tool plants defects in each port's modules
and runs the fixtures and the generated calls against each; a defect nothing notices is either a missing
check or code that changes nothing; what is left is listed, each with its reason, in
`../scripts/survivors_accepted.json`. What no check owns is a port that is wrong the same way as the other
three on a call inside the contract: the examples and a reader of this text are all that stand there.
