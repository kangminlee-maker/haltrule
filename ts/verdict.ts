/**
 * Verdict — the one shape every part returns.
 *
 * A verdict is a value, never an exception: a part decides, the caller acts.
 * It is plain JSON by construction, so it can be written into an artifact
 * field, a spreadsheet cell, or a database column as it is. `message` is for
 * a person and is not part of conformance; the other four fields are.
 */

/** The spec version stamped on every verdict: draft 0 until the spec freezes. */
export const SPEC = "haltrule/0";

export type VerdictLevel = "ok" | "warning" | "halt";

export interface Verdict {
  spec: string;
  verdict: VerdictLevel;
  reason: string;
  message: string;
  resume: string | null;
}

export function verdict(level: VerdictLevel, reason: string, message: string, resume: string | null = null): Verdict {
  return { spec: SPEC, verdict: level, reason, message, resume };
}
