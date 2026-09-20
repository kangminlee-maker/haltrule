# Conformance fixtures

Each fixture is a pair: an input that every implementation is given, and the verdict every implementation
must produce. The fixtures are the contract; the implementations are what get tested against it.

Two rules hold for every fixture added here:

1. **No real data.** Fixtures are generated from fixed seeds, never copied from a production artifact.
   Inputs that came from a real pipeline are paraphrased until nothing identifies a person, an account, or
   a document.
2. **A corrupted expectation must fail.** The driver corrupts every expectation of every case in turn and
   requires each to fail under its own id, on every run. A fixture that passes either way is measuring
   nothing.

## Format

One JSON file per part, `<part>/v<N>.json`, each naming itself in `fixture_version` by that same path
(`breaker/v0`, `checkpoint/v0`, `budget/v0`, `slot/v0`, and `protocol/v0` for the line format below). Every
other key is a section: a list of cases, each with an `id`, its inputs, and an `expect`.

Two things read these files and neither trusts the other. A port's **adapter** reads every `.json` file under
`fixtures/`, by path, finds its function for a section by the section's name, and prints one result line per
case. The **driver**, `scripts/conform.py`, reads the same files on its own, works out the line each case
expects, and compares. It wants exactly one line for every case it found, in order, so neither a file, a
section nor a case can pass by being skipped, and a case id is unique across all files. A case whose
computation raises is reported under its own id. The driver also refuses a fixture file that is malformed,
before any port sees it; what "malformed" means is the rest of this section.

An input never holds a raw JSON number. Parsers disagree about some — JavaScript reads `1.0` as `1` and
`9007199254740993` as `9007199254740992` — so two ports would be tested on two different values. A number is
written `{"$number": "<literal>"}` and each adapter builds it from the literal; an integer literal a double
cannot hold exactly is refused, and is written `{"$bigint": "<literal>"}` instead, which the TypeScript
adapter builds as a bigint. `{"$unsupported": "<kind>"}` builds a value outside the digest model that JSON
cannot spell, in each language's own form: `undefined`, `instance` (a class instance), `non_string_key` (a
map with a symbol key in TypeScript, an int key in Python), `sparse_array` (an array with a hole in
TypeScript; Python has no holes, so a list holding an unsupported element). Expected values are plain JSON.

`$bigint` appears only where an integer may pass 2^53 − 1 — a budget's caps and amounts, a digest input, a
result-line value — and `$unsupported` only in the last two. Everywhere else an argument is one of JSON's
kinds, and what a language can hold beyond them is not asked about: a bigint slot bound or an `undefined`
slot value has no case, because not every port can build one.

A literal is read by this grammar and by nothing else:

    $number   -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?   or   NaN | Infinity | -Infinity
    $bigint   -?(0|[1-9][0-9]*)

It is the JSON number grammar plus the three values JSON cannot spell. A language's own number parser does
not decide: Python's reads `1_0`, ` 7 ` and `nan`; JavaScript's reads `0x10`, ` 7 `, `+1`, and the empty
string as zero. The driver refuses any other literal ("outside the fixture grammar") before an adapter
builds anything from it, so one literal can never be two values.

The files are ASCII — the driver refuses one that is not — so every non-ASCII character, and every unpaired surrogate a
test needs, is a `\u` escape, and no editor or transport can normalize a test value away.

A canonicalize case expects either `{"canonical", "digest"}` or `{"halt": "<reason>"}`. Its canonical form
is written from the spec, never produced by an implementation, and its digest is `sha256sum` of those
bytes — which the driver checks on the fixture itself, with no port involved.

## Result lines

An adapter prints one line per case, files by path and each file in its own order:
`{"actual":<result>,"id":"<case id>","section":"<section>"}`, with `"raised":"<what>"` in place of `actual`
when the case raised something that is not a refusal — the line is a map like any other, so its keys are
sorted there too: `id`, `raised`, `section`. A port whose types cannot build a case's input prints
`{"id":"<case id>","section":"<section>","unbuildable":true}` for it. The driver takes that line for a
case whose input holds an unpaired surrogate, a `$bigint` outside a signed 64-bit integer, or an
`$unsupported` value, and whose answer is not a refusal; it reads that off the input and the expectation, so
no case is marked by hand. Run with `--every-input`, as the TypeScript and Python ports are, it
takes that line for no case at all. The driver compares each line with the line it
expects, byte for byte, so the line format is part of the contract and a port writes it itself rather than
trusting its language's JSON library to agree. Output is UTF-8, each line ended by one `\n`.

A result holds null, booleans, integers, strings, lists, and maps with string keys, and is written as JSON
with no whitespace, where:

- **Maps** list their members by key, keys compared as sequences of UTF-16 code units. That is text order
  — `"10"` before `"2"` — and it puts a key past U+FFFF (whose first unit is a surrogate, U+D800..U+DBFF)
  before one at U+E000..U+FFFF. It is neither code point order nor the order a JavaScript object lists
  integer-like keys in. Lists keep their order.
- **Strings**, keys included, escape `"` as `\"`, `\` as `\\`, U+0008, U+0009, U+000A, U+000C and U+000D
  as `\b`, `\t`, `\n`, `\f` and `\r`, every other character below U+0020 as `\u00xx` in lowercase hex,
  and an unpaired surrogate as `\udxxx` in lowercase hex. Everything else is written as itself: `/`, `<`,
  `>`, `&`, U+007F, U+2028 and U+2029 are not escaped, and nothing is normalized.
- **Numbers** are integers within ±(2^53 − 1), in decimal, with no sign on zero, no fraction and no
  exponent — however the language holds them: `1.0` and a bigint `1` are both `1`.
- **Anything else is refused**: a number with a fraction, NaN, an infinity, an integer past the range, a
  map with a key that is not a string, a value of any other kind. The line is refused whole, never written
  without the part that could not be.

`protocol/v0.json` holds the vectors: a `result_line` case gives a `value` and expects either
`{"line": "<the line for that value>"}` or `{"refused": true}`. Its lines are written by hand from the text
above, never produced by an implementation.

## Expectations

A `charge` case runs its charges, in order, against one budget and expects one verdict per charge
(`verdicts`) — `{"refused": true}` for a charge the budget refuses, which changes nothing — and the ledger
afterwards (`used`, in decimal strings so 2^63 − 1 survives every JSON parser), or `{"refused": true}`
alone when the budget's caps are refused; a `validate` case expects one verdict, or `{"refused": true}` for a
spec outside the contract; a `checkpoint` case gives `args`, under the argument names the spec lists, and
expects the issue list, or `{"refused": true}` for arguments outside the contract. A `classify` or
`backoff` case expects the answer or `{"refused": true}`; a `state` case runs its events, in order, against
one batch and expects one answer per event (`returns`) — `{"refused": true}` for a report the batch
refuses, which changes nothing — or `{"refused": true}` alone when the policy is refused. An adapter
answers `refused` only for the part's own refusal: inputs are built
before the part is called, and a verdict of the wrong shape is a failure of the case. An expected verdict
omits `message`, which is not part of conformance; the adapters check that it is present and a string before
dropping it.
