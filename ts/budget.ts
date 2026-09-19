/**
 * Budget — a ledger of turns, time, and tokens against caps. Pure: it never
 * reads a clock or counts a token; the caller charges what it measured.
 *
 * Lifted from a chat agent's per-question budget (a turn cap, a wall-clock
 * cap, an input-plus-output token cap), where exhaustion ends the loop with
 * "the findings so far" instead of an error. This part keeps the accounting
 * and the exhaustion verdict; what to do when exhausted stays with the caller.
 *
 * Departures from the source: every cap is optional (null: no cap); a budget
 * is exhausted when the amount used reaches its cap, for all three resources
 * alike (the source compared turns with >= but time and tokens with >); the
 * ledger holds 64-bit integers and its additions saturate at 2^63 - 1.
 *
 * Caps and amounts are non-negative integers up to 2^63 - 1, as numbers or
 * bigints. Anything else is out of contract, like a string handed to the
 * breaker: these are the caller's own literals, not data a verdict must
 * speak to, so they throw.
 */
import { verdict, type Verdict } from "./verdict.ts";

/** 2^63 - 1: the largest value every port's ledger holds. */
const LEDGER_MAX = 9223372036854775807n;

export interface BudgetCaps {
  /** Turns (calls, iterations) allowed; null or absent for no cap. */
  max_turns?: number | bigint | null;
  /** Milliseconds allowed; null or absent for no cap. */
  time_budget_ms?: number | bigint | null;
  /** Tokens allowed, counted however the caller counts them; null or absent for no cap. */
  token_budget?: number | bigint | null;
}

export interface Charge {
  turns?: number | bigint;
  ms?: number | bigint;
  tokens?: number | bigint;
}

function ledger(value: number | bigint | undefined, what: string): bigint {
  let n: bigint;
  if (typeof value === "bigint") n = value;
  else if (typeof value === "number" && Number.isInteger(value)) n = BigInt(value);
  else if (value === undefined) n = 0n;
  else throw new TypeError(`${what} must be an integer, got ${String(value)}`);
  if (n < 0n || n > LEDGER_MAX) throw new RangeError(`${what} must be within [0, 2^63 - 1], got ${n}`);
  return n;
}

function cap(value: number | bigint | null | undefined, what: string): bigint | null {
  return value === null || value === undefined ? null : ledger(value, what);
}

function saturatingAdd(used: bigint, amount: bigint): bigint {
  const sum = used + amount;
  return sum > LEDGER_MAX ? LEDGER_MAX : sum;
}

export class Budget {
  readonly max_turns: bigint | null;
  readonly time_budget_ms: bigint | null;
  readonly token_budget: bigint | null;
  turns_used = 0n;
  ms_used = 0n;
  tokens_used = 0n;

  constructor(caps: BudgetCaps = {}) {
    this.max_turns = cap(caps.max_turns, "max_turns");
    this.time_budget_ms = cap(caps.time_budget_ms, "time_budget_ms");
    this.token_budget = cap(caps.token_budget, "token_budget");
  }

  /**
   * Add what was used and say where the budget stands. `warning` with the
   * first exhausted resource, in the order turns, time, tokens; `ok` while
   * every capped resource is below its cap. A charge of nothing reports the
   * current state, and an exhausted budget stays exhausted.
   */
  charge(charge: Charge = {}): Verdict {
    // Every amount is read before any is added: a refused charge changes nothing.
    const turns = ledger(charge.turns, "turns");
    const ms = ledger(charge.ms, "ms");
    const tokens = ledger(charge.tokens, "tokens");
    this.turns_used = saturatingAdd(this.turns_used, turns);
    this.ms_used = saturatingAdd(this.ms_used, ms);
    this.tokens_used = saturatingAdd(this.tokens_used, tokens);
    if (this.max_turns !== null && this.turns_used >= this.max_turns) {
      return verdict("warning", "budget_turns", `turns exhausted: ${this.turns_used} of ${this.max_turns} used`);
    }
    if (this.time_budget_ms !== null && this.ms_used >= this.time_budget_ms) {
      return verdict("warning", "budget_time", `time exhausted: ${this.ms_used} of ${this.time_budget_ms} ms used`);
    }
    if (this.token_budget !== null && this.tokens_used >= this.token_budget) {
      return verdict("warning", "budget_tokens", `tokens exhausted: ${this.tokens_used} of ${this.token_budget} used`);
    }
    const show = (used: bigint, limit: bigint | null) => `${used} of ${limit === null ? "no cap" : limit}`;
    return verdict(
      "ok",
      "budget_ok",
      `within budget: turns ${show(this.turns_used, this.max_turns)}, ms ${show(this.ms_used, this.time_budget_ms)}, tokens ${show(this.tokens_used, this.token_budget)}`,
    );
  }
}
