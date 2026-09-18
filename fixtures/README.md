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
`checkpoint/v0`). A runner given no path runs every `.json` file under `fixtures/`, picks the part from that
field, and refuses a version it does not know or a section it does not read, so neither a file nor a
section it skips can pass as zero cases. The gates count cases from the files themselves and require each
runner to have run, and dumped, every one.

Inputs never hold a raw JSON number. Parsers disagree about some — JavaScript reads `1.0` as `1` and
`9007199254740993` as `9007199254740992` — so two runners would test two different values. A number is
written `{"$number": "<literal>"}` and each runner decodes the literal itself; `{"$bigint": "<literal>"}` is
an integer the TypeScript runner builds as a bigint. `{"$unsupported": "<kind>"}` builds a value outside the
digest model that JSON cannot spell, in each language's own form: `undefined`, `instance` (a class
instance), `non_string_key` (a map with a symbol key in TypeScript, an int key in Python), `sparse_array`
(an array with a hole in TypeScript; Python has no holes, so a list holding an unsupported element). Runners
refuse a raw number in an input and a kind they do not know. Expected values are plain JSON.

The files are ASCII — checked by the gates — so every non-ASCII character, and every unpaired surrogate a
test needs, is a `\u` escape, and no editor or transport can normalize a test value away.

A canonicalize case expects either `{"canonical", "digest"}` or `{"halt": "<reason>"}`. Its canonical form
is written from the spec, never produced by an implementation, and its digest is `sha256sum` of those
bytes.
