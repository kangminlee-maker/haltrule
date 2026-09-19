/**
 * Slot — does a value a person or a model filled in satisfy its contract?
 *
 * Lifted from three pipelines' checks on submitted values: a spreadsheet
 * adjudication that accepts only a pick from a closed set, a workflow worker
 * that rejects a model-filled slot outside its contract before anything is
 * written, and a hold flow that takes a person's own sentence and checks only
 * its display contract. Two kinds cover them: `choice`, a value that must be
 * one of the candidates exactly, and `text`, a value of the person's own
 * within length bounds. A reference to something that exists is a `choice`
 * whose candidates are the known identifiers.
 *
 * A missing value is a warning: the judgment has not been made yet. A present
 * value that fails its contract is a halt: it would be written to a ledger as
 * if it were valid. Missing is null, or a string that is empty or holds only
 * ASCII whitespace. Comparison is exact — no trimming, no case folding, no
 * Unicode normalization; a caller that wants those applies them first.
 * Lengths count Unicode scalar values, so every language counts the same,
 * and a string that is not made of them (it holds a lone surrogate) fails
 * its contract. A shape beyond length — a UUID, a URL — is the caller's to
 * check, as a float's rendering is the caller's in a digest.
 */
import { verdict, type Verdict } from "./verdict.ts";

export type SlotKind = "choice" | "text";

export interface SlotSpec {
  name: string;
  kind: SlotKind;
  /** choice: the values accepted, compared exactly. */
  candidates?: readonly string[];
  /** text: bounds on the length in Unicode scalar values, each in 0..2^53 - 1; null or absent for none. */
  min_length?: number | null;
  max_length?: number | null;
}

const ASCII_WHITESPACE = " \t\n\r\f\v";

function isBlank(text: string): boolean {
  for (const character of text) {
    if (!ASCII_WHITESPACE.includes(character)) return false;
  }
  return true;
}

/** True when a UTF-16 code unit sequence holds a surrogate without its pair. */
function hasLoneSurrogate(text: string): boolean {
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        index += 1;
        continue;
      }
      return true;
    }
    if (unit >= 0xdc00 && unit <= 0xdfff) return true;
  }
  return false;
}

function bound(value: number | null | undefined, what: string): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  throw new TypeError(`${what} must be a non-negative integer up to 2^53 - 1, got ${String(value)}`);
}

export function validateSlot(spec: SlotSpec, value: unknown): Verdict {
  const name = spec.name;
  if (typeof name !== "string") throw new TypeError(`slot spec without a string name: ${String(name)}`);
  if (spec.kind !== "choice" && spec.kind !== "text") {
    throw new TypeError(`slot ${name}: unknown kind ${JSON.stringify(spec.kind)}`);
  }
  if (spec.kind === "choice" && !Array.isArray(spec.candidates)) {
    throw new TypeError(`slot ${name}: a choice needs candidates`);
  }
  const min = bound(spec.min_length, `slot ${name}: min_length`);
  const max = bound(spec.max_length, `slot ${name}: max_length`);
  if (min !== null && max !== null && min > max) {
    throw new RangeError(`slot ${name}: min_length ${min} exceeds max_length ${max}`);
  }

  if (value === null || value === undefined) return verdict("warning", "slot_missing", `slot ${name}: no value`);
  if (typeof value !== "string") return verdict("halt", "slot_invalid", `slot ${name}: a ${typeof value} is not a string`);
  if (hasLoneSurrogate(value)) {
    return verdict("halt", "slot_invalid", `slot ${name}: not a string of Unicode scalar values (a lone surrogate)`);
  }
  if (isBlank(value)) return verdict("warning", "slot_missing", `slot ${name}: blank`);

  if (spec.kind === "choice") {
    const candidates = spec.candidates as readonly string[];
    return candidates.includes(value)
      ? verdict("ok", "slot_accepted", `slot ${name}: ${JSON.stringify(value)} is a candidate`)
      : verdict("halt", "slot_invalid", `slot ${name}: ${JSON.stringify(value)} is not one of ${candidates.length} candidates`);
  }
  const length = Array.from(value).length;
  if (min !== null && length < min) {
    return verdict("halt", "slot_invalid", `slot ${name}: ${length} characters, fewer than ${min}`);
  }
  if (max !== null && length > max) {
    return verdict("halt", "slot_invalid", `slot ${name}: ${length} characters, more than ${max}`);
  }
  return verdict("ok", "slot_accepted", `slot ${name}: ${length} characters`);
}
