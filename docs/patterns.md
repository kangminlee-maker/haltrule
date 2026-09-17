# Where this came from

Five production pipelines, built independently over two years, each solved the same handful of problems in
its own way: a media localization pipeline on a cloud workflow engine, an accounting reconciliation batch
driven by hand from a runbook, a planning pipeline behind a single large runner, an MCP review server with
an explicit state machine, and a recruiting workflow whose ledger is a spreadsheet.

None of them shared code. The table below is what they duplicated.

| Pattern | How each one solved it | What a shared part can absorb |
|---|---|---|
| Consecutive-failure breaker | three separate implementations, each with its own thresholds and dead-letter file | the whole policy — classification, backoff schedule, state machine, dead-letter entry |
| "Is this already done?" | file existence, input digest, lease plus compare-and-swap plus read-back, run manifest — five different answers | the digest and the reuse verdict; the read-back stays with the caller, because it is I/O |
| Concurrency limit | engine config, a bespoke map-with-limit helper, shell sharding, a per-run item budget | nothing — this is a 30-line idiom in every language, and sharing it buys little |
| Budget exhaustion | turn, time and token budgets in one project; a wall-clock run budget in another | the accounting and the exhaustion verdict; what to do when exhausted stays with the caller |
| Structured-output validation | forced tool use, fixed JSON schema with a validation retry, slot contracts, runtime schema checks | the contract check for values a person or a model submits |
| Stage order as one source | a stage array with a derived projection, a write-order contract, a transition table, engine YAML | nothing — an invariant test in your own repo is enough |
| Human intervention surface | a spreadsheet adjudication tab, an exception queue of files, a local operator UI, a confirmation contract | nothing — engines already do approval well, and the surface belongs to the product |

Three of the seven are worth sharing. Two are cheaper to keep local. Two belong to whatever engine you use.

## The incidents that shaped the rule

- A retry predicate spent three attempts on a permanent failure, costing minutes per item across a batch.
- A long preflight consumed an eight-minute run budget, so every one of nine due items was deferred and the
  pipeline silently stalled.
- A missing idempotency key created a duplicate record in an external system that a person had to unpick.
- Thirteen concurrent verifiers exhausted host memory and the OS killed the run.

None of these are exotic. They are what happens when each pipeline invents its own failure policy, and they
are the reason the policy is worth writing down once and testing once.
