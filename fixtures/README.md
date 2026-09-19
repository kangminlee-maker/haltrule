# Conformance fixtures

Each fixture is a pair: an input that every implementation is given, and the verdict every implementation
must produce. The fixtures are the contract; the implementations are what get tested against it.

Two rules hold for every fixture added here:

1. **No real data.** Fixtures are generated from fixed seeds, never copied from a production artifact.
   Inputs that came from a real pipeline are paraphrased until nothing identifies a person, an account, or
   a document.
2. **A corrupted expectation must fail.** Before a fixture counts, flip one expected value and confirm the
   runner reports a failure. A fixture that passes either way is measuring nothing.

## Format

One JSON file per part, `<part>/v<N>.json`, each naming itself in `fixture_version` (`breaker/v0`,
`checkpoint/v0`, `budget/v0`, `slot/v0`, and `protocol/v0` for the runners' own output format, below). A runner given no path runs every `.json` file under `fixtures/`, picks the part from that
field, and refuses a version it does not know or a section it does not read, so neither a file nor a
section it skips can pass as zero cases. The gates count cases from the files themselves and require each
runner to have run, and dumped, every one; a case id is therefore unique across all fixture files. A case
whose computation raises fails under its own id.

Inputs to `checkpoint`, `budget`, `slot` and `protocol` never hold a raw JSON number (`breaker/v0`, which
is older than this rule, holds plain integers within ±(2^53 − 1), which every parser reads alike).
Parsers disagree about some — JavaScript reads `1.0` as `1` and
`9007199254740993` as `9007199254740992` — so two runners would test two different values. A number is
written `{"$number": "<literal>"}` and each runner decodes the literal itself, refusing an integer literal a
double cannot hold exactly (past ±2^53 the two languages would decode different values);
`{"$bigint": "<literal>"}` is an integer the TypeScript runner builds as a bigint, for exactly those. `{"$unsupported": "<kind>"}` builds a value outside the
digest model that JSON cannot spell, in each language's own form: `undefined`, `instance` (a class
instance), `non_string_key` (a map with a symbol key in TypeScript, an int key in Python), `sparse_array`
(an array with a hole in TypeScript; Python has no holes, so a list holding an unsupported element). Runners
refuse a raw number in an input and a kind they do not know. Expected values are plain JSON.

A literal is read by this grammar and by nothing else:

    $number   -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?   or   NaN | Infinity | -Infinity
    $bigint   -?(0|[1-9][0-9]*)

It is the JSON number grammar plus the three values JSON cannot spell. A language's own number parser does
not decide: Python's reads `1_0`, ` 7 ` and `nan`; JavaScript's reads `0x10`, ` 7 `, `+1`, and the empty
string as zero. A runner refuses any other literal ("outside the fixture grammar") before it builds anything,
so one literal can never be two values.

The files are ASCII — checked by the gates — so every non-ASCII character, and every unpaired surrogate a
test needs, is a `\u` escape, and no editor or transport can normalize a test value away.

A canonicalize case expects either `{"canonical", "digest"}` or `{"halt": "<reason>"}`. Its canonical form
is written from the spec, never produced by an implementation, and its digest is `sha256sum` of those
bytes.

## Result lines

`--dump` prints one line per case, in file path order and, within a file, in the part's section order and
the file's case order: `{"actual":<result>,"id":"<case id>","section":"<section>"}`. The gates compare the
runners' output byte for byte, so the line format is part of the contract and a port writes it itself rather
than trusting its language's JSON library to agree. Output is UTF-8, each line ended by one `\n`.

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
(`verdicts`) and the ledger afterwards (`used`, in decimal strings so 2^63 − 1 survives every JSON parser); a
`validate` case expects one verdict. An expected verdict omits `message`, which is not part of conformance;
the runners check that it is present and a string before dropping it.
