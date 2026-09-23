//! Dispatch-loop circuit breaker - pure policy: no clock and no waiting. It
//! classifies a failure message into a systemic class, computes capped
//! exponential backoff delays, and tracks a batch's systemic-failure streak to
//! decide trip, dead letter and completed. The loop, the retries and what is
//! persisted are the caller's.

use alloc::borrow::{Cow, ToOwned};
use alloc::format;
use alloc::string::String;
use alloc::vec::Vec;

use crate::value::Refused;
use crate::verdict::{verdict, Level, Verdict};

/// What a failure message says about the provider. `None` where one belongs
/// means the failure is the item's own and says nothing.
///
/// It is a string and not a closed set of three: [`DispatchBreakerState`]
/// takes the class the caller hands it and does not judge it, the empty
/// string included - a message's wording is nobody's contract, so a caller
/// with a provider's own error type names it here.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FailureClass(pub Cow<'static, str>);

impl FailureClass {
    /// The three classes [`classify_systemic_dispatch_failure`] answers.
    pub const RATE_LIMIT: FailureClass = FailureClass(Cow::Borrowed("rate_limit"));
    pub const AUTH: FailureClass = FailureClass(Cow::Borrowed("auth"));
    pub const TRANSPORT: FailureClass = FailureClass(Cow::Borrowed("transport"));

    /// A class the caller names, which is any string at all.
    pub fn new(class: impl Into<Cow<'static, str>>) -> Self {
        FailureClass(class.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

const RATE_LIMIT_PATTERNS: [&str; 12] = [
    "429",
    "rate limit",
    "limit reached",
    "rate_limit",
    "too many requests",
    "overloaded",
    "selected model is at capacity",
    "session limit",
    "usage limit",
    "quota",
    "retry-after",
    "retry_after",
];

const AUTH_PATTERNS: [&str; 8] = [
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid x-api-key",
    "authentication",
    "not logged in",
];

/// Its own list because retry decisions outside this policy may want the same
/// substrings without the rest.
pub const TRANSIENT_TRANSPORT_MESSAGE_PATTERNS: [&str; 7] = [
    "stream disconnected before completion",
    "connection reset by peer",
    "error sending request",
    "failed to connect to websocket",
    "transport channel closed",
    "http/request failed",
    "request failed after",
];

/// The transport class is [`TRANSIENT_TRANSPORT_MESSAGE_PATTERNS`] and these.
/// Which of the two holds a pattern cannot be seen from outside: a class is
/// answered when any of its patterns occurs.
const FURTHER_TRANSPORT_PATTERNS: [&str; 7] = [
    "timed out",
    "timeout",
    "econnrefused",
    "econnreset",
    "etimedout",
    "socket hang up",
    "fetch failed",
];

/// Unicode's full default lowercasing, which is what `char::to_lowercase`
/// does and what a per-character simple lowercasing - Go's `unicode.ToLower`,
/// which the reference port has to special-case - does not. The two differ in
/// one way a pattern can see: U+0130 lowercases to "i" followed by U+0307, so
/// the i in it is not the i inside a pattern. Every other difference - the
/// final sigma - makes a non-ASCII letter, and every pattern here is ASCII.
fn lower_for_patterns(message: &str) -> String {
    message.chars().flat_map(char::to_lowercase).collect()
}

fn holds_any(message: &str, patterns: &[&str]) -> bool {
    patterns.iter().any(|pattern| message.contains(*pattern))
}

/// The class a failure message names, or `None` when the failure is the
/// item's own. An empty message is `None`, as an absent one is - which this
/// port's type says by not holding it. The classes are tried in order, and a
/// pattern is a plain substring.
pub fn classify_systemic_dispatch_failure(message: &str) -> Option<FailureClass> {
    if message.is_empty() {
        return None;
    }
    let lowered = lower_for_patterns(message);
    if holds_any(&lowered, &RATE_LIMIT_PATTERNS) {
        return Some(FailureClass::RATE_LIMIT);
    }
    if holds_any(&lowered, &AUTH_PATTERNS) {
        return Some(FailureClass::AUTH);
    }
    if holds_any(&lowered, &TRANSIENT_TRANSPORT_MESSAGE_PATTERNS)
        || holds_any(&lowered, &FURTHER_TRANSPORT_PATTERNS)
    {
        return Some(FailureClass::TRANSPORT);
    }
    None
}

/// The delay before retry `attempt + 1`, with no jitter: `cap_ms` when
/// `initial_ms` is zero or less, otherwise
/// `min(cap_ms, initial_ms * 2^max(0, attempt))`.
///
/// The other ports compute it in float64, where a power of two times an
/// integer within +/-(2^53 - 1) is exact until it is infinite, so a product
/// past the range is the cap and never an overflow. This port has no floats
/// to reach for - they are the standard library's, which `no_std` leaves out
/// - and computes the same answer by doubling, which is what the spec allows:
///   a port stops at the cap, so a product past its integer type is the cap.
///
/// The doubling below stops the moment the delay reaches the cap, so it runs
/// at most as many times as a positive integer can double inside the range -
/// about fifty - however large `attempt` is, and the doubled value is always
/// under twice the cap and so never near an `i64`'s edge.
pub fn dispatch_backoff_delay_ms(
    attempt: i64,
    initial_ms: i64,
    cap_ms: i64,
) -> Result<i64, Refused> {
    whole(attempt, Refused("attempt must be within +/-(2^53 - 1)"))?;
    whole(
        initial_ms,
        Refused("initial_ms must be within +/-(2^53 - 1)"),
    )?;
    whole(cap_ms, Refused("cap_ms must be within +/-(2^53 - 1)"))?;
    // An initial of zero or less is no delay to double, and a cap the first
    // delay has already reached is the answer whatever the attempt is. Both
    // are where the float form's product is not the smaller of the two.
    if initial_ms <= 0 || cap_ms <= initial_ms {
        return Ok(cap_ms);
    }
    let mut delay = initial_ms;
    // Fifty-three doublings take any delay of 1 or more past 2^53 - 1, which
    // is the largest cap there can be, so the answer is the cap from there on
    // and the count need go no higher. Without the bound an attempt in the
    // millions would be counted down one at a time to reach the same answer.
    let mut doublings = attempt.min(53);
    while doublings > 0 {
        delay *= 2;
        if delay >= cap_ms {
            return Ok(cap_ms);
        }
        doublings -= 1;
    }
    Ok(delay)
}

/// 2^53 - 1: the largest integer every language holds, and so the breaker's.
/// A Rust `i64` holds more, which is why the bound is written here.
const WHOLE_MAX: i64 = (1 << 53) - 1;

/// Refuses an argument outside the range every port shares. The refusal names
/// the argument and not the value it was handed: the text is for a person and
/// is not part of conformance.
fn whole(value: i64, refusal: Refused) -> Result<(), Refused> {
    if !(-WHOLE_MAX..=WHOLE_MAX).contains(&value) {
        return Err(refusal);
    }
    Ok(())
}

/// How one batch is judged. `per_call_max_attempts`, `backoff_initial_ms` and
/// `backoff_cap_ms` are carried for the caller's loop and read by nothing
/// here.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DispatchBreakerPolicy {
    pub enabled: bool,
    pub systemic_threshold: i64,
    /// When on, keeps a pre-trip success from flushing the pending systemic
    /// failures, so which items end where does not depend on the order they
    /// finished in. Off by default, which is what a sequential caller wants.
    pub concurrent: bool,
    pub per_call_max_attempts: i64,
    pub backoff_initial_ms: i64,
    pub backoff_cap_ms: i64,
}

/// One item's final failure, as the caller reports it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DispatchDeadLetterEntry {
    pub item_id: String,
    pub failure_class: Option<FailureClass>,
    pub failure_message: String,
    pub attempt_count: i64,
}

/// The batch's trip, once it has one: a warning verdict, and the three facts its
/// reason names. Its `resume` is `None`, because what a next run picks up is
/// the pending entries and not a place.
#[derive(Clone, Debug, PartialEq)]
pub struct DispatchBreakerTripState {
    pub verdict: Verdict,
    pub failure_class: FailureClass,
    pub consecutive_item_count: i64,
    pub threshold: i64,
}

/// The breaker over one batch. The caller reports each item's final outcome,
/// after its own retries.
///
/// A systemic failure is held pending until the batch proves the provider is
/// alive, which a later success does: only then was it the item's own, and it
/// is dead-lettered. If the streak instead reaches the threshold the batch
/// trips, and the pending entries stay out of the dead letter: they are the
/// outage's victims, to be dispatched again. A failure whose class is `None`
/// is the item's own and is dead-lettered at once.
#[derive(Clone, Debug)]
pub struct DispatchBreakerState {
    policy: DispatchBreakerPolicy,
    pending_systemic: Vec<DispatchDeadLetterEntry>,
    trip: Option<DispatchBreakerTripState>,
    completed: Vec<String>,
    dead_letter: Vec<DispatchDeadLetterEntry>,
}

impl DispatchBreakerState {
    /// Starts a batch, or refuses a policy outside the contract - the call
    /// fails and there is no batch.
    pub fn new(policy: DispatchBreakerPolicy) -> Result<Self, Refused> {
        whole(
            policy.systemic_threshold,
            Refused("systemic_threshold must be within +/-(2^53 - 1)"),
        )?;
        // Carried for the caller's loop and read by nothing here - and in the
        // contract all the same.
        whole(
            policy.per_call_max_attempts,
            Refused("per_call_max_attempts must be within +/-(2^53 - 1)"),
        )?;
        whole(
            policy.backoff_initial_ms,
            Refused("backoff_initial_ms must be within +/-(2^53 - 1)"),
        )?;
        whole(
            policy.backoff_cap_ms,
            Refused("backoff_cap_ms must be within +/-(2^53 - 1)"),
        )?;
        if policy.systemic_threshold < 1 {
            return Err(Refused("systemic_threshold must be at least 1"));
        }
        Ok(DispatchBreakerState {
            policy,
            pending_systemic: Vec::new(),
            trip: None,
            completed: Vec::new(),
            dead_letter: Vec::new(),
        })
    }

    /// Reports a real dispatch success, the only event that proves the
    /// provider is alive. An item that made no call uses
    /// [`Self::record_item_skipped`].
    pub fn record_item_success(&mut self, item_id: &str) {
        self.completed.push(item_id.to_owned());
        // Attribution freezes at the trip: a late success must not write the
        // outage's victims off as the items' own failures.
        if self.trip.is_some() {
            return;
        }
        if self.policy.concurrent {
            return;
        }
        self.dead_letter.append(&mut self.pending_systemic);
    }

    /// Reports an item that owed no dispatch. It is completed, and it proves
    /// nothing about the provider.
    pub fn record_item_skipped(&mut self, item_id: &str) {
        self.completed.push(item_id.to_owned());
    }

    /// Reports an item's final failure. It answers the trip when this failure
    /// is the one that crosses the threshold, and `None` otherwise.
    pub fn record_item_failure(
        &mut self,
        entry: DispatchDeadLetterEntry,
    ) -> Result<Option<DispatchBreakerTripState>, Refused> {
        whole(
            entry.attempt_count,
            Refused("attempt_count must be within +/-(2^53 - 1)"),
        )?;
        let reported_class = entry.failure_class.clone();
        let Some(class) = reported_class else {
            self.dead_letter.push(entry);
            return Ok(None);
        };
        let pending = self
            .pending_systemic
            .iter()
            .any(|held| held.item_id == entry.item_id);
        if !pending {
            // A post-trip systemic failure still joins the pending entries: it
            // is an outage victim and belongs to the incomplete set.
            self.pending_systemic.push(entry);
        }
        if self.policy.enabled
            && self.trip.is_none()
            && self.pending_systemic.len() as i64 >= self.policy.systemic_threshold
        {
            // The first crossing is the trip, and later reports do not
            // rewrite it. The class is the reported entry's, which is the one
            // that crossed even where an entry with its id was already
            // pending and it is the first that is kept.
            let trip = DispatchBreakerTripState {
                verdict: verdict(
                    Level::Warning,
                    "breaker_tripped",
                    format!(
                        // One line: a `\` at the end of a string literal eats the newline
                        // AND the indentation after it, so the space this sentence needs
                        // in the middle was never there. No gate can see that - a
                        // message's wording is nobody's contract - and it took reading
                        // the four ports' output side by side to notice.
                        "{} items in a row failed with {:?}, which is the threshold: the provider, and not the items, is the likely cause",
                        self.pending_systemic.len(),
                        class.as_str()
                    ),
                ),
                failure_class: class,
                consecutive_item_count: self.pending_systemic.len() as i64,
                threshold: self.policy.systemic_threshold,
            };
            self.trip = Some(trip.clone());
            return Ok(Some(trip));
        }
        Ok(None)
    }

    /// The batch's trip, or `None` while it has none.
    pub fn tripped(&self) -> Option<&DispatchBreakerTripState> {
        self.trip.as_ref()
    }

    /// The ids reported complete, in the order they were reported, success
    /// and skipped alike.
    pub fn completed_item_ids(&self) -> &[String] {
        &self.completed
    }

    /// The entries written off, in the order they arrived.
    pub fn dead_letter_entries(&self) -> &[DispatchDeadLetterEntry] {
        &self.dead_letter
    }
}

// ------------------------------------------------------------------ the loop

/// What one call answers the loop. A failure's class is the provider's when
/// it answers with fields, [`classify_systemic_dispatch_failure`]'s when a
/// string is all there is, and `None` for the item's own failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DispatchOutcome {
    /// A real dispatch success: the provider answered.
    Success,
    /// An item that owed no dispatch: completed, and proof of nothing.
    Skipped,
    /// A failed call, with its message and its class.
    Failure {
        failure_message: String,
        failure_class: Option<FailureClass>,
    },
}

/// What [`run_batch`] leaves: the batch's lists and its trip, and the ids
/// that are neither completed nor dead-lettered - the trip's victims and what
/// was never dispatched - to be dispatched again.
#[derive(Clone, Debug, PartialEq)]
pub struct DispatchRunResult {
    pub completed: Vec<String>,
    pub dead_letter: Vec<DispatchDeadLetterEntry>,
    pub tripped: Option<DispatchBreakerTripState>,
    pub incomplete: Vec<String>,
}

/// The loop around one batch, sequential: each item id in order through
/// `call`, which answers an outcome. A systemic failure is tried again after
/// `sleep(backoff)` while the calls made are fewer than the policy's
/// `per_call_max_attempts`; an item's own failure is final at once; the trip stops the loop. It holds
/// no clock: `sleep` is the caller's, and a test hands in one that records. A
/// policy outside the contract is refused before any call is made.
pub fn run_batch(
    policy: DispatchBreakerPolicy,
    items: &[String],
    mut call: impl FnMut(&str) -> DispatchOutcome,
    mut sleep: impl FnMut(i64),
) -> Result<DispatchRunResult, Refused> {
    let attempts = policy.per_call_max_attempts;
    let (initial_ms, cap_ms) = (policy.backoff_initial_ms, policy.backoff_cap_ms);
    let mut state = DispatchBreakerState::new(policy)?;
    for item_id in items {
        if state.tripped().is_some() {
            break;
        }
        let mut attempt: i64 = 0;
        loop {
            let (failure_message, failure_class) = match call(item_id) {
                DispatchOutcome::Success => {
                    state.record_item_success(item_id);
                    break;
                }
                DispatchOutcome::Skipped => {
                    state.record_item_skipped(item_id);
                    break;
                }
                DispatchOutcome::Failure {
                    failure_message,
                    failure_class,
                } => (failure_message, failure_class),
            };
            attempt += 1;
            if failure_class.is_some() && attempt < attempts {
                sleep(dispatch_backoff_delay_ms(attempt - 1, initial_ms, cap_ms)?);
                continue;
            }
            state.record_item_failure(DispatchDeadLetterEntry {
                item_id: item_id.clone(),
                failure_class,
                failure_message,
                attempt_count: attempt,
            })?;
            break;
        }
    }
    let incomplete = items
        .iter()
        .filter(|item_id| !settled(&state, item_id))
        .cloned()
        .collect();
    Ok(DispatchRunResult {
        completed: state.completed_item_ids().to_vec(),
        dead_letter: state.dead_letter_entries().to_vec(),
        tripped: state.tripped().cloned(),
        incomplete,
    })
}

/// Whether an id is completed or dead-lettered: everything else is incomplete.
fn settled(state: &DispatchBreakerState, item_id: &str) -> bool {
    state
        .completed_item_ids()
        .iter()
        .any(|held| held == item_id)
        || state
            .dead_letter_entries()
            .iter()
            .any(|entry| entry.item_id == item_id)
}
