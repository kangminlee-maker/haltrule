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
 * bigints, in maps holding no other field. Anything else is out of contract,
 * like a string handed to the breaker: these are the caller's own literals,
 * not data a verdict must speak to, so they throw.
 */
import { checkFields, integer } from "./contract.ts";
import { show } from "./messages.ts";
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

/** What was used; an amount that is null or not given is nothing used. */
export interface Charge {
  turns?: number | bigint | null;
  ms?: number | bigint | null;
  tokens?: number | bigint | null;
}

const CAP_FIELDS = ["max_turns", "time_budget_ms", "token_budget"];
const CHARGE_FIELDS = ["turns", "ms", "tokens"];

function ledger(value: number | bigint | null | undefined, what: string): bigint {
  if (value == null) return 0n; // absent, null or not given: nothing used
  return integer(value, what, 0n, LEDGER_MAX);
}

function cap(value: number | bigint | null | undefined, what: string): bigint | null {
  return value == null ? null : ledger(value, what);
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
    checkFields(caps, CAP_FIELDS, "budget caps");
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
    checkFields(charge, CHARGE_FIELDS, "charge");
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
    return verdict(
      "ok",
      "budget_ok",
      `within budget: turns ${show(this.turns_used, this.max_turns)}, ms ${show(this.ms_used, this.time_budget_ms)}, tokens ${show(this.tokens_used, this.token_budget)}`,
    );
  }
}
