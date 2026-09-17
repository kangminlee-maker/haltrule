# Conformance fixtures

Each fixture is a pair: an input that every implementation is given, and the verdict every implementation
must produce. The fixtures are the contract; the implementations are what get tested against it.

Two rules hold for every fixture added here:

1. **No real data.** Fixtures are generated from fixed seeds, never copied from a production artifact.
   Inputs that came from a real pipeline are paraphrased until nothing identifies a person, an account, or
   a document.
2. **A corrupted expectation must fail.** Before a fixture counts, flip one expected value and confirm the
   runner reports a failure. A fixture that passes either way is measuring nothing.

Format: TODO, alongside the first implementation.
