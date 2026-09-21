/**
 * Slot — does a value a person or a model filled in satisfy its contract?
 *
 * Lifted from three pipelines' checks on submitted values: a spreadsheet
 * adjudication that accepts only a pick from a closed set, a workflow worker
 * that rejects a model-filled slot outside its contract before anything is
 * written, and a hold flow that takes a person's own sentence and checks only
 * its display contract. Three kinds cover them: `choice`, a value that must
 * be one of the candidates exactly, `text`, a value of the person's own
 * within length bounds, and `score`, a number held against a bar. A
 * reference to something that exists is a `choice` whose candidates are the
 * known identifiers.
 *
 * A missing value is a warning: the judgment has not been made yet. A present
 * value that fails its contract is a halt: it would be written to a ledger as
 * if it were valid. Missing is null, or a string that is empty or holds only
 * ASCII whitespace. Comparison is exact — no trimming, no case folding, no
 * Unicode normalization; a caller that wants those applies them first.
 * Lengths count Unicode scalar values, so every language counts the same,
 * and a string that is not made of them (it holds a lone surrogate) fails
 * its contract. A shape beyond length — a UUID, a URL — is the caller's to
 * check, as a float's rendering is the caller's in a digest. A score is only
 * compared: rounding it, summing it or weighting it is the caller's, done
 * first, where it can be reviewed. A spec holds no field beyond the seven
 * below; one that does, or is not a map, is refused.
 */
import { checkFields } from "./contract.ts";
import { verdict, type Verdict } from "./verdict.ts";

export type SlotKind = "choice" | "text" | "score";

export interface SlotSpec {
  name: string;
  kind: SlotKind;
  /** choice: the values accepted, compared exactly. */
  candidates?: readonly string[];
  /** text: bounds on the length in Unicode scalar values, each in 0..2^53 - 1; null or absent for none. */
  min_length?: number | null;
  max_length?: number | null;
  /** score: the bar, both ends included; null or absent for none. */
  min?: number | bigint | null;
  max?: number | bigint | null;
}

const SPEC_FIELDS = ["name", "kind", "candidates", "min_length", "max_length", "min", "max"];

const ASCII_WHITESPACE = " \t\n\r\f\v";

function isBlank(text: string): boolean {
  for (const character of text) {
    if (!ASCII_WHITESPACE.includes(character)) return false;
  }
  return true;
}

/** A bound in 0..2^53 - 1, or `absent` when none is given. */
function bound(value: unknown, what: string, absent: number): number {
  if (value == null) return absent;
  // isSafeInteger is false for whatever is not a number, so it is the type test too.
  if (Number.isSafeInteger(value) && (value as number) >= 0) return value as number;
  throw new TypeError(`${what} must be a non-negative integer up to 2^53 - 1, got ${String(value)}`);
}

/** A number as this spec has it, as a double, or null when it is not one: a
 * finite double, and an integral one an integer within +/-(2^53 - 1), as every
 * other number in the spec is, a bigint included. Every double past 2^53 - 1
 * is integral, so that is one range test, which NaN and the infinities fail
 * too; within it a bigint converts exactly. */
function numberOf(value: unknown): number | null {
  if (typeof value === "bigint") {
    return value >= -BigInt(Number.MAX_SAFE_INTEGER) && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : null;
  }
  return typeof value === "number" && Math.abs(value) <= Number.MAX_SAFE_INTEGER ? value : null;
}

/** A score bound, or `absent` when none is given. */
function bar(value: unknown, what: string, absent: number): number {
  if (value == null) return absent;
  const number = numberOf(value);
  if (number !== null) return number;
  throw new TypeError(`${what} must be a finite number, an integral one within 2^53 - 1, got ${String(value)}`);
}

export function validateSlot(spec: SlotSpec, value: unknown): Verdict {
  checkFields(spec, SPEC_FIELDS, "slot spec");
  const name = spec.name;
  if (typeof name !== "string") throw new TypeError(`slot spec without a string name: ${String(name)}`);
  if (spec.kind !== "choice" && spec.kind !== "text" && spec.kind !== "score") {
    throw new TypeError(`slot ${name}: unknown kind ${JSON.stringify(spec.kind)}`);
  }
  // A field that is given is held to its type whatever the kind, as the bounds are below.
  const given: unknown = spec.candidates ?? null;
  if (given !== null && !(Array.isArray(given) && given.every((candidate) => typeof candidate === "string"))) {
    throw new TypeError(`slot ${name}: candidates must be a list of strings`);
  }
  if (spec.kind === "choice" && given === null) throw new TypeError(`slot ${name}: a choice needs candidates`);
  // No bound is the bound every length meets: at least 0, at most infinity.
  const min = bound(spec.min_length, `slot ${name}: min_length`, 0);
  const max = bound(spec.max_length, `slot ${name}: max_length`, Infinity);
  if (min > max) {
    throw new RangeError(`slot ${name}: min_length ${min} exceeds max_length ${max}`);
  }
  const floor = bar(spec.min, `slot ${name}: min`, -Infinity);
  const ceiling = bar(spec.max, `slot ${name}: max`, Infinity);
  if (floor > ceiling) throw new RangeError(`slot ${name}: min ${floor} exceeds max ${ceiling}`);

  if (value == null) return verdict("warning", "slot_missing", `slot ${name}: no value`);
  if (spec.kind === "score" && typeof value !== "string") {
    const score = numberOf(value);
    if (score === null) {
      return verdict("halt", "slot_invalid", `slot ${name}: a ${typeof value} that is not a number this spec holds`);
    }
    if (score < floor) return verdict("halt", "slot_invalid", `slot ${name}: ${score} is below ${floor}`);
    if (score > ceiling) return verdict("halt", "slot_invalid", `slot ${name}: ${score} is above ${ceiling}`);
    return verdict("ok", "slot_accepted", `slot ${name}: ${score}`);
  }
  if (typeof value !== "string") return verdict("halt", "slot_invalid", `slot ${name}: a ${typeof value} is not a string`);
  if (!value.isWellFormed()) {
    return verdict("halt", "slot_invalid", `slot ${name}: not a string of Unicode scalar values (a lone surrogate)`);
  }
  if (isBlank(value)) return verdict("warning", "slot_missing", `slot ${name}: blank`);
  if (spec.kind === "score") {
    return verdict("halt", "slot_invalid", `slot ${name}: a string is not a number, however it reads`);
  }

  if (spec.kind === "choice") {
    const candidates = spec.candidates as readonly string[];
    return candidates.includes(value)
      ? verdict("ok", "slot_accepted", `slot ${name}: ${JSON.stringify(value)} is a candidate`)
      : verdict("halt", "slot_invalid", `slot ${name}: ${JSON.stringify(value)} is not one of ${candidates.length} candidates`);
  }
  const length = Array.from(value).length;
  if (length < min) {
    return verdict("halt", "slot_invalid", `slot ${name}: ${length} characters, fewer than ${min}`);
  }
  if (length > max) {
    return verdict("halt", "slot_invalid", `slot ${name}: ${length} characters, more than ${max}`);
  }
  return verdict("ok", "slot_accepted", `slot ${name}: ${length} characters`);
}
