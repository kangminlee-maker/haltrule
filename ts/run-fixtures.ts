/**
 * Conformance runner for the fixtures under fixtures/ against
 * ts/breaker.ts, ts/checkpoint.ts, ts/budget.ts and ts/slot.ts.
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
import { Budget, type BudgetCaps, type Charge } from "./budget.ts";
import { validateSlot, type SlotSpec } from "./slot.ts";
import type { Verdict } from "./verdict.ts";

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

/** A verdict as conformance sees it: without `message`, which is for people. */
type NormativeVerdict = Omit<Verdict, "message">;

/** What a fixture holds where an argument outside the contract was refused. */
type Refused = { refused: true };

interface ChargeCase {
  id: string;
  budget: unknown;
  charges: unknown[];
  /** One verdict per charge - or a refusal, which changes nothing - then the
   * ledger, in decimal so 2^63 - 1 survives JSON. A budget whose caps are
   * refused is a refusal alone. */
  expect:
    | { verdicts: (NormativeVerdict | Refused)[]; used: { turns: string; ms: string; tokens: string } }
    | Refused;
}

interface BudgetFixtureFile {
  fixture_version: "budget/v0";
  charge: ChargeCase[];
}

interface ValidateCase {
  id: string;
  spec: unknown;
  value: unknown;
  expect: NormativeVerdict | Refused;
}

interface SlotFixtureFile {
  fixture_version: "slot/v0";
  validate: ValidateCase[];
}

interface ResultLineCase {
  id: string;
  value: unknown;
  expect: { line: string } | { refused: true };
}

interface ProtocolFixtureFile {
  fixture_version: "protocol/v0";
  result_line: ResultLineCase[];
}

type FixtureFile =
  | BreakerFixtureFile
  | CheckpointFixtureFile
  | BudgetFixtureFile
  | SlotFixtureFile
  | ProtocolFixtureFile;

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

/** A result line, as fixtures/README.md defines it; fixtures/protocol/v0.json
 * holds its vectors. Written out member by member: JSON.stringify would put
 * an object's integer-like keys ("2", "10") first, in numeric order, whatever
 * order they were inserted in. */
function canonicalStringify(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  // JSON.stringify escapes a string as the protocol says, lone surrogates included.
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new Error(`a result holds the number ${value}; a result number is an integer within \u00b1(2^53 - 1)`);
    }
    return String(value); // -0 is written "0"
  }
  if (typeof value === "bigint") {
    // An integer is an integer however the language holds it.
    if (value > BigInt(Number.MAX_SAFE_INTEGER) || value < -BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new Error(`a result holds the number ${value}; a result number is an integer within \u00b1(2^53 - 1)`);
    }
    return value.toString();
  }
  if (Array.isArray(value)) {
    const items: string[] = [];
    // Indexed, so a hole is met as undefined and refused rather than skipped.
    for (let index = 0; index < value.length; index += 1) items.push(canonicalStringify(value[index]));
    return `[${items.join(",")}]`;
  }
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    const source = value as Record<string, unknown>;
    const keys = Object.keys(source);
    // A symbol key, or a hidden one: writing the map without it would be a different map.
    if (Reflect.ownKeys(source).length !== keys.length) {
      throw new Error("a result holds a map with a key that is not an enumerable string");
    }
    // The default sort compares UTF-16 code units, which is the protocol's order.
    const members = keys
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalStringify(source[key])}`);
    return `{${members.join(",")}}`;
  }
  throw new Error(`a result holds a value a result line cannot carry: ${typeof value}`);
}

function fail(caseId: string, field: string, expected: unknown, actual: unknown): void {
  failureCount += 1;
  console.error(
    `FAIL [${caseId}] field=${field}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`,
  );
}

const RAISED = Symbol("raised");

/** A case whose computation throws is a failure of that case, reported under
 * its id — not a crash that hides which case failed, or whether any case ran
 * at all. */
function computeOrFail<T>(caseId: string, section: string, compute: () => T): T | typeof RAISED {
  try {
    return compute();
  } catch (error) {
    fail(caseId, `${section}.raised`, null, String(error));
    return RAISED;
  }
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

const VERDICT_FIELDS = "message,reason,resume,spec,verdict";

/** Strips `message` after checking the shape the spec promises: exactly the
 * five fields, `message` a string. Its wording is not conformance; its
 * presence and type are. */
function normative(result: Verdict): NormativeVerdict {
  const fields = Object.keys(result).sort().join(",");
  if (fields !== VERDICT_FIELDS) throw new Error(`verdict has fields ${fields}, not ${VERDICT_FIELDS}`);
  if (typeof result.message !== "string") throw new Error(`verdict message is ${typeof result.message}, not a string`);
  const { message, ...rest } = result;
  void message;
  return rest;
}

const REFUSED = Symbol("refused");

/** The part's own refusal of an argument outside its contract - and only
 * that: decoding the fixture and checking the verdict's shape happen outside
 * this, so a malformed fixture or verdict is a failure, never a refusal. */
function orRefused<T>(call: () => T): T | typeof REFUSED {
  try {
    return call();
  } catch (error) {
    if (error instanceof TypeError || error instanceof RangeError) return REFUSED;
    throw error;
  }
}

function actualCharge(tc: ChargeCase): ChargeCase["expect"] {
  const caps = decodeFixtureValue(tc.budget) as BudgetCaps;
  const budget = orRefused(() => new Budget(caps));
  if (budget === REFUSED) return { refused: true };
  const verdicts = tc.charges.map((encoded): NormativeVerdict | Refused => {
    const charge = decodeFixtureValue(encoded) as Charge;
    const result = orRefused(() => budget.charge(charge));
    return result === REFUSED ? { refused: true } : normative(result);
  });
  return {
    verdicts,
    used: { turns: String(budget.turns_used), ms: String(budget.ms_used), tokens: String(budget.tokens_used) },
  };
}

function actualValidate(tc: ValidateCase): NormativeVerdict | Refused {
  const spec = decodeFixtureValue(tc.spec) as SlotSpec;
  const value = decodeFixtureValue(tc.value);
  const result = orRefused(() => validateSlot(spec, value));
  return result === REFUSED ? { refused: true } : normative(result);
}

function runClassify(cases: ClassifyCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "classify", () => actualClassify(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "classify.expect", tc.expect, actual);
    }
  }
}

function runBackoff(cases: BackoffCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "backoff", () => actualBackoff(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect_ms)) {
      fail(tc.id, "backoff.expect_ms", tc.expect_ms, actual);
    }
  }
}

function runState(cases: StateCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "state", () => actualState(tc));
    if (actual === RAISED) continue;
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

const INTEGER_LITERAL = /^-?(0|[1-9][0-9]*)$/;
// The JSON number grammar, plus the three values JSON cannot spell. Number()
// and BigInt() read more than this - "0x10", "", " 7 " - and Python's float()
// and int() read other things again - "1_0" - so neither decides.
const NUMBER_LITERAL = /^(-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?|NaN|Infinity|-Infinity)$/;

function exactlyRepresentable(literal: string, decoded: number): boolean {
  try {
    return BigInt(literal) === BigInt(decoded);
  } catch {
    return false; // decoded is infinite or fractional, so the literal was not held
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
    if (key === "$number" && typeof inner === "string") {
      if (!NUMBER_LITERAL.test(inner)) {
        throw new Error(`$number literal ${JSON.stringify(inner)} is outside the fixture grammar`);
      }
      const decoded = Number(inner);
      // An integer literal a double cannot hold rounds here and stays exact in
      // Python, so the two runners would test two different values.
      if (INTEGER_LITERAL.test(inner) && !exactlyRepresentable(inner, decoded)) {
        throw new Error(
          `$number ${inner} is not exactly representable as a double; write {"$bigint": "${inner}"} so both runners decode the same value`,
        );
      }
      return decoded;
    }
    if (key === "$bigint" && typeof inner === "string") {
      if (!INTEGER_LITERAL.test(inner)) {
        throw new Error(`$bigint literal ${JSON.stringify(inner)} is outside the fixture grammar`);
      }
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
    const actual = computeOrFail(tc.id, "canonicalize", () => actualCanonicalize(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "canonicalize.expect", tc.expect, actual);
    }
  }
}

function runCheckpoint(cases: CheckpointCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "checkpoint", () => actualCheckpoint(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "checkpoint.expect", tc.expect, actual);
    }
  }
}

function runCharge(cases: ChargeCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "charge", () => actualCharge(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "charge.expect", tc.expect, actual);
    }
  }
}

function runValidate(cases: ValidateCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "validate", () => actualValidate(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "validate.expect", tc.expect, actual);
    }
  }
}

/** The protocol's own vectors: what the serializer above writes for a value,
 * against a line written by hand from fixtures/README.md. */
function actualResultLine(tc: ResultLineCase): { line: string } | { refused: true } {
  const value = decodeFixtureValue(tc.value);
  try {
    return { line: canonicalStringify(value) };
  } catch {
    return { refused: true };
  }
}

function runResultLine(cases: ResultLineCase[]): void {
  for (const tc of cases) {
    caseCount += 1;
    const actual = computeOrFail(tc.id, "result_line", () => actualResultLine(tc));
    if (actual === RAISED) continue;
    if (!deepEqual(actual, tc.expect)) {
      fail(tc.id, "result_line.expect", tc.expect, actual);
    }
  }
}

function dumpResultLine(cases: ResultLineCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "result_line", id: tc.id, actual: actualResultLine(tc) }));
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

function dumpCharge(cases: ChargeCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "charge", id: tc.id, actual: actualCharge(tc) }));
  }
}

function dumpValidate(cases: ValidateCase[]): void {
  for (const tc of cases) {
    console.log(canonicalStringify({ section: "validate", id: tc.id, actual: actualValidate(tc) }));
  }
}

const SECTIONS: Record<string, readonly string[]> = {
  "breaker/v0": ["classify", "backoff", "state"],
  "checkpoint/v0": ["canonicalize", "checkpoint"],
  "budget/v0": ["charge"],
  "slot/v0": ["validate"],
  "protocol/v0": ["result_line"],
};

function runFile(fixtures: FixtureFile, dump: boolean): void {
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
  } else if (fixtures.fixture_version === "budget/v0") {
    if (dump) {
      dumpCharge(fixtures.charge);
    } else {
      runCharge(fixtures.charge);
    }
  } else if (fixtures.fixture_version === "slot/v0") {
    if (dump) {
      dumpValidate(fixtures.validate);
    } else {
      runValidate(fixtures.validate);
    }
  } else if (fixtures.fixture_version === "protocol/v0") {
    if (dump) {
      dumpResultLine(fixtures.result_line);
    } else {
      runResultLine(fixtures.result_line);
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
    const fixtures = JSON.parse(readFileSync(fixturePath, "utf8")) as FixtureFile;
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
