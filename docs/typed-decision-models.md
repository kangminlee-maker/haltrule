# What a typed decision model leaves undecided

A new kind of model answers a question with a typed value instead of a sentence. You hand it some state
and a set of questions declared in advance — is this true, which of these, what score — and it returns a
probability per option with a confidence, in 70 to 500 milliseconds, for $0.042 per million input tokens
with output free. It cannot violate the schema, because the schema is fixed before the call. TypeSafe's
Jev is the one this page was written against, in September 2026.

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
that it "returns similar answers for similar inputs". Similar is a failure here. This library is the ruler;
a model of this kind is someone very good at estimating by eye, and you cannot draw the ruler's marks by
eye.

## The seams the spec already leaves

Three holes in the spec are exactly this shape, and they were there before any of it existed:

- the breaker takes a caller-supplied `failure_class` and "does not judge it";
- `slot` says a shape beyond length is "the caller's to check first";
- `checkpoint` takes `validation_issues`, "what the caller's own validation found".

The division of labour is one sentence. **The model answers what is true, and with what confidence. This
library answers whether that may be written down, and if not, where the next run picks up.**

The pairing that is worth the most is not a judgment at all. It is invalidation. A cached judgment goes
stale along three independent axes — the model version, the question text, and the input state — and
`checkpoint` is a three-axis staleness check with a per-axis reason: `contract_revision_mismatch`,
`stage_config_digest_mismatch`, `dependency_digest_mismatch`, the last naming which dependency moved.
Pinning a version, which the vendor recommends, is the easy half; knowing which stored answers the pin
just orphaned is the half nobody ships.

The response makes that cheap. Its body names the version that actually answered, resolved to a concrete
one even when the request named a moving alias, so the recorded contract revision is exact without pinning
anything. A caller may keep asking for the alias and still learn, per item and on the day it happens, that
the alias moved underneath it.

## The bar this library does not have yet

`if (score > 0.8)` has no home in `slot`. Neither `choice` nor `text` compares a number to a bar. The
design is settled and deliberately not yet built: a `score` kind whose value is an integer at a declared
scale, so 0.87 arrives as 870 at scale 1000 and the caller does the rounding where it can be reviewed.
That is the stance the spec already takes on floats in a digest input, so it adds no special case, and it
removes cross-language float comparison entirely.

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

Four pairs of different inputs, four identical digests. Each one is a stage artifact whose content changed
while its key did not, reused as though it were fresh — the first of the three halting conditions in the
README, failing open and saying nothing.

The spec answers every row by name: `digest_input_float` for NaN and the infinities, because a float that
matters to a digest is the caller's to render explicitly; `digest_input_unsupported` for a Map, a Set, or
an absent value. It halts with a reason instead of hashing them all to the same thing.

`localeCompare` adds a second failure in the other direction. It depends on the locale and on which ICU
data the runtime was built with, so the same object can sort differently on two machines:

```
locale order: _internal a_b a-b ab co-op coop generated_at module_id Module_id
code-unit   : Module_id _internal a-b a_b ab co-op coop generated_at module_id
```

Different order, different bytes, different digest for identical content — a cache that looks broken on
one machine and fine on another. The spec sorts map keys by UTF-16 code unit, which no locale can move,
and the linter in this repository already refuses `localeCompare` for exactly this reason.

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

All three codebases invented a verdict. One returns `ok | failed | skipped` with a prose error string,
which a metric filter downstream matches on, so rewording the message breaks the filter silently. One
returns a boolean, a violation list and an action, three encodings of one fact, with the action fusing the
verdict and the response. One builds reason codes by interpolation, giving an unbounded set that nothing
can count or alert on.

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
- `checkpoint` still returns an issue list rather than a `Verdict`, so anything written against that shape
  is written against a moving one.
- The breaker's reasons are not in the registry yet.

## Sources

Read on 2026-09-20: TypeSafe's model documentation and blog; the Pydantic AI, OpenRouter, Cloudflare
Workers AI and LangChain integration pages, for how many languages this reaches; two third-party
walkthroughs. The four digest collisions and the sort-order divergence above were run, not read.
