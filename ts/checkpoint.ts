/**
 * Checkpoint reuse verdict — pure policy: no I/O, no timers, no clock.
 *
 * Extracted from a production planning pipeline's checkpoint-invalidation
 * module (issue fields and reason codes unchanged, except where noted).
 * Canonicalizes a value and digests it, and decides whether an artifact
 * recorded by an earlier run may be reused: its status must map to
 * `complete`, and its recorded contract revision, stage-config digest, and
 * dependency digests must equal what the caller expects now.
 *
 * Departures from the source, each so that every language computes the same
 * answer:
 * - `canonicalize` returns a halt for anything outside the spec's value model
 *   instead of serializing it however the host language happens to (the
 *   source turned `undefined` into invalid JSON and every class instance into
 *   `{}`). Like every verdict here it is a value, never a throw: the caller
 *   records it before it stops.
 * - A string is always a value: the source digested a bare string's raw
 *   bytes, so the text `{"a":1}` and the value `{a: 1}` collided. Callers
 *   that want a digest of raw text compute it themselves.
 * - `evaluateCheckpointArtifact` takes the artifact instead of reading a
 *   path, takes the caller's status vocabulary through `status_map`, compares
 *   dependencies in UTF-16 key order rather than insertion order, and returns
 *   issues without an `issue_id` — ids and file reads stay with the caller.
 *   The status reason is `artifact_status_not_reusable` (the source's
 *   `artifact_status_not_pass` named a vocabulary this spec does not have).
 * - A recorded `dependency_digests` that is not a plain object, and a key it
 *   only inherits (`constructor`), read as absent; the source indexed into
 *   lists, strings, and the prototype chain.
 * - The arguments are typed and an argument of another type is refused; the
 *   source read them by JavaScript falsiness, under which an expectation of
 *   `""` or `0` was no expectation, and ignored validation issues that were
 *   not a list. What the artifact *records* is still read leniently: it is
 *   data, written by whoever wrote it.
 * - A null field in a caller's validation issue is an absent one. The source
 *   laid it over the default, so a null `reason` came back as null.
 */
import { createHash } from "node:crypto";
import { checkFields } from "./contract.ts";
import { describe } from "./messages.ts";
import { isVerdictLevel, verdict, type Verdict, type VerdictLevel } from "./verdict.ts";

// ---------------------------------------------------------------- canonicalize

/** Largest integer every implementation represents exactly: 2^53 − 1. */
const MAX_SAFE_INTEGER = 9007199254740991;
const MAX_SAFE_BIGINT = 9007199254740991n;

/** Deepest nesting of lists and maps a digest input may have. A bound every
 * language can reach without exhausting its stack, so all of them halt at
 * the same depth instead of each crashing at its own. */
const MAX_DEPTH = 100;

export type DigestInputReason =
  | "digest_input_float"
  | "digest_input_int_range"
  | "digest_input_unsupported";

/** A value outside the digest value model: a halt verdict whose message names
 * where the value sits (`$.a[2]`) for the person reading it, a path being no
 * thing two languages spell alike. */
export interface DigestInputHalt extends Verdict {
  verdict: "halt";
  reason: DigestInputReason;
}

/** Internal: unwinds the recursive encoder to the public boundary, where it
 * becomes a {@link DigestInputHalt} value. */
class DigestInputError extends Error {
  readonly reason: DigestInputReason;

  constructor(reason: DigestInputReason, message: string) {
    super(message);
    this.name = "DigestInputError";
    this.reason = reason;
  }
}

/**
 * Canonical form of a value: an RFC 8785 subset. Map keys are sorted by
 * UTF-16 code unit, there is no insignificant whitespace, integers are
 * written in decimal, and strings use the escape table above. Strings are
 * not Unicode-normalized: NFC and NFD spellings of the same text canonicalize
 * differently.
 *
 * Accepted: null, booleans, integers with |n| <= 2^53 − 1 whether stored as
 * a number or a bigint (so `1.0` is the integer 1 — the spec follows the
 * value, not the storage), strings of Unicode scalar values, and arrays and
 * plain objects nested at most 100 deep. Anything else returns a
 * {@link DigestInputHalt}.
 */
export function canonicalize(value: unknown): { canonical: string } | DigestInputHalt {
  try {
    return { canonical: encodeValue(value, "$", 0) };
  } catch (error) {
    if (error instanceof DigestInputError) {
      return verdict("halt", error.reason, error.message) as DigestInputHalt;
    }
    throw error;
  }
}

/** `sha256:` + lowercase hex SHA-256 of the canonical form's UTF-8 bytes —
 * the same value `printf '%s' "$canonical" | sha256sum` prints — or the
 * halt {@link canonicalize} returned. */
export function checkpointDigest(value: unknown): { digest: string } | DigestInputHalt {
  const result = canonicalize(value);
  if ("verdict" in result) return result;
  return { digest: `sha256:${createHash("sha256").update(result.canonical, "utf8").digest("hex")}` };
}

function encodeValue(value: unknown, at: string, depth: number): string {
  if (value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") return encodeNumber(value, at);
  if (typeof value === "bigint") return encodeBigInt(value, at);
  if (typeof value === "string") return encodeString(value, at);
  if (!Array.isArray(value) && !isPlainObject(value)) {
    throw new DigestInputError(
      "digest_input_unsupported",
      `${at}: ${describe(value)} is outside the digest value model`,
    );
  }
  if (depth >= MAX_DEPTH) {
    throw new DigestInputError(
      "digest_input_unsupported",
      `${at}: nested deeper than ${MAX_DEPTH} lists and maps`,
    );
  }
  if (Array.isArray(value)) {
    const parts: string[] = [];
    // An index loop, not map(): map() skips holes, and a hole must be
    // rejected like any other undefined rather than silently dropped.
    for (let index = 0; index < value.length; index += 1) {
      parts.push(encodeValue(value[index], `${at}[${index}]`, depth + 1));
    }
    return `[${parts.join(",")}]`;
  }
  if (Object.getOwnPropertySymbols(value).length > 0) {
    throw new DigestInputError("digest_input_unsupported", `${at}: map has a symbol key`);
  }
  // The default sort compares UTF-16 code units, which is the spec's order.
  const keys = Object.keys(value).sort();
  const parts = keys.map(
    (key) => `${encodeString(key, `${at} key`)}:${encodeValue(value[key], `${at}.${key}`, depth + 1)}`,
  );
  return `{${parts.join(",")}}`;
}

function encodeNumber(value: number, at: string): string {
  if (!Number.isFinite(value) || !Number.isInteger(value)) {
    throw new DigestInputError(
      "digest_input_float",
      `${at}: ${String(value)} is not an integer; render it as a string if it belongs in a digest`,
    );
  }
  if (Math.abs(value) > MAX_SAFE_INTEGER) {
    throw new DigestInputError(
      "digest_input_int_range",
      `${at}: ${String(value)} is outside ±(2^53 − 1)`,
    );
  }
  // String(-0) is "0": zero is written without a sign.
  return String(value);
}

function encodeBigInt(value: bigint, at: string): string {
  if (value > MAX_SAFE_BIGINT || value < -MAX_SAFE_BIGINT) {
    throw new DigestInputError(
      "digest_input_int_range",
      `${at}: ${value.toString()} is outside ±(2^53 − 1)`,
    );
  }
  return value.toString();
}

/** A string of Unicode scalar values, quoted and escaped as the spec says:
 * seven two-character escapes, every other code point below U+0020 as
 * `\u00xx` in lowercase hex, everything else as itself. For a well-formed
 * string that is exactly what JSON.stringify writes; the spec spells the
 * table out for a port whose language has no such function. */
function encodeString(value: string, at: string): string {
  if (!value.isWellFormed()) {
    throw new DigestInputError("digest_input_unsupported", `${at}: string has an unpaired surrogate`);
  }
  return JSON.stringify(value);
}

/** A map as JSON.parse and an object literal make one. `instanceof Object` is
 * false for null and for every value that is not an object. */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value instanceof Object && Object.getPrototypeOf(value) === Object.prototype;
}

// ------------------------------------------------------------ reuse verdict

/** The status vocabulary every artifact is read through. Only `complete` is
 * reusable. */
export type ArtifactStatus = "complete" | "partial" | "failed" | "blocked";

/** Used when the caller passes no statusMap: the vocabulary maps to itself. */
const IDENTITY_STATUS_MAP: Readonly<Record<string, ArtifactStatus>> = {
  complete: "complete",
  partial: "partial",
  failed: "failed",
  blocked: "blocked",
};

/** One verdict about an artifact: the five fields, with stage_id and
 * subject_ref beside them, and what its reason adds. A caller's own issue may
 * lay any of them over the defaults, so the extra fields are open. */
export interface CheckpointIssue extends Verdict {
  stage_id: string;
  subject_ref: string | null;
  [detail: string]: unknown;
}

/** The arguments, under the names the fixtures and every port use. Null and
 * not given are the same thing: absent. */
export interface EvaluateCheckpointArtifactArgs {
  stage_id: string;
  /** How issues refer to the artifact (a repo-relative path, a key). */
  subject_ref?: string | null;
  /** The artifact as recorded; absent when it does not exist. */
  artifact?: Readonly<Record<string, unknown>> | null;
  /** What the caller expects now. Absent: not checked. Any string is
   * compared, the empty one included. */
  expected_contract_revision?: string | null;
  expected_stage_config_digest?: string | null;
  expected_dependency_digests?: Readonly<Record<string, string>> | null;
  /** Where a rerun must start when this artifact is not reusable; defaults
   * to stage_id. */
  required_resume_from_stage?: string | null;
  /** Issues the caller's own validation found; each becomes an `invalid`
   * issue, with its own fields laid over the defaults. */
  validation_issues?: readonly Readonly<Record<string, unknown>>[] | null;
  /** The caller's status values mapped onto the vocabulary. Replaces the
   * default identity map; a status it does not name is not reusable. */
  status_map?: Readonly<Record<string, ArtifactStatus>> | null;
}

const ARGUMENT_FIELDS = [
  "stage_id",
  "subject_ref",
  "artifact",
  "expected_contract_revision",
  "expected_stage_config_digest",
  "expected_dependency_digests",
  "required_resume_from_stage",
  "validation_issues",
  "status_map",
];

/** An optional argument: null when absent, itself when it is what the
 * contract says, refused otherwise. */
function optional<T>(value: unknown, holds: (value: unknown) => value is T, what: string): T | null {
  if (value === null || value === undefined) return null;
  if (holds(value)) return value;
  throw new TypeError(`${what} is outside the contract`);
}

const isText = (value: unknown): value is string => typeof value === "string";
const isArtifactStatus = (value: unknown): value is ArtifactStatus =>
  isText(value) && Object.hasOwn(IDENTITY_STATUS_MAP, value);
const mapOf =
  <T>(holds: (value: unknown) => value is T) =>
  (value: unknown): value is Record<string, T> =>
    isPlainObject(value) && Object.values(value).every(holds);
const isListOfMaps = (value: unknown): value is Record<string, unknown>[] =>
  Array.isArray(value) && value.every(isPlainObject);

/**
 * Decide whether a recorded artifact may be reused. Returns every issue
 * found, in this order: status, contract revision, stage-config digest,
 * dependency digests (by UTF-16 key order), the caller's validation issues.
 * With none, returns one `valid` issue, so the result is never empty.
 */
export function evaluateCheckpointArtifact(args: EvaluateCheckpointArtifactArgs): CheckpointIssue[] {
  checkFields(args, ARGUMENT_FIELDS, "checkpoint arguments");
  const stageId: unknown = args.stage_id;
  if (!isText(stageId)) throw new TypeError("stage_id must be a string");
  const subjectRef = optional(args.subject_ref, isText, "subject_ref");
  const requiredResumeFromStage =
    optional(args.required_resume_from_stage, isText, "required_resume_from_stage") ?? stageId;
  const expectedRevision = optional(args.expected_contract_revision, isText, "expected_contract_revision");
  const expectedConfig = optional(args.expected_stage_config_digest, isText, "expected_stage_config_digest");
  const expectedDependencies =
    optional(args.expected_dependency_digests, mapOf(isText), "expected_dependency_digests") ?? {};
  const statusMap = optional(args.status_map, mapOf(isArtifactStatus), "status_map") ?? IDENTITY_STATUS_MAP;
  const validationIssues = optional(args.validation_issues, isListOfMaps, "validation_issues") ?? [];
  const artifact = optional(args.artifact, isPlainObject, "artifact");
  const base = (
    level: VerdictLevel,
    reason: string,
    message: string,
    details: Record<string, unknown> = {},
  ): CheckpointIssue => ({
    ...verdict(level, reason, `${stageId}: ${message}`, requiredResumeFromStage),
    stage_id: stageId,
    subject_ref: subjectRef,
    ...details,
  });

  if (artifact === null) return [base("halt", "artifact_missing", "nothing was recorded")];

  const issues: CheckpointIssue[] = [];
  const status = artifact.status;
  if (!status) {
    issues.push(base("halt", "artifact_status_missing", "what was recorded has no status"));
  } else if (resolveStatus(status, statusMap) !== "complete") {
    issues.push(
      base("halt", "artifact_status_not_reusable", "the recorded status is not one that may be reused", {
        actual_status: status,
      }),
    );
  }

  if (expectedRevision !== null && !artifact.contract_revision) {
    issues.push(
      base(
        "halt",
        "contract_revision_missing",
        `a contract revision of "${expectedRevision}" is expected and none is recorded`,
        { expected_contract_revision: expectedRevision },
      ),
    );
  } else if (expectedRevision !== null && artifact.contract_revision !== expectedRevision) {
    issues.push(
      base(
        "halt",
        "contract_revision_mismatch",
        `the recorded contract revision is not the expected "${expectedRevision}"`,
        {
          expected_contract_revision: expectedRevision,
          actual_contract_revision: artifact.contract_revision,
        },
      ),
    );
  }

  if (expectedConfig !== null && artifact.stage_config_digest !== expectedConfig) {
    issues.push(
      base(
        "halt",
        "stage_config_digest_mismatch",
        `the recorded stage config digest is not the expected "${expectedConfig}"`,
        {
          expected_stage_config_digest: expectedConfig,
          actual_stage_config_digest: artifact.stage_config_digest ?? null,
        },
      ),
    );
  }

  const recordedDependencies = artifact.dependency_digests;
  for (const dependencyId of Object.keys(expectedDependencies).sort()) {
    const expectedDigest = expectedDependencies[dependencyId];
    const actualDigest =
      isPlainObject(recordedDependencies) && Object.hasOwn(recordedDependencies, dependencyId)
        ? (recordedDependencies[dependencyId] ?? null)
        : null;
    if (actualDigest !== expectedDigest) {
      issues.push(
        base("halt", "dependency_digest_mismatch", `the dependency "${dependencyId}" moved`, {
          dependency_id: dependencyId,
          expected_digest: expectedDigest,
          actual_digest: actualDigest,
        }),
      );
    }
  }

  for (const validationIssue of validationIssues) {
    // A null field is an absent one: the default stands where there is one.
    const given = Object.entries(validationIssue).filter(([, field]) => field != null);
    for (const [key, field] of given) {
      // A caller may disagree with a verdict, not sign one, and the three are
      // as closed a set here as they are anywhere else.
      if (key === "spec") throw new TypeError("a validation issue carries a spec, which only the library says");
      if (key === "verdict" && !isVerdictLevel(field)) {
        throw new TypeError("a validation issue's verdict is not one of the three");
      }
    }
    issues.push({
      ...base("halt", "validation_issue", "the caller's own validation found something"),
      ...Object.fromEntries(given),
    });
  }

  if (issues.length === 0) {
    return [{ ...base("ok", "checkpoint_valid", "the artifact may be reused"), resume: null }];
  }
  return issues;
}

function resolveStatus(
  status: unknown,
  statusMap: Readonly<Record<string, ArtifactStatus>>,
): ArtifactStatus | null {
  // Own keys only: a status of "constructor" must not find Object's.
  if (typeof status !== "string" || !Object.hasOwn(statusMap, status)) return null;
  return statusMap[status] ?? null;
}
