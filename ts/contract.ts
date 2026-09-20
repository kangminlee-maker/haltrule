/**
 * How a part reads a map argument — budget caps, a charge, a slot spec, the
 * checkpoint's arguments — and refuses one outside its contract. A refusal
 * is a TypeError: the call fails and changes nothing.
 *
 * Python needs no counterpart: its entry points take keyword arguments, and
 * the call itself refuses a non-map and a key the signature does not name.
 */

/** Refuses `value` unless it is a map (not null, not a list) whose every key
 * is one of `fields`. */
export function checkFields(value: unknown, fields: readonly string[], what: string): void {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${what} must be a map, got ${value === null ? "null" : typeof value}`);
  }
  for (const key of Object.keys(value)) {
    if (!fields.includes(key)) throw new TypeError(`${what} has no field ${JSON.stringify(key)}`);
  }
}
