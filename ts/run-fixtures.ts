/**
 * Conformance runner for fixtures/breaker/v0.json against ts/breaker.ts.
 *
 * Loads the fixture file, feeds each case to the pure policy functions, and
 * diffs the actual result against the fixture's expected value. Exits
 * non-zero and prints every mismatch (fixture id + field + expected/actual)
 * on any failure — this is the instrument the "corrupt one expectation and
 * confirm it fails" check in fixtures/README.md exercises.
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

function fail(caseId: string, field: string, expected: unknown, actual: unknown): void {
  failureCount += 1;
  console.error(
    `FAIL [${caseId}] field=${field}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`,
  );
}

function runClassify(cases: ClassifyCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    // `message` may be a non-string (e.g. a JSON number) to exercise the
    // classifier's typeof guard — cast through unknown, matching how an
    // untyped caller could hand it anything.
    const actual = classifySystemicDispatchFailure(tc.message as unknown as string | null);
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "classify.expect", tc.expect, actual);
    }
  }
}

function runBackoff(cases: BackoffCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = dispatchBackoffDelayMs({
      attempt: tc.attempt,
      initialMs: tc.initial_ms,
      capMs: tc.cap_ms,
    });
    if (!deepEqual(actual, tc.expect_ms)) {
      fail(tc.id, "backoff.expect_ms", tc.expect_ms, actual);
    }
  }
}

function runState(cases: StateCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
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
    if (!deepEqual(returns, tc.expect.returns)) {
      fail(tc.id, "state.returns", tc.expect.returns, returns);
    }
    const completed = [...state.completedItemIds()];
    if (!deepEqual(completed, tc.expect.completed)) {
      fail(tc.id, "state.completed", tc.expect.completed, completed);
    }
    const deadLetter = [...state.deadLetterEntries()];
    if (!deepEqual(deadLetter, tc.expect.dead_letter)) {
      fail(tc.id, "state.dead_letter", tc.expect.dead_letter, deadLetter);
    }
    const tripped = state.tripped();
    if (!deepEqual(tripped, tc.expect.tripped)) {
      fail(tc.id, "state.tripped", tc.expect.tripped, tripped);
    }
  }
}

function main(): void {
  // A runner that silently ignores the path it was handed reports PASS on a file
  // it never read, so the argument is honored here exactly as the Python runner
  // honors sys.argv[1].
  const fixturePath = process.argv[2] ?? DEFAULT_FIXTURE_PATH;
  const raw = readFileSync(fixturePath, "utf8");
  const fixtures = JSON.parse(raw) as FixtureFile;

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
