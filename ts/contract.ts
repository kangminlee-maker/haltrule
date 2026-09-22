/**
 * How a part reads an argument and refuses one outside its contract
 * (spec/contract.json). A refusal is a TypeError or a RangeError: the call
 * fails and changes nothing. Each kind of argument is read here once, for
 * every part: a rule written in one place has one place to be wrong.
 */

/** An integer however the language holds it - a finite integral number or a
 * bigint - within min..max. Read as a bigint so that one reader serves a
 * count within 2^53 - 1 and a ledger within 2^63 - 1 alike; a caller whose
 * range a number holds converts back with Number(). */
export function integer(value: unknown, what: string, min: bigint, max: bigint): bigint {
  let n: bigint;
  if (typeof value === "bigint") n = value;
  // isInteger is false for whatever is not a number, so it is the type test too.
  else if (Number.isInteger(value)) n = BigInt(value as number);
  else throw new TypeError(`${what} must be an integer, got ${String(value)}`);
  if (n < min || n > max) throw new RangeError(`${what} must be within [${min}, ${max}], got ${n}`);
  return n;
}

/** An argument that must be a string. */
export function text(value: unknown, what: string): string {
  if (typeof value !== "string") throw new TypeError(`${what} must be a string, got ${String(value)}`);
  return value;
}

/** An argument that must be a boolean. */
export function flag(value: unknown, what: string): boolean {
  if (typeof value !== "boolean") throw new TypeError(`${what} must be a boolean, got ${String(value)}`);
  return value;
}

/** Refuses `value` unless it is a map (not null, not a list) whose every key
 * is one of `fields`. */
export function checkFields(value: unknown, fields: readonly string[], what: string): void {
  // `instanceof Object` is false for null and for every value that is not an object.
  if (!(value instanceof Object) || Array.isArray(value)) {
    throw new TypeError(`${what} must be a map, got ${String(value)}`);
  }
  for (const key of Object.keys(value)) {
    if (!fields.includes(key)) throw new TypeError(`${what} has no field ${JSON.stringify(key)}`);
  }
}

/** Refuses `value` unless it is a map whose keys are among `allowed` and
 * hold every one of `required`. What Python's keyword signature and Go's
 * struct do for their ports, written once for this one. */
export function requireFields(
  value: unknown,
  allowed: readonly string[],
  required: readonly string[],
  what: string,
): void {
  checkFields(value, allowed, what);
  for (const key of required) {
    if (!(key in (value as object))) throw new TypeError(`${what} has no ${key}`);
  }
}
