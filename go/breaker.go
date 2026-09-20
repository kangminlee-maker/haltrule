package haltrule

// Dispatch-loop circuit breaker - pure policy: no clock and no waiting. It
// classifies a failure message into a systemic class, computes capped
// exponential backoff delays, and tracks a batch's systemic-failure streak to
// decide trip, dead letter and completed. The loop, the retries and what is
// persisted are the caller's.

import (
	"errors"
	"fmt"
	"math"
	"strings"
	"unicode"
)

// FailureClass is what a failure message says about the provider. A nil
// *FailureClass means the failure is the item's own and says nothing.
type FailureClass string

const (
	RateLimit FailureClass = "rate_limit"
	Auth      FailureClass = "auth"
	Transport FailureClass = "transport"
)

var rateLimitPatterns = []string{
	"429", "rate limit", "limit reached", "rate_limit", "too many requests",
	"overloaded", "selected model is at capacity", "session limit",
	"usage limit", "quota", "retry-after", "retry_after",
}

var authPatterns = []string{
	"401", "403", "unauthorized", "forbidden", "invalid api key",
	"invalid x-api-key", "authentication", "not logged in",
}

// TransientTransportMessagePatterns is its own list because retry decisions
// outside this policy may want the same substrings without the rest.
var TransientTransportMessagePatterns = []string{
	"stream disconnected before completion", "connection reset by peer",
	"error sending request", "failed to connect to websocket",
	"transport channel closed", "http/request failed", "request failed after",
}

var transportPatterns = append(append([]string{}, TransientTransportMessagePatterns...),
	"timed out", "timeout", "econnrefused", "econnreset", "etimedout",
	"socket hang up", "fetch failed")

// lowerForPatterns is Unicode's full default lowercasing, which Go's per-rune
// strings.ToLower is not. The two differ in one way a pattern can see: U+0130
// lowercases to "i" followed by U+0307, so the i in it is not the i inside a
// pattern. Every other difference - the final sigma - makes a non-ASCII
// letter, and every pattern here is ASCII.
func lowerForPatterns(message string) string {
	if !strings.ContainsRune(message, 'İ') {
		return strings.ToLower(message)
	}
	var lowered strings.Builder
	for _, character := range message {
		if character == 'İ' {
			lowered.WriteString("i̇")
			continue
		}
		lowered.WriteRune(unicode.ToLower(character))
	}
	return lowered.String()
}

func holdsAny(message string, patterns []string) bool {
	for _, pattern := range patterns {
		if strings.Contains(message, pattern) {
			return true
		}
	}
	return false
}

// ClassifySystemicDispatchFailure answers the class a failure message names,
// or nil when the failure is the item's own. An absent or empty message is
// nil. The classes are tried in order, and a pattern is a plain substring.
func ClassifySystemicDispatchFailure(message string) *FailureClass {
	if message == "" {
		return nil
	}
	lowered := lowerForPatterns(message)
	for _, tried := range []struct {
		class    FailureClass
		patterns []string
	}{
		{RateLimit, rateLimitPatterns},
		{Auth, authPatterns},
		{Transport, transportPatterns},
	} {
		if holdsAny(lowered, tried.patterns) {
			class := tried.class
			return &class
		}
	}
	return nil
}

// DispatchBackoffDelayMs is the delay before retry attempt+1, with no jitter:
// capMs when initialMs is zero or less, otherwise min(capMs, initialMs * 2^max(0, attempt)).
//
// Computed in float64, as the other ports compute it: the spec's numbers are
// integers within +/-(2^53 - 1), which a float64 holds exactly, and a power of
// two times one of them is exact until it is infinite. A product past the
// range is therefore the cap and never an overflow.
func DispatchBackoffDelayMs(attempt, initialMs, capMs int64) (int64, error) {
	if err := errors.Join(
		whole(attempt, "attempt"),
		whole(initialMs, "initial_ms"),
		whole(capMs, "cap_ms"),
	); err != nil {
		return 0, err
	}
	power := math.Pow(2, math.Max(0, float64(attempt)))
	bounded := math.Min(float64(capMs), float64(initialMs)*power)
	if !math.IsInf(bounded, 0) && !math.IsNaN(bounded) && bounded > 0 {
		return int64(math.Floor(bounded)), nil
	}
	return capMs, nil
}

// wholeMax is 2^53 - 1: the largest integer every language holds, and so the
// breaker's. A Go int64 holds more, which is why the bound is written here.
const wholeMax = 1<<53 - 1

// whole refuses an argument outside the range every port shares.
func whole(value int64, what string) error {
	if value < -wholeMax || value > wholeMax {
		return fmt.Errorf("%s must be within +/-(2^53 - 1), got %d", what, value)
	}
	return nil
}

// DispatchBreakerPolicy is how one batch is judged. PerCallMaxAttempts,
// BackoffInitialMs and BackoffCapMs are carried for the caller's loop and read
// by nothing here.
type DispatchBreakerPolicy struct {
	Enabled           bool
	SystemicThreshold int64
	// Concurrent, when on, keeps a pre-trip success from flushing the pending
	// systemic failures, so which items end where does not depend on the order
	// they finished in. Off by default, which is what a sequential caller wants.
	Concurrent         bool
	PerCallMaxAttempts int64
	BackoffInitialMs   int64
	BackoffCapMs       int64
}

// DispatchDeadLetterEntry is one item's final failure, as the caller reports it.
type DispatchDeadLetterEntry struct {
	ItemID         string
	FailureClass   *FailureClass
	FailureMessage string
	AttemptCount   int64
}

// DispatchBreakerTripState is the batch's trip, once it has one.
type DispatchBreakerTripState struct {
	FailureClass         FailureClass
	ConsecutiveItemCount int64
	Threshold            int64
}

// DispatchBreakerState is the breaker over one batch. The caller reports each
// item's final outcome, after its own retries.
//
// A systemic failure is held pending until the batch proves the provider is
// alive, which a later success does: only then was it the item's own, and it
// is dead-lettered. If the streak instead reaches the threshold the batch
// trips, and the pending entries stay out of the dead letter: they are the
// outage's victims, to be dispatched again. A failure whose class is nil is
// the item's own and is dead-lettered at once.
type DispatchBreakerState struct {
	policy          DispatchBreakerPolicy
	pendingSystemic []DispatchDeadLetterEntry
	trip            *DispatchBreakerTripState
	completed       []string
	deadLetter      []DispatchDeadLetterEntry
}

// NewDispatchBreakerState starts a batch, or refuses a policy outside the
// contract - the call fails and there is no batch.
func NewDispatchBreakerState(policy DispatchBreakerPolicy) (*DispatchBreakerState, error) {
	if err := errors.Join(
		whole(policy.SystemicThreshold, "systemic_threshold"),
		// Carried for the caller's loop and read by nothing here - and in the
		// contract all the same.
		whole(policy.PerCallMaxAttempts, "per_call_max_attempts"),
		whole(policy.BackoffInitialMs, "backoff_initial_ms"),
		whole(policy.BackoffCapMs, "backoff_cap_ms"),
	); err != nil {
		return nil, err
	}
	if policy.SystemicThreshold < 1 {
		return nil, fmt.Errorf("systemic_threshold must be at least 1, got %d", policy.SystemicThreshold)
	}
	return &DispatchBreakerState{policy: policy}, nil
}

// RecordItemSuccess reports a real dispatch success, the only event that
// proves the provider is alive. An item that made no call uses RecordItemSkipped.
func (state *DispatchBreakerState) RecordItemSuccess(itemID string) {
	state.completed = append(state.completed, itemID)
	// Attribution freezes at the trip: a late success must not write the
	// outage's victims off as the items' own failures.
	if state.trip != nil {
		return
	}
	if state.policy.Concurrent {
		return
	}
	state.deadLetter = append(state.deadLetter, state.pendingSystemic...)
	state.pendingSystemic = nil
}

// RecordItemSkipped reports an item that owed no dispatch. It is completed,
// and it proves nothing about the provider.
func (state *DispatchBreakerState) RecordItemSkipped(itemID string) {
	state.completed = append(state.completed, itemID)
}

// RecordItemFailure reports an item's final failure. It answers the trip when
// this failure is the one that crosses the threshold, and nil otherwise.
func (state *DispatchBreakerState) RecordItemFailure(entry DispatchDeadLetterEntry) (*DispatchBreakerTripState, error) {
	if err := whole(entry.AttemptCount, "attempt_count"); err != nil {
		return nil, err
	}
	if entry.FailureClass == nil {
		state.deadLetter = append(state.deadLetter, entry)
		return nil, nil
	}
	pending := false
	for _, held := range state.pendingSystemic {
		if held.ItemID == entry.ItemID {
			pending = true
			break
		}
	}
	if !pending {
		// A post-trip systemic failure still joins the pending entries: it is
		// an outage victim and belongs to the incomplete set.
		state.pendingSystemic = append(state.pendingSystemic, entry)
	}
	if state.policy.Enabled && state.trip == nil &&
		int64(len(state.pendingSystemic)) >= state.policy.SystemicThreshold {
		// The first crossing is the trip, and later reports do not rewrite it.
		state.trip = &DispatchBreakerTripState{
			FailureClass:         *entry.FailureClass,
			ConsecutiveItemCount: int64(len(state.pendingSystemic)),
			Threshold:            state.policy.SystemicThreshold,
		}
		return state.trip, nil
	}
	return nil, nil
}

// Tripped is the batch's trip, or nil while it has none.
func (state *DispatchBreakerState) Tripped() *DispatchBreakerTripState {
	return state.trip
}

// CompletedItemIDs are the ids reported complete, in the order they were
// reported, success and skipped alike.
func (state *DispatchBreakerState) CompletedItemIDs() []string {
	return state.completed
}

// DeadLetterEntries are the entries written off, in the order they arrived.
func (state *DispatchBreakerState) DeadLetterEntries() []DispatchDeadLetterEntry {
	return state.deadLetter
}
