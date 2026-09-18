/**
 * Conformance runner for the fixtures under fixtures/ against
 * ts/breaker.ts and ts/checkpoint.ts.
 *
 * With no path, runs every .json file under fixtures/, in path order; with
 * one, runs that file only. The file's own `fixture_version` picks the part
 * under test, and an unknown version is an error rather than zero cases
 * passed, so a fixture file added under fixtures/ is run or refused, never
 * skipped.
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
 * Fixture inputs never hold a raw JSON number: JSON.parse and Python's json
 * disagree about some (`1.0`, anything past 2^53), so both would test
 * different values. A number is written `{"$number": "<literal>"}` and each
 * runner decodes the literal itself; `{"$bigint": "<literal>"}` is an integer
 * this runner builds as a bigint; `{"$unsupported": "<kind>"}` builds a value
 * outside the digest model that JSON cannot spell. A file section, or an
 * `$unsupported` kind, this runner does not know is an error.
 *
 * Only node:fs/node:path (both stdlib) are used; no external dependencies.
 */
import { readdirSync, readFileSync } from "node:fs";
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
import {
  canonicalize,
  checkpointDigest,
  evaluateCheckpointArtifact,
  type EvaluateCheckpointArtifactArgs,
} from "./checkpoint.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_ROOT = path.join(__dirname, "..", "fixtures");

/** Every .json under fixtures/, in the order py/run_fixtures.py uses too:
 * relative paths compared as ASCII strings. */
function defaultFixturePaths(): string[] {
  return readdirSync(FIXTURE_ROOT, { recursive: true, encoding: "utf8" })
    .map((relative) => relative.split(path.sep).join("/"))
    .filter((relative) => relative.endsWith(".json"))
    .sort()
    .map((relative) => path.join(FIXTURE_ROOT, relative));
}

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

interface BreakerFixtureFile {
  fixture_version: "breaker/v0";
  classify: ClassifyCase[];
  backoff: BackoffCase[];
  state: StateCase[];
}

type CanonicalizeOutcome = { canonical: string; digest: string } | { halt: string };

interface CanonicalizeCase {
  id: string;
  input: unknown;
  expect: CanonicalizeOutcome;
}

interface CheckpointCase {
  id: string;
  args: Record<string, unknown>;
  expect: unknown[];
}

interface CheckpointFixtureFile {
  fixture_version: "checkpoint/v0";
  canonicalize: CanonicalizeCase[];
  checkpoint: CheckpointCase[];
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

/** Decode a fixture input: see the header for why numbers arrive as text. */
function decodeFixtureValue(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    throw new Error(
      `fixture input holds the raw JSON number ${value}; write {"$number": "${value}"} so both runners decode the same literal`,
    );
  }
  if (Array.isArray(value)) return value.map(decodeFixtureValue);
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 1) {
    const [key, inner] = entries[0]!;
    if (key === "$number" && typeof inner === "string") return Number(inner);
    if (key === "$bigint" && typeof inner === "string") {
      // The bigint cases exist to reach the implementation's bigint path; the
      // same value as a number would pass every one of them and test nothing.
      const decoded: unknown = BigInt(inner);
      if (typeof decoded !== "bigint") throw new Error(`$bigint ${inner} did not decode to a bigint`);
      return decoded;
    }
    if (key === "$unsupported") {
      if (inner === "undefined") return undefined;
      if (inner === "instance") return new Map();
      if (inner === "non_string_key") return { [Symbol("key")]: 1 };
      if (inner === "sparse_array") return new Array(1);
      throw new Error(`unknown $unsupported kind ${JSON.stringify(inner)}`);
    }
  }
  // fromEntries defines own properties, so a "__proto__" key stays a key.
  return Object.fromEntries(entries.map(([key, inner]) => [key, decodeFixtureValue(inner)]));
}

function actualCanonicalize(tc: CanonicalizeCase): CanonicalizeOutcome {
  const input = decodeFixtureValue(tc.input);
  const canonical = canonicalize(input);
  const digest = checkpointDigest(input);
  // The two entry points must agree about the same value; if they do not,
  // that is a bug in the implementation, not a verdict to compare.
  if ("halt" in canonical || "halt" in digest) {
    if (!("halt" in canonical && "halt" in digest && canonical.halt === digest.halt)) {
      throw new Error(`[${tc.id}] canonicalize and checkpointDigest disagree about halting`);
    }
    return { halt: canonical.halt };
  }
  return { canonical: canonical.canonical, digest: digest.digest };
}

const CHECKPOINT_ARG_NAMES: Record<string, keyof EvaluateCheckpointArtifactArgs> = {
  stage_id: "stageId",
  subject_ref: "subjectRef",
  artifact: "artifact",
  expected_contract_revision: "expectedContractRevision",
  expected_stage_config_digest: "expectedStageConfigDigest",
  expected_dependency_digests: "expectedDependencyDigests",
  required_resume_from_stage: "requiredResumeFromStage",
  validation_issues: "validationIssues",
  status_map: "statusMap",
};

function actualCheckpoint(tc: CheckpointCase): unknown[] {
  const decoded = decodeFixtureValue(tc.args) as Record<string, unknown>;
  const args: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(decoded)) {
    const name = CHECKPOINT_ARG_NAMES[key];
    if (name === undefined) throw new Error(`[${tc.id}] unknown checkpoint arg ${key}`);
    args[name] = value;
  }
  return evaluateCheckpointArtifact(args as unknown as EvaluateCheckpointArtifactArgs);
}

function runCanonicalize(cases: CanonicalizeCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = actualCanonicalize(tc);
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "canonicalize.expect", tc.expect, actual);
    }
  }
}

function runCheckpoint(cases: CheckpointCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = actualCheckpoint(tc);
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "checkpoint.expect", tc.expect, actual);
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

function dumpCanonicalize(cases: CanonicalizeCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "canonicalize", id: tc.id, actual: actualCanonicalize(tc) }));
  }
}

function dumpCheckpoint(cases: CheckpointCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "checkpoint", id: tc.id, actual: actualCheckpoint(tc) }));
  }
}

const SECTIONS: Record<string, readonly string[]> = {
  "breaker/v0": ["classify", "backoff", "state"],
  "checkpoint/v0": ["canonicalize", "checkpoint"],
};

function runFile(fixtures: BreakerFixtureFile | CheckpointFixtureFile, dump: boolean): void {
  // A section no runner reads would pass with every case in it wrong.
  const known = SECTIONS[fixtures.fixture_version];
  if (known !== undefined) {
    for (const key of Object.keys(fixtures)) {
      if (key !== "fixture_version" && !known.includes(key)) {
        throw new Error(`unknown fixture section ${JSON.stringify(key)} in ${fixtures.fixture_version}`);
      }
    }
  }
  if (fixtures.fixture_version === "breaker/v0") {
    if (dump) {
      dumpClassify(fixtures.classify);
      dumpBackoff(fixtures.backoff);
      dumpState(fixtures.state);
    } else {
      runClassify(fixtures.classify);
      runBackoff(fixtures.backoff);
      runState(fixtures.state);
    }
  } else if (fixtures.fixture_version === "checkpoint/v0") {
    if (dump) {
      dumpCanonicalize(fixtures.canonicalize);
      dumpCheckpoint(fixtures.checkpoint);
    } else {
      runCanonicalize(fixtures.canonicalize);
      runCheckpoint(fixtures.checkpoint);
    }
  } else {
    throw new Error(`unknown fixture_version ${JSON.stringify((fixtures as { fixture_version: unknown }).fixture_version)}`);
  }
}

function main(): void {
  // A runner that silently ignores the path it was handed reports PASS on a file
  // it never read, so the argument is honored here exactly as the Python runner
  // honors sys.argv[1].
  const args = process.argv.slice(2);
  const dump = args.includes("--dump");
  const positional = args.filter((a) => a !== "--dump");
  const fixturePaths = positional.length > 0 ? [positional[0]!] : defaultFixturePaths();
  const versions: string[] = [];
  for (const fixturePath of fixturePaths) {
    const fixtures = JSON.parse(readFileSync(fixturePath, "utf8")) as BreakerFixtureFile | CheckpointFixtureFile;
    runFile(fixtures, dump);
    versions.push(fixtures.fixture_version);
  }
  if (dump) return;

  const label = `fixture_version=${versions.join(", ")}`;
  if (failureCount > 0) {
    console.error(`\n${failureCount} mismatch(es) across ${caseCount} cases (${label}).`);
    process.exit(1);
  }
  console.log(`OK: ${caseCount} cases passed (${label}).`);
}

main();
