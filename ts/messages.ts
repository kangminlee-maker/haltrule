/**
 * Words for people. Nothing here decides anything: a verdict's `message` and
 * a refusal's text are not part of conformance, so no fixture can tell one
 * wording from another - and the mutation run leaves this file alone for
 * that reason. Code that only words a message lives here; code that decides
 * lives in the part.
 */

/** What kind of thing a value outside the digest value model is. */
export function describe(value: unknown): string {
  if (value === undefined) return "undefined";
  if (typeof value === "object" && value !== null) {
    return `an instance of ${value.constructor?.name ?? "an unknown class"}`;
  }
  return `a ${typeof value}`;
}

/** An amount against its cap. */
export function show(used: bigint, limit: bigint | null): string {
  return `${used} of ${limit === null ? "no cap" : limit}`;
}
