# What a typed decision model leaves undecided

A new kind of model answers a question with a typed value instead of a sentence. You hand it some state
and a set of questions declared in advance — is this true, which of these, what score — and it returns a
probability per option with a confidence. It cannot violate the schema, because the schema is fixed before
the call. TypeSafe's Jev is the one this page was written against, in September 2026.

It quotes 70 to 500 milliseconds and $0.042 per million input tokens with output free. Both figures
deserve a caveat their vendor does not give them. The latency is a regional floor: measured from this side
of the Pacific the median was around 600 milliseconds and nothing came back under half a second, with the
first call of a session near a second and a half. And the headline cost multiples are against slow
expensive models — recalculated against models of comparable accuracy they are nearer 25 times faster and
76 times cheaper, and nearer 8 times against small ones.

That removes a whole class of work: no prompt to parse, no field to clip back into range, no batching
twenty items into one call to keep the cost down, no realigning an answer set that drifted from the
question set. Four bug shapes, gone.

It leaves one thing untouched, and the vendor says so in three places of their own documentation:

- "An alias moves when a new release ships, so the answers behind it can change without a change on your
  side."
- "Pin specific versions if you've calibrated confidence thresholds, and upgrade on your own schedule."
- "a guard built this way belongs alongside deterministic checks, not instead of them. Keep deterministic
  work in code."

A calibrated threshold is a constant that lives nowhere reviewable. A moving alias makes that constant,
and every judgment already stored under it, expire on somebody else's schedule. And the third sentence is
the vendor telling you to go and build a deterministic layer next to theirs.

## Why it cannot live inside this library

Two reasons, and neither is a limitation to work around.

The modules here compile with no host types at all, and the Python ones import through a closed allowlist,
so a network call does not typecheck and does not import. That is not a rule anyone follows; it is how the
modules are built.

And conformance is byte-for-byte across languages. The strongest claim made for a model of this kind is
that it "returns similar answers for similar inputs". Similar is a failure here, and similar is the right
word. Twenty identical requests, one question about one unchanging state, every one answered by the same
version: 0.85, 0.86, 0.87, 0.88, 0.89 — five distinct answers, a spread of 0.04, a standard deviation of
0.009, no errors. Nothing moved and the answer moved anyway.

Asking one identical judgment two ways moves it further. As a probability that the meaning was preserved,
a translation that drops a condition scores 0.14; asked as a two-way choice, the same judgment about the
same text comes back "no" at 0.97, with a confidence of 0.95. Read as one number those are 0.86 and 0.97,
a gap of a tenth where the run-to-run spread is 0.04.

This library is the ruler; a model of this kind is someone very good at estimating by eye, and you cannot
draw the ruler's marks by eye.

## The seams the spec already leaves

Three holes in the spec are exactly this shape, and they were there before any of it existed:

- the breaker takes a caller-supplied `failure_class` and "does not judge it";
- `slot` says a shape beyond length is "the caller's to check first";
- `checkpoint` takes `validation_issues`, "what the caller's own validation found".

The division of labour is one sentence. **The model answers what is true, and with what confidence. This
library answers whether that may be written down, and if not, where the next run picks up.**

The pairing that is worth the most is not a judgment at all. It is invalidation. A cached judgment goes
stale along three independent axes — the model version, the question text, and the input state — and the
middle one is not cosmetic: in independent testing, splitting one judgment into five questions
moved a benchmark from 62.6% to 95.0%, which is to say a change in how you ask can matter more than a
change in what you ask about. A question set is a contract, and
`checkpoint` is a three-axis staleness check with a per-axis reason: `contract_revision_mismatch`,
`stage_config_digest_mismatch`, `dependency_digest_mismatch`, the last naming which dependency moved.
Pinning a version, which the vendor recommends, is the easy half; knowing which stored answers the pin
just orphaned is the half nobody ships.

The response makes that cheap. Its body names the version that actually answered, resolved to a concrete
one even when the request named a moving alias, so the recorded contract revision is exact without pinning
anything. A caller may keep asking for the alias and still learn, per item and on the day it happens, that
the alias moved underneath it.

## The bar

`if (score > 0.8)` had no home in `slot`: neither `choice` nor `text` compares a number to a bar. It has
one now, a `score` kind that takes the number as it arrives and holds it against an optional `min` and
`max`, both included. A value that is not such a number — NaN, an infinity, a boolean, a string that is
not blank, an integer past ±(2^53 − 1) — is `slot_invalid`, a present value failing its contract; a bar
that is not one is refused, as any spec outside the contract is. The kind does nothing to the number but
compare it.

An earlier version of this page had it as an integer at a declared scale, on the premise that comparing
floats is where languages part ways. That premise had never been run, and run it failed. A thousand and
one decimal strings, read by the standard parser of Python, JavaScript, Go and Rust and held against
four bars, on linux, darwin and windows, amd64 and arm64 each: not one comparison of 4,004 came out
differently anywhere. IEEE 754 fixes parsing, comparison and the four operations bit for bit, and the
four languages keep to it.

What does differ is narrower, and none of it is comparison. Writing a double as text: `1.0` is `1.0` in
Python and `1` in JavaScript, and NaN is `NaN`, `null` or an error — which is why the canonical form
refuses floats, and that rule stands, because a digest is bytes. Rounding: Python rounds a half to even
and the other three away from zero, so the conversion the integer design needed — 0.865 into 86 or 87 —
disagreed between languages on 46 of 1,001 inputs. The design had added the one step that diverges. And
arithmetic on some processors: Go fuses `x*y + z` on arm64 and not on amd64, on all three operating
systems, and the last bit moves; Rust fuses only when asked.

So the number goes in as it is, on one condition: the library does nothing with a score but compare it.
Sums, weights and averages stay with the caller, which is where the line below already put them.

One seam remains. A bar of 0.8 cannot go into a digest, because the canonical form refuses it, and the
bar belongs in the stage's configuration digest — change the bar and every judgment made under the old
one is stale. The spec's standing answer covers it: render it as a string. A bar is a constant somebody
typed into a configuration file, so the string is the text they typed, and there is no rounding to
argue about.

Two things for whoever sets a bar, neither of them the spec's. It has to stand outside the instrument's
own spread: the model measured here moved 0.04 across twenty identical requests, and a bar inside that
is a coin. And a caller who must round should name the rounding instead of taking the language's
default; with round-half-even named in all four languages, the 46 disagreements go to none.

The line it will not cross: this library owns the acceptance bar — may this judgment be recorded — and
never the routing bar — is this ticket urgent. The second is the caller's domain, and taking it would
make a decision spec into an application framework.

## What the pipelines actually built

Two of the pipelines in `patterns.md` were read again in September 2026, with this question in mind. Both
had built the parts of this spec by hand, independently, without knowing about each other. So had this
library. Three codebases, five components each: a canonicalizer, a digest, a dependency map, a status
vocabulary, and a verdict carrying a reason.

Two of the three canonicalizers are wrong, and wrong in the direction that fails open.

### A canonicalizer that fails open

The planning pipeline hashes a sorted JSON rendering of its inputs, and that hash decides whether a stage
artifact may be reused. Sorting is by `localeCompare`, and rendering is `JSON.stringify`. Both choices
look harmless. Run them:

```js
import crypto from "node:crypto";
const sortValue = (v) => Array.isArray(v) ? v.map(sortValue)
  : (!v || typeof v !== "object") ? v
  : Object.fromEntries(Object.entries(v).sort(([l],[r]) => l.localeCompare(r)).map(([k,n]) => [k, sortValue(n)]));
const h = (v) => crypto.createHash("sha256").update(JSON.stringify(sortValue(v), null, 2)).digest("hex");

h({x: NaN})                    === h({x: null});                     // true
h({x: Infinity})               === h({x: null});                     // true
h({x: new Map([["a",1]])})     === h({x: new Map([["b",2]])});       // true
h({a: 1, b: undefined})        === h({a: 1});                        // true
```

Four pairs of different inputs, four identical digests. Each is a way for content to change while the key
does not, which is the first of the three halting conditions failing open and saying nothing.

Be precise about what that does and does not show. It shows the canonicalizer collides on those shapes. It
does not show that any of them reaches a particular payload — `undefined` beside an absent field is
arguably the same thing, and a NaN or a Map has to get in there before it can hurt anyone. Whether they do
is the first question to ask of any pipeline hashing its inputs this way, not something to assume from the
collision. What the spec offers is that the question never has to be asked: those shapes halt by name
rather than hashing alike.

The spec answers every row by name: `digest_input_float` for NaN and the infinities, because a float that
matters to a digest is the caller's to render explicitly; `digest_input_unsupported` for a Map, a Set, or
an absent value. It halts with a reason instead of hashing them all to the same thing.

Sorting by `localeCompare` is the second half of it, and here too the honest claim is narrower than the
obvious one. It orders differently from code units:

```
locale order: _internal a_b a-b ab co-op coop generated_at module_id Module_id
code-unit   : Module_id _internal a-b a_b ab co-op coop generated_at module_id
```

Tried across English, Korean, Swedish and a German phonebook collation, realistic keys of that shape did
not move, so "two machines disagree" is not a thing to claim without measuring it. What does bite is that
`localeCompare` answers 0 for two strings that are not equal — the composed and decomposed spellings of
one accented character, say — and a stable sort then leaves insertion order to pick the bytes. File paths
read from a filesystem that hands back decomposed names are a real way to meet that.

The spec sorts map keys by UTF-16 code unit, which is a total order on distinct strings and cannot tie,
and the linter in this repository already refuses `localeCompare`.

### Identity by modification time, or by URI alone

The localization pipeline identifies a dependency by its size and modification time when the file is
local, and by its URI alone when it is in object storage. The first invalidates when a re-download leaves
the bytes identical, which costs a recomputation. The second does not invalidate when the content at that
URI changes, which costs correctness, and is the worse of the two.

Its cache key also flattened every axis into one hash, so when it moved nobody could tell what moved; and
the dependency set was implicit, so a signature belonging to a later stage invalidated an earlier one that
does not read it. The recorded result was two language fleets each re-transcribing fifty-two chapters
across three speech models, and re-running the merge over all of it.

A dependency map fixes the last of those by construction rather than by care: a stage that does not
consume something simply has no entry for it.

### Three verdicts, none of them with a resume

All three codebases invented a verdict. One returns `ok | failed | skipped` with a prose error string. Its
alert reads the status, so rewording breaks nothing; what the prose costs is the reason. Of 128 failures it
recorded, 74 say only `ValueError` before the colon, and which failure it was is in the sentence after it.
One returns a boolean, a violation list and an action, three encodings of one fact, with the action fusing
the verdict and the response. One builds a reason by interpolating a status word; its records hold three
such words and its tests pin four, so the set is closed in practice and listed nowhere.

None of them carries a resume point, so the orchestrator derives it again each time.

The one thing they got right independently is worth naming: two of the three restrict their reasons to a
closed set. That is the reason registry here, arrived at from the same pressure — a reason code that ships
is already sitting in somebody's artifacts, so it can be added to and never redefined.

## What this changes here

Two corrections to this spec came out of reading that code, and both hold whether or not a typed decision
model exists:

- **`classify` is the fallback, not the recommendation.** One of those pipelines classifies an exhausted
  credit balance structurally — parse the body, compare an error type — and rejects prose matching in as
  many words, because "the message wording is not a contract". It is right. When a provider gives you
  fields, classify on the fields and pass the result in as `failure_class`, which `state` already accepts.
  `classify` is for when a message string is all you have, which across a dozen providers is most of the
  time.
- **The backoff carries no jitter, and the caller adds it.** Jitter is randomness, so it cannot live in a
  spec whose output is compared byte for byte. The number here is the delay before the jitter.

## What is not settled

- Accuracy outside English. The vendor states English is most accurate and that other languages,
  CJK among them, should be tested before production. Any recommendation that skips this measurement is
  not an honest one.

The other two this section carried are settled since: `checkpoint` and the breaker's trip return the
shared verdict now, and `breaker_tripped` is in the registry.

## Sources

Measured on 2026-09-20 against `jev-1.13.0`: the run-to-run spread, the two question shapes, the
hundredths the model answers in, and the latency. The harness is standard library only and its results
are kept beside it.

Run on 2026-09-21: the comparison and rounding of parsed doubles in four languages, on six hosted
runners — linux, darwin and windows, amd64 and arm64 each.

Read the same day: TypeSafe's model documentation, API reference and blog; the Pydantic AI, OpenRouter,
Cloudflare Workers AI and LangChain integration pages, for how many languages this reaches; and four
third-party pieces — two walkthroughs, one independent evaluation that measured the run-to-run and
question-shape variation quoted above, and one that recalculated the cost and latency multiples against
comparable models. Nobody has published accuracy for this model outside English, which is why the last
section says what it says.

The four digest collisions and the sort-order divergence were run, not read.
