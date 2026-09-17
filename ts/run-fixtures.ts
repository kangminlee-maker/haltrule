/**
 * Conformance runner for fixtures/breaker/v0.json against ts/breaker.ts.
 *
 * Loads the fixture file, feeds each case to the pure policy functions, and
 * diffs the actual result against the fixture's expected value. Exits
 * non-zero and prints every mismatch (fixture id + field + expected/actual)
 * on any failure — this is the instrument the "corrupt one expectation and
 * confirm it fails" check in fixtures/README.md exercises.
 *
 * With `--dump`, skips the expected-value comparison entirely and instead
 * prints one canonical JSON line per case holding this implementation's
 * actual result (never the fixture's expectation). scripts/check.sh diffs
 * this output byte-for-byte against `py/run_fixtures.py --dump` — that is
 * the real parity check the README promises ("CI compares their output byte
 * for byte"), not just the summary line both runners happen to print at the
 * end.
 *
 * Only node:fs/node:path (both stdlib) are used; no external dependencies.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  classifySystemicDispatchFailure,
  dispatchBackoffDelayMs,
  DispatchBreakerState,
  type DispatchBreakerPolicy,
  type DispatchDeadLetterEntry,
  type DispatchBreakerTripState,
  type SystemicDispatchFailureClass,
} from "./breaker.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_FIXTURE_PATH = path.join(__dirname, "..", "fixtures", "breaker", "v0.json");

interface ClassifyCase {
  id: string;
  message: string | number | null;
  expect: SystemicDispatchFailureClass | null;
}

interface BackoffCase {
  id: string;
  attempt: number;
  initial_ms: number;
  cap_ms: number;
  expect_ms: number;
}

type StateEvent =
  | { kind: "success"; item_id: string }
  | { kind: "skipped"; item_id: string }
  | {
      kind: "failure";
      item_id: string;
      failure_class: SystemicDispatchFailureClass | null;
      failure_message: string;
      attempt_count: number;
    };

interface StateCase {
  id: string;
  policy: DispatchBreakerPolicy;
  events: StateEvent[];
  expect: {
    returns: (DispatchBreakerTripState | null)[];
    completed: string[];
    dead_letter: DispatchDeadLetterEntry[];
    tripped: DispatchBreakerTripState | null;
  };
}

interface FixtureFile {
  fixture_version: string;
  classify: ClassifyCase[];
  backoff: BackoffCase[];
  state: StateCase[];
}

interface StateActual {
  returns: (DispatchBreakerTripState | null)[];
  completed: string[];
  dead_letter: DispatchDeadLetterEntry[];
  tripped: DispatchBreakerTripState | null;
}

let failureCount = 0;
let caseCount = 0;

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return a === b;
  if (typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const aObj = a as Record<string, unknown>;
  const bObj = b as Record<string, unknown>;
  const aKeys = Object.keys(aObj);
  const bKeys = Object.keys(bObj);
  if (aKeys.length !== bKeys.length) return false;
  for (const key of aKeys) {
    if (!Object.prototype.hasOwnProperty.call(bObj, key)) return false;
    if (!deepEqual(aObj[key], bObj[key])) return false;
  }
  return true;
}

/** RFC-8785-ish canonical form (sorted keys, no insignificant whitespace) so
 * a --dump line is byte-identical to the Python runner's for the same
 * actual result. Must stay in lockstep with the Python runner's
 * `_canonical_json`. */
function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) {
      sorted[key] = sortKeysDeep(source[key]);
    }
    return sorted;
  }
  return value;
}

function canonicalStringify(value: unknown): string {
  return JSON.stringify(sortKeysDeep(value));
}

function fail(caseId: string, field: string, expected: unknown, actual: unknown): void {
  failureCount += 1;
  console.error(
    `FAIL [${caseId}] field=${field}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`,
  );
}

function actualClassify(tc: ClassifyCase): SystemicDispatchFailureClass | null {
  // `message` may be a non-string (e.g. a JSON number) to exercise the
  // classifier's typeof guard — cast through unknown, matching how an
  // untyped caller could hand it anything.
  return classifySystemicDispatchFailure(tc.message as unknown as string | null);
}

function actualBackoff(tc: BackoffCase): number {
  return dispatchBackoffDelayMs({
    attempt: tc.attempt,
    initialMs: tc.initial_ms,
    capMs: tc.cap_ms,
  });
}

function actualState(tc: StateCase): StateActual {
  const state = new DispatchBreakerState(tc.policy);
  const returns: (DispatchBreakerTripState | null)[] = [];
  for (const event of tc.events) {
    if (event.kind === "success") {
      state.recordItemSuccess(event.item_id);
      returns.push(null);
    } else if (event.kind === "skipped") {
      state.recordItemSkipped(event.item_id);
      returns.push(null);
    } else {
      const entry: DispatchDeadLetterEntry = {
        item_id: event.item_id,
        failure_class: event.failure_class,
        failure_message: event.failure_message,
        attempt_count: event.attempt_count,
      };
      returns.push(state.recordItemFailure(entry));
    }
  }
  return {
    returns,
    completed: [...state.completedItemIds()],
    dead_letter: [...state.deadLetterEntries()],
    tripped: state.tripped(),
  };
}

function runClassify(cases: ClassifyCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = actualClassify(tc);
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "classify.expect", tc.expect, actual);
    }
  }
}

function runBackoff(cases: BackoffCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = actualBackoff(tc);
    if (!deepEqual(actual, tc.expect_ms)) {
      fail(tc.id, "backoff.expect_ms", tc.expect_ms, actual);
    }
  }
}

function runState(cases: StateCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = actualState(tc);
    if (!deepEqual(actual.returns, tc.expect.returns)) {
      fail(tc.id, "state.returns", tc.expect.returns, actual.returns);
    }
    if (!deepEqual(actual.completed, tc.expect.completed)) {
      fail(tc.id, "state.completed", tc.expect.completed, actual.completed);
    }
    if (!deepEqual(actual.dead_letter, tc.expect.dead_letter)) {
      fail(tc.id, "state.dead_letter", tc.expect.dead_letter, actual.dead_letter);
    }
    if (!deepEqual(actual.tripped, tc.expect.tripped)) {
      fail(tc.id, "state.tripped", tc.expect.tripped, actual.tripped);
    }
  }
}

function dumpClassify(cases: ClassifyCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "classify", id: tc.id, actual: actualClassify(tc) }));
  }
}

function dumpBackoff(cases: BackoffCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "backoff", id: tc.id, actual: actualBackoff(tc) }));
  }
}

function dumpState(cases: StateCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "state", id: tc.id, actual: actualState(tc) }));
  }
}

function main(): void {
  // A runner that silently ignores the path it was handed reports PASS on a file
  // it never read, so the argument is honored here exactly as the Python runner
  // honors sys.argv[1].
  const args = process.argv.slice(2);
  const dump = args.includes("--dump");
  const positional = args.filter((a) => a !== "--dump");
  const fixturePath = positional[0] ?? DEFAULT_FIXTURE_PATH;
  const raw = readFileSync(fixturePath, "utf8");
  const fixtures = JSON.parse(raw) as FixtureFile;

  if (dump) {
    dumpClassify(fixtures.classify);
    dumpBackoff(fixtures.backoff);
    dumpState(fixtures.state);
    return;
  }

  runClassify(fixtures.classify);
  runBackoff(fixtures.backoff);
  runState(fixtures.state);

  if (failureCount > 0) {
    console.error(`\n${failureCount} mismatch(es) across ${caseCount} cases (fixture_version=${fixtures.fixture_version}).`);
    process.exit(1);
  }
  console.log(`OK: ${caseCount} cases passed (fixture_version=${fixtures.fixture_version}).`);
}

main();
