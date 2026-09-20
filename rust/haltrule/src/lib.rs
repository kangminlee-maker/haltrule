//! The Rust port of the haltrule spec: the policy of stopping well. Pure
//! policy - no I/O, no timers, no clock, no randomness - and it is the crate
//! itself that says so: `no_std` leaves `std::fs`, `std::time`, `std::env`
//! and `std::process` out of the language this code is written in, and the
//! one dependency is SHA-256. ../../fixtures is the contract every port must
//! satisfy; ../../spec/README.md is the spec in words.
//!
//! What the other ports check at run time, this one mostly cannot be handed
//! at all: [`Value`] is closed, a cap is an `i64`, and an absent argument is
//! `None`. That is the same contract, kept by the type instead of by code.
#![no_std]

extern crate alloc;

mod breaker;
mod budget;
mod checkpoint;
mod messages;
mod slot;
mod value;
mod verdict;

pub use breaker::{
    classify_systemic_dispatch_failure, dispatch_backoff_delay_ms, DispatchBreakerPolicy,
    DispatchBreakerState, DispatchBreakerTripState, DispatchDeadLetterEntry, FailureClass,
    TRANSIENT_TRANSPORT_MESSAGE_PATTERNS,
};
pub use budget::{Budget, BudgetCaps, Charge};
pub use checkpoint::{
    canonicalize, checkpoint_digest, evaluate_checkpoint_artifact, ArtifactStatus, CheckpointArgs,
    Issue,
};
pub use slot::{validate_slot, SlotKind, SlotSpec};
pub use value::{Map, Refused, Value};
pub use verdict::{Level, Verdict, SPEC};
