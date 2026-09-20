/**
 * Dispatch-loop circuit breaker — pure policy: no I/O, no timers, no clock.
 *
 * Extracted from a production LLM dispatch loop's breaker module (names and
 * signatures unchanged; only the doc comments were generalized away from the
 * source repo's internal rule numbers and cross-file references). Classifies
 * a failure message into a systemic class, computes capped-exponential
 * backoff delays, and tracks a batch's consecutive-systemic-failure streak
 * to decide trip / dead-letter / completed.
 */

/**
 * Systemic failure class a dispatch failure message can be classified into.
 * `null` (returned by the classifier, not a member of this type) means the
 * failure is item-local and must never count toward the breaker.
 */
export type SystemicDispatchFailureClass = "rate_limit" | "auth" | "transport";

const RATE_LIMIT_PATTERNS = [
  "429",
  "rate limit",
  "limit reached",
  "rate_limit",
  "too many requests",
  "overloaded",
  // Provider capacity refusal — same systemic class as "overloaded": the
  // provider is shedding load, not the item failing. Anchored to the full
  // phrase: a bare "at capacity" can occur in unrelated domain text, and an
  // item-local contract failure must never become breaker fuel.
  "selected model is at capacity",
  "session limit",
  "usage limit",
  "quota",
  "retry-after",
  "retry_after",
];

const AUTH_PATTERNS = [
  "401",
  "403",
  "unauthorized",
  "forbidden",
  "invalid api key",
  "invalid x-api-key",
  "authentication",
  "not logged in",
];

/** Single source for the transient-transport message patterns, kept as its
 * own list because other per-unit retry decisions outside this policy may
 * want the same substrings without the rest of TRANSPORT_PATTERNS. */
export const TRANSIENT_TRANSPORT_MESSAGE_PATTERNS = [
  "stream disconnected before completion",
  "connection reset by peer",
  "error sending request",
  "failed to connect to websocket",
  "transport channel closed",
  "http/request failed",
  "request failed after",
] as const;

const TRANSPORT_PATTERNS = [
  ...TRANSIENT_TRANSPORT_MESSAGE_PATTERNS,
  "timed out",
  "timeout",
  "econnrefused",
  "econnreset",
  "etimedout",
  "socket hang up",
  "fetch failed",
];

/**
 * Classify a failure message into a systemic dispatch class, or null for
 * item-local failures (malformed output, validation rejection, …) that must
 * never trip the batch breaker. Message-based by necessity: providers and
 * adapters commonly flatten their status into plain strings.
 */
export function classifySystemicDispatchFailure(
  message: string | null | undefined,
): SystemicDispatchFailureClass | null {
  // An empty message needs no test of its own: it holds no pattern.
  if (typeof message !== "string") return null;
  const normalized = message.toLowerCase();
  if (RATE_LIMIT_PATTERNS.some((pattern) => normalized.includes(pattern))) {
    return "rate_limit";
  }
  if (AUTH_PATTERNS.some((pattern) => normalized.includes(pattern))) {
    return "auth";
  }
  if (TRANSPORT_PATTERNS.some((pattern) => normalized.includes(pattern))) {
    return "transport";
  }
  return null;
}

/** Capped exponential backoff (no jitter — deterministic for replay/tests).
 * attempt is 0-based: delay before retry #1 is initialMs. */
export function dispatchBackoffDelayMs(args: {
  attempt: number;
  initialMs: number;
  capMs: number;
}): number {
  const exponential = args.initialMs * 2 ** Math.max(0, args.attempt);
  const bounded = Math.min(args.capMs, exponential);
  return Number.isFinite(bounded) && bounded > 0 ? Math.floor(bounded) : args.capMs;
}

export interface DispatchBreakerPolicy {
  enabled: boolean;
  /** N: consecutive distinct-item systemic FINAL failures that trip the breaker. */
  systemic_threshold: number;
  /** Per-CALL total attempt cap (1 original + backoff retries) for
   * systemic-class failures. Breaker counting is per ITEM (observation);
   * backoff is per call. */
  per_call_max_attempts: number;
  backoff_initial_ms: number;
  backoff_cap_ms: number;
  /** Concurrent-pool mode (opt-in; default off/undefined preserves sequential
   * behavior byte-for-byte). When on, a pre-trip success does NOT flush the
   * pending systemic failures to poison, so the trip and the
   * completed/dead-letter/incomplete classification depend only on WHICH
   * items failed systemically, not on their completion order. Sequential
   * callers omit it and keep the poison-vs-systemic-via-later-success
   * attribution. */
  concurrent?: boolean;
}

export interface DispatchDeadLetterEntry {
  item_id: string;
  failure_class: SystemicDispatchFailureClass | null;
  failure_message: string;
  attempt_count: number;
}

export interface DispatchBreakerTripState {
  failure_class: SystemicDispatchFailureClass;
  consecutive_item_count: number;
  threshold: number;
}

/**
 * Breaker state machine over one batch. The loop reports each item's FINAL
 * outcome (after its bounded retries); the machine answers "has this become
 * systemic?" and keeps the dead-letter/completion bookkeeping the caller
 * persists.
 *
 * Poison-vs-systemic attribution rule: a systemic-class failure is held
 * PENDING until the batch proves the provider lane is alive (a later item
 * succeeds) — only then is it a poison item (reproduced on that item alone)
 * and dead-lettered. If the streak instead reaches the threshold, the batch
 * trips and the pending items stay out of the dead-letter set: they were
 * victims of the outage and must be re-dispatched on recovery, not
 * complete-with-failure. Item-local failures (null class) dead-letter
 * immediately and say nothing about the provider, so they neither extend nor
 * reset the systemic streak.
 */
export class DispatchBreakerState {
  readonly policy: DispatchBreakerPolicy;
  private pendingSystemic: DispatchDeadLetterEntry[] = [];
  private trip: DispatchBreakerTripState | null = null;
  private readonly completed: string[] = [];
  private readonly deadLetter: DispatchDeadLetterEntry[] = [];

  constructor(policy: DispatchBreakerPolicy) {
    this.policy = policy;
  }

  /** Report a REAL dispatch success — the only event that proves the
   * provider lane is alive and may reclassify pending systemic failures as
   * poison. Items that made no successful provider call must use
   * {@link recordItemSkipped} instead. */
  recordItemSuccess(itemId: string): void {
    this.completed.push(itemId);
    // Attribution freezes at trip: a CONCURRENT pool can deliver an in-flight
    // success after the trip decision, and letting it reclassify the pending
    // outage victims as poison would dead-letter them out of the incomplete
    // recovery set. The late unit itself still counts as completed.
    if (this.trip !== null) return;
    // Concurrent pool (opt-in): during a concurrent burst a success does NOT
    // prove the whole lane is alive (a partial rate-limit yields some
    // successes and some failures), and letting completion order decide
    // which pending victims become poison makes the trip/classification
    // non-deterministic. In this mode systemic victims stay pending — the
    // trip is count-based and order-independent; un-tripped victims end as
    // incomplete. Sequential callers omit the flag and keep the
    // poison-via-later-success attribution.
    if (this.policy.concurrent) return;
    // The provider lane is alive: pending systemic failures were item-scoped
    // after all — poison, dead-lettered.
    for (const entry of this.pendingSystemic) this.deadLetter.push(entry);
    this.pendingSystemic = [];
  }

  /** Report an item that owes no dispatch (structural skip, budget cap, all
   * subsumed): completed for recovery-set purposes, but it proves NOTHING
   * about the provider lane — the systemic streak and pending attribution
   * are untouched. Conflating this with success would let one interleaved
   * skip reset an outage streak and write its victims off as poison. */
  recordItemSkipped(itemId: string): void {
    this.completed.push(itemId);
  }

  /** Report an item's FINAL failure (per-item budget exhausted). Returns the
   * trip state when this failure crosses the systemic threshold. */
  recordItemFailure(entry: DispatchDeadLetterEntry): DispatchBreakerTripState | null {
    if (entry.failure_class === null) {
      // Item-local failure class: dead-letter, never breaker fuel.
      this.deadLetter.push(entry);
      return null;
    }
    if (
      !this.pendingSystemic.some((pending) => pending.item_id === entry.item_id)
    ) {
      // Post-trip in-flight systemic failures still join the pending set —
      // they are outage victims and belong to the incomplete recovery set.
      this.pendingSystemic.push(entry);
    }
    if (
      this.policy.enabled &&
      this.trip === null &&
      this.pendingSystemic.length >= this.policy.systemic_threshold
    ) {
      // The FIRST crossing is the trip authority; later records must not
      // rewrite its count. Concurrent-mode guarantee: the trip DECISION
      // (bool), `consecutive_item_count`, and the completed/dead-letter/
      // incomplete SETS are order-independent (pendingSystemic is never
      // flushed). `failure_class` is NOT: the trip fires early on the
      // first-N-to-complete prefix, so a mixed-class burst labels the trip by
      // whichever systemic class happened to cross. It is a best-effort
      // diagnostic label, never recovery-relevant, and is left as the
      // crossing item's class.
      this.trip = {
        failure_class: entry.failure_class,
        consecutive_item_count: this.pendingSystemic.length,
        threshold: this.policy.systemic_threshold,
      };
      return this.trip;
    }
    return null;
  }

  tripped(): DispatchBreakerTripState | null {
    return this.trip;
  }

  completedItemIds(): readonly string[] {
    return this.completed;
  }

  deadLetterEntries(): readonly DispatchDeadLetterEntry[] {
    return this.deadLetter;
  }
}
