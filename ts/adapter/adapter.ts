/**
 * The TypeScript adapter: reads the fixture files, feeds each case to the
 * modules under ts/, and prints one result line per case. It judges nothing —
 * scripts/conform.py compares the lines with what the fixtures expect, for
 * every language alike — and it is the only place that knows how a fixture's
 * fields map to this port's functions.
 *
 * With no argument it reads every .json file under fixtures/, by path; given
 * paths, those. A line is {"actual": <result>, "id", "section"}, or "raised"
 * in place of "actual" when the case threw something that is not a refusal.
 * The line format and the $number / $bigint / $unsupported tags are defined in
 * fixtures/README.md; the driver has already checked every literal's spelling.
 *
 * It lives in its own directory because it may use Node (files, paths) and
 * the modules it tests may not: ts/tsconfig.json compiles those with no host
 * types at all, and this directory's tsconfig.json adds Node's.
 */
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  classifySystemicDispatchFailure,
  dispatchBackoffDelayMs,
  DispatchBreakerState,
  type DispatchBreakerPolicy,
  type DispatchBreakerTripState,
  type DispatchDeadLetterEntry,
} from "../breaker.ts";
import { Budget, type BudgetCaps, type Charge } from "../budget.ts";
import {
  canonicalize,
  checkpointDigest,
  evaluateCheckpointArtifact,
  type DigestInputHalt,
  type EvaluateCheckpointArtifactArgs,
} from "../checkpoint.ts";
import { validateSlot, type SlotSpec } from "../slot.ts";
import type { Verdict } from "../verdict.ts";

const FIXTURE_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "fixtures");

// ------------------------------------------------------------------ the line

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

// ------------------------------------------------------------ fixture inputs

const UNSUPPORTED: Record<string, () => unknown> = {
  undefined: () => undefined,
  instance: () => new Map(),
  non_string_key: () => ({ [Symbol("key")]: 1 }),
  sparse_array: () => new Array(1),
};

function decode(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decode);
  if (value === null || typeof value !== "object") return value;
  const entries = Object.entries(value);
  if (entries.length === 1) {
    const [key, inner] = entries[0]!;
    if (key === "$number") return Number(inner);
    if (key === "$bigint") return BigInt(inner as string);
    if (key === "$unsupported") return UNSUPPORTED[inner as string]!();
  }
  // fromEntries defines own properties, so a "__proto__" key stays a key.
  return Object.fromEntries(entries.map(([key, inner]) => [key, decode(inner)]));
}

// ------------------------------------------------- one function per section

type Inputs = Record<string, unknown>;
type Refused = { refused: true };
const REFUSED = Symbol("refused");

/** The part's own refusal of an argument outside its contract, and only that.
 * Inputs are decoded before any section function runs, and a verdict's shape
 * is checked outside this, so neither can pass as a refusal. */
function orRefused<T>(call: () => T): T | typeof REFUSED {
  try {
    return call();
  } catch (error) {
    if (error instanceof TypeError || error instanceof RangeError) return REFUSED;
    throw error;
  }
}

const VERDICT_FIELDS = "message,reason,resume,spec,verdict";

/** Strips `message` after checking the shape the spec promises: exactly the
 * five fields, `message` a string. Its wording is not conformance; its
 * presence and type are. */
function normative(result: Verdict): Omit<Verdict, "message"> {
  const fields = Object.keys(result).sort().join(",");
  if (fields !== VERDICT_FIELDS) throw new Error(`verdict has fields ${fields}, not ${VERDICT_FIELDS}`);
  if (typeof result.message !== "string") throw new Error(`verdict message is ${typeof result.message}, not a string`);
  const { message, ...rest } = result;
  void message;
  return rest;
}

/** The same for a verdict carrying the fields its reason names beside the
 * five: those may be there, the five must be, and `message` still goes. */
function normativeOpen(given: object): Record<string, unknown> {
  const result = given as Record<string, unknown>;
  for (const field of VERDICT_FIELDS.split(",")) {
    if (!(field in result)) throw new Error(`a verdict has no ${field}`);
  }
  if (typeof result.message !== "string") throw new Error(`verdict message is ${typeof result.message}, not a string`);
  const { message, ...rest } = result;
  void message;
  return rest;
}

function classify(tc: Inputs): unknown {
  const result = orRefused(() => classifySystemicDispatchFailure(tc.message as string | null));
  return result === REFUSED ? { refused: true } : result;
}

function backoff(tc: Inputs): unknown {
  // The case's own map is the argument: a field the contract does not name
  // reaches the part, as it would from a caller.
  const result = orRefused(() => dispatchBackoffDelayMs(tc as Parameters<typeof dispatchBackoffDelayMs>[0]));
  return result === REFUSED ? { refused: true } : result;
}

function state(tc: Inputs): unknown {
  const machine = orRefused(() => new DispatchBreakerState(tc.policy as DispatchBreakerPolicy));
  if (machine === REFUSED) return { refused: true };
  const returns: (Record<string, unknown> | null | Refused)[] = [];
  for (const event of tc.events as ({ kind: string } & DispatchDeadLetterEntry)[]) {
    // A refused report leaves the batch as it was, as a refused charge leaves
    // the ledger: the answer is the refusal and the next report goes on.
    let result: DispatchBreakerTripState | null | typeof REFUSED;
    if (event.kind === "success") {
      result = orRefused(() => {
        machine.recordItemSuccess(event.item_id);
        return null;
      });
    } else if (event.kind === "skipped") {
      result = orRefused(() => {
        machine.recordItemSkipped(event.item_id);
        return null;
      });
    } else {
      const { kind, ...entry } = event;
      void kind;
      result = orRefused(() => machine.recordItemFailure(entry));
    }
    returns.push(result === REFUSED ? { refused: true } : result && normativeOpen(result));
  }
  const tripped = machine.tripped();
  return {
    returns,
    completed: [...machine.completedItemIds()],
    dead_letter: [...machine.deadLetterEntries()],
    tripped: tripped && normativeOpen(tripped),
  };
}

function canonicalizeCase(tc: Inputs): unknown {
  const canonical = canonicalize(tc.input);
  const digest = checkpointDigest(tc.input);
  // The two entry points must agree about the same value; if they do not,
  // that is a bug in the implementation, not a result to compare.
  if ("verdict" in canonical || "verdict" in digest) {
    // They must agree in the whole verdict, not only the reason: only one of
    // the two is printed, so a field this comparison leaves out is a field no
    // case can see.
    const shown = "verdict" in canonical ? canonicalStringify(normative(canonical)) : "";
    const other = "verdict" in digest ? canonicalStringify(normative(digest)) : "";
    if (shown !== other) {
      throw new Error(`canonicalize halts with ${shown} and the digest with ${other}`);
    }
    return normative(canonical as DigestInputHalt);
  }
  return { canonical: canonical.canonical, digest: digest.digest };
}

function checkpoint(tc: Inputs): unknown {
  const result = orRefused(() => evaluateCheckpointArtifact(tc.args as EvaluateCheckpointArtifactArgs));
  return result === REFUSED ? { refused: true } : result.map(normativeOpen);
}

function charge(tc: Inputs): unknown {
  const budget = orRefused(() => new Budget(tc.budget as BudgetCaps));
  if (budget === REFUSED) return { refused: true };
  const verdicts = (tc.charges as Charge[]).map((amounts): Omit<Verdict, "message"> | Refused => {
    const result = orRefused(() => budget.charge(amounts));
    return result === REFUSED ? { refused: true } : normative(result);
  });
  // The ledger in decimal, so 2^63 - 1 survives JSON in every language.
  return {
    verdicts,
    used: { turns: String(budget.turns_used), ms: String(budget.ms_used), tokens: String(budget.tokens_used) },
  };
}

function validate(tc: Inputs): unknown {
  const result = orRefused(() => validateSlot(tc.spec as SlotSpec, tc.value));
  return result === REFUSED ? { refused: true } : normative(result);
}

/** The line format's own vectors: what this adapter writes for a value. */
function resultLine(tc: Inputs): unknown {
  try {
    return { line: canonicalStringify(tc.value) };
  } catch {
    return { refused: true };
  }
}

const SECTIONS: Record<string, (tc: Inputs) => unknown> = {
  classify,
  backoff,
  state,
  canonicalize: canonicalizeCase,
  checkpoint,
  charge,
  validate,
  result_line: resultLine,
};

function main(): void {
  const given = process.argv.slice(2);
  const paths =
    given.length > 0
      ? given
      : readdirSync(FIXTURE_ROOT, { recursive: true, encoding: "utf8" })
          .map((relative) => relative.split(path.sep).join("/"))
          .filter((relative) => relative.endsWith(".json"))
          .sort()
          .map((relative) => path.join(FIXTURE_ROOT, relative));
  for (const fixturePath of paths) {
    const fixtures = JSON.parse(readFileSync(fixturePath, "utf8")) as Record<string, unknown>;
    for (const [section, cases] of Object.entries(fixtures)) {
      if (section === "fixture_version") continue;
      const compute = SECTIONS[section];
      // An unknown section stops the run; the driver counts the lines.
      if (compute === undefined) throw new Error(`no function for section ${section}`);
      for (const tc of cases as (Inputs & { id: string })[]) {
        const { id, expect, ...encoded } = tc;
        void expect;
        let text: string;
        try {
          text = canonicalStringify({ actual: compute(decode(encoded) as Inputs), id, section });
        } catch (error) {
          // Reported under the case's id, never hidden.
          text = canonicalStringify({ id, raised: String(error), section });
        }
        console.log(text);
      }
    }
  }
}

main();
