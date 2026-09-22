# Ported from ts/breaker.ts. Pure policy: no I/O, no timers, no clock.
# fixtures/breaker/v0.json is the contract both language ports must satisfy —
# do not change behavior here without checking it still passes that fixture.
# Behavior (not names) must match the TypeScript original; Python identifiers
# follow snake_case instead of the source's camelCase.

from __future__ import annotations

import math
from dataclasses import dataclass

from haltrule.contract import flag as _flag
from haltrule.contract import integer
from haltrule.contract import text as _text
from haltrule.verdict import VerdictLevel, verdict
from typing import Any, Literal, Optional

SystemicDispatchFailureClass = Literal["rate_limit", "auth", "transport"]

# 2^53 - 1: the largest integer every language holds, and so the breaker's.
_WHOLE_MAX = 2**53 - 1


def _whole(value: Any, what: str) -> int:
    """An argument that must be an integer within +/-(2^53 - 1), however the
    caller holds it. Outside the contract it raises."""
    return integer(value, what, -_WHOLE_MAX, _WHOLE_MAX)


def _optional_flag(value: Any, what: str) -> bool:
    """A flag that may be absent - null or not given - and is then off."""
    return False if value is None else _flag(value, what)


_RATE_LIMIT_PATTERNS = (
    "429",
    "rate limit",
    "limit reached",
    "rate_limit",
    "too many requests",
    "overloaded",
    # Provider capacity refusal - same systemic class as "overloaded": the
    # provider is shedding load, not the item failing. Anchored to the full
    # phrase: a bare "at capacity" can occur in unrelated domain text, and an
    # item-local contract failure must never become breaker fuel.
    "selected model is at capacity",
    "session limit",
    "usage limit",
    "quota",
    "retry-after",
    "retry_after",
)

_AUTH_PATTERNS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid x-api-key",
    "authentication",
    "not logged in",
)

# Single source for the transient-transport message patterns, kept as its own
# tuple because other per-unit retry decisions outside this policy may want
# the same substrings without the rest of _TRANSPORT_PATTERNS.
TRANSIENT_TRANSPORT_MESSAGE_PATTERNS = (
    "stream disconnected before completion",
    "connection reset by peer",
    "error sending request",
    "failed to connect to websocket",
    "transport channel closed",
    "http/request failed",
    "request failed after",
)

_TRANSPORT_PATTERNS = TRANSIENT_TRANSPORT_MESSAGE_PATTERNS + (
    "timed out",
    "timeout",
    "econnrefused",
    "econnreset",
    "etimedout",
    "socket hang up",
    "fetch failed",
)


def classify_systemic_dispatch_failure(
    message: Optional[str] = None,
) -> Optional[SystemicDispatchFailureClass]:
    """Classify a failure message into a systemic dispatch class, or None for
    item-local failures (malformed output, validation rejection, ...) that
    must never trip the batch breaker. Message-based by necessity: providers
    and adapters commonly flatten their status into plain strings.

    Absent - null or not given - is no failure text to read; anything else
    that is not a string is outside the contract and raises.
    """
    if message is None:
        return None
    # An empty message needs no test of its own: it holds no pattern.
    normalized = _text(message, "a failure message").lower()
    if any(pattern in normalized for pattern in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(pattern in normalized for pattern in _AUTH_PATTERNS):
        return "auth"
    if any(pattern in normalized for pattern in _TRANSPORT_PATTERNS):
        return "transport"
    return None


def dispatch_backoff_delay_ms(*, attempt: int, initial_ms: int, cap_ms: int) -> int:
    """Capped exponential backoff (no jitter - deterministic for replay/tests).

    `attempt` is 0-based: delay before retry #1 is initial_ms.

    Computed in float64, as TypeScript computes it: the spec's numbers are
    integers within +/-(2^53 - 1), which a float holds exactly, and a power of
    two times one of them is exact until it is infinite. Python's exact
    `2 ** attempt` would never finish for an `attempt` near that ceiling, and
    `2.0 ** attempt` raises where JavaScript answers infinity.
    """
    attempt = _whole(attempt, "attempt")
    initial_ms = _whole(initial_ms, "initial_ms")
    cap_ms = _whole(cap_ms, "cap_ms")
    exponent = max(0.0, float(attempt))
    try:
        power = 2.0**exponent
    except OverflowError:
        power = math.inf
    exponential = float(initial_ms) * power
    bounded = min(float(cap_ms), exponential)
    if math.isfinite(bounded) and bounded > 0:
        return math.floor(bounded)
    return cap_ms


@dataclass
class DispatchBreakerPolicy:
    enabled: bool
    # N: consecutive distinct-item systemic FINAL failures that trip the breaker.
    systemic_threshold: int
    # Per-CALL total attempt cap (1 original + backoff retries) for
    # systemic-class failures. Breaker counting is per ITEM (observation);
    # backoff is per call.
    per_call_max_attempts: int
    backoff_initial_ms: int
    backoff_cap_ms: int
    # Concurrent-pool mode (opt-in; default off/None preserves sequential
    # behavior byte-for-byte). When on, a pre-trip success does NOT flush the
    # pending systemic failures to poison, so the trip and the
    # completed/dead-letter/incomplete classification depend only on WHICH
    # items failed systemically, not on their completion order. Sequential
    # callers omit it and keep the poison-vs-systemic-via-later-success
    # attribution.
    concurrent: Optional[bool] = None


@dataclass
class DispatchDeadLetterEntry:
    item_id: str
    failure_class: Optional[SystemicDispatchFailureClass]
    failure_message: str
    attempt_count: int


@dataclass
class DispatchBreakerTripState:
    """The batch's trip, once it has one: a warning verdict, and the three facts
    its reason names. `resume` is None, because what a next run picks up is
    the pending entries and not a place."""

    spec: str
    verdict: VerdictLevel
    reason: str
    message: str
    resume: Optional[str]
    failure_class: SystemicDispatchFailureClass
    consecutive_item_count: int
    threshold: int


class DispatchBreakerState:
    """Breaker state machine over one batch. The loop reports each item's
    FINAL outcome (after its bounded retries); the machine answers "has this
    become systemic?" and keeps the dead-letter/completion bookkeeping the
    caller persists.

    Poison-vs-systemic attribution rule: a systemic-class failure is held
    PENDING until the batch proves the provider lane is alive (a later item
    succeeds) - only then is it a poison item (reproduced on that item alone)
    and dead-lettered. If the streak instead reaches the threshold, the batch
    trips and the pending items stay out of the dead-letter set: they were
    victims of the outage and must be re-dispatched on recovery, not
    complete-with-failure. Item-local failures (None class) dead-letter
    immediately and say nothing about the provider, so they neither extend
    nor reset the systemic streak.
    """

    def __init__(self, policy: DispatchBreakerPolicy) -> None:
        # The policy as read: what the batch decides by, once and not again.
        self._enabled = _flag(policy.enabled, "enabled")
        self._concurrent = _optional_flag(policy.concurrent, "concurrent")
        self._threshold = _whole(policy.systemic_threshold, "systemic_threshold")
        if self._threshold < 1:
            raise ValueError(
                f"systemic_threshold must be at least 1, got {self._threshold}"
            )
        # Carried for the caller's loop and read by nothing here - and in the
        # contract all the same, so that a typo cannot pass as a policy.
        _whole(policy.per_call_max_attempts, "per_call_max_attempts")
        _whole(policy.backoff_initial_ms, "backoff_initial_ms")
        _whole(policy.backoff_cap_ms, "backoff_cap_ms")
        self.policy = policy
        self._pending_systemic: list[DispatchDeadLetterEntry] = []
        self._trip: Optional[DispatchBreakerTripState] = None
        self._completed: list[str] = []
        self._dead_letter: list[DispatchDeadLetterEntry] = []

    def record_item_success(self, item_id: str) -> None:
        """Report a REAL dispatch success - the only event that proves the
        provider lane is alive and may reclassify pending systemic failures
        as poison. Items that made no successful provider call must use
        record_item_skipped instead.
        """
        self._completed.append(_text(item_id, "item_id"))
        # Attribution freezes at trip: a CONCURRENT pool can deliver an
        # in-flight success after the trip decision, and letting it
        # reclassify the pending outage victims as poison would dead-letter
        # them out of the incomplete recovery set. The late unit itself still
        # counts as completed.
        if self._trip is not None:
            return
        # Concurrent pool (opt-in): during a concurrent burst a success does
        # NOT prove the whole lane is alive (a partial rate-limit yields some
        # successes and some failures), and letting completion order decide
        # which pending victims become poison makes the trip/classification
        # non-deterministic. In this mode systemic victims stay pending - the
        # trip is count-based and order-independent; un-tripped victims end
        # as incomplete. Sequential callers omit the flag and keep the
        # poison-via-later-success attribution.
        if self._concurrent:
            return
        # The provider lane is alive: pending systemic failures were
        # item-scoped after all - poison, dead-lettered.
        for entry in self._pending_systemic:
            self._dead_letter.append(entry)
        self._pending_systemic = []

    def record_item_skipped(self, item_id: str) -> None:
        """Report an item that owes no dispatch (structural skip, budget cap,
        all subsumed): completed for recovery-set purposes, but it proves
        NOTHING about the provider lane - the systemic streak and pending
        attribution are untouched. Conflating this with success would let one
        interleaved skip reset an outage streak and write its victims off as
        poison.
        """
        self._completed.append(_text(item_id, "item_id"))

    def record_item_failure(
        self, entry: DispatchDeadLetterEntry
    ) -> Optional[DispatchBreakerTripState]:
        """Report an item's FINAL failure (per-item budget exhausted).
        Returns the trip state when this failure crosses the systemic
        threshold.
        """
        entry = DispatchDeadLetterEntry(
            item_id=_text(entry.item_id, "item_id"),
            failure_class=(
                None
                if entry.failure_class is None
                else _text(entry.failure_class, "failure_class")
            ),
            failure_message=_text(entry.failure_message, "failure_message"),
            attempt_count=_whole(entry.attempt_count, "attempt_count"),
        )
        if entry.failure_class is None:
            # Item-local failure class: dead-letter, never breaker fuel.
            self._dead_letter.append(entry)
            return None
        if not any(
            pending.item_id == entry.item_id for pending in self._pending_systemic
        ):
            # Post-trip in-flight systemic failures still join the pending
            # set - they are outage victims and belong to the incomplete
            # recovery set.
            self._pending_systemic.append(entry)
        if (
            self._enabled
            and self._trip is None
            and len(self._pending_systemic) >= self._threshold
        ):
            # The FIRST crossing is the trip authority; later records must
            # not rewrite its count. Concurrent-mode guarantee: the trip
            # DECISION (bool), consecutive_item_count, and the
            # completed/dead-letter/incomplete SETS are order-independent
            # (pending_systemic is never flushed). failure_class is NOT: the
            # trip fires early on the first-N-to-complete prefix, so a mixed-
            # class burst labels the trip by whichever systemic class
            # happened to cross. It is a best-effort diagnostic label, never
            # recovery-relevant, and is left as the crossing item's class.
            self._trip = DispatchBreakerTripState(
                **verdict(
                    "warning",
                    "breaker_tripped",
                    f"{len(self._pending_systemic)} items in a row failed with"
                    f" {entry.failure_class!r}, which is the threshold: the provider,"
                    " and not the items, is the likely cause",
                ),
                failure_class=entry.failure_class,
                consecutive_item_count=len(self._pending_systemic),
                threshold=self._threshold,
            )
            return self._trip
        return None

    def tripped(self) -> Optional[DispatchBreakerTripState]:
        return self._trip

    def completed_item_ids(self) -> list[str]:
        return self._completed

    def dead_letter_entries(self) -> list[DispatchDeadLetterEntry]:
        return self._dead_letter
