//! One function per fixture section: it builds the arguments, calls the part,
//! and answers the value the line carries. It holds no expectation.

use crate::decode::{
    decode_i64, decode_optional_f64, decode_optional_i64, decode_value, optional_text, strictly,
    text, Built, Refusal,
};
use crate::line::refused;
use crate::run::Inputs;
use haltrule::{
    canonicalize, checkpoint_digest, classify_systemic_dispatch_failure, dispatch_backoff_delay_ms,
    evaluate_checkpoint_artifact, validate_slot, ArtifactStatus, Budget, BudgetCaps, Charge,
    CheckpointArgs, DispatchBreakerPolicy, DispatchBreakerState, DispatchBreakerTripState,
    DispatchDeadLetterEntry, FailureClass, Map, SlotKind, SlotSpec, Value, Verdict,
};
use serde_json::Value as Json;
use std::collections::BTreeMap;

/// Why a case has no answer. A value this port cannot hold sits the case out;
/// anything else is this port's own failure, reported under the case's id.
pub enum Trouble {
    Unbuildable,
    Raised(String),
}

pub type Answer = Result<Value, Trouble>;
pub type Section = fn(&Inputs) -> Answer;

pub fn section_for(name: &str) -> Option<Section> {
    Some(match name {
        "classify" => classify,
        "backoff" => backoff,
        "state" => state,
        "canonicalize" => canonicalize_case,
        "checkpoint" => checkpoint,
        "charge" => charge,
        "validate" => validate,
        "result_line" => result_line,
        _ => return None,
    })
}

/// An argument this port cannot hold is outside the part's contract as well,
/// so both kinds of trouble are the one answer every port gives: refused. A
/// case is sat out only where the value itself is the subject - a digest
/// input, an artifact - which [`or_refused`] is for.
fn refusing(built: Built<Value>) -> Answer {
    Ok(built.unwrap_or_else(|_| refused()))
}

/// The part's own refusal is an answer; a value this port cannot hold is not.
fn or_refused(built: Built<Value>) -> Answer {
    match built {
        Ok(value) => Ok(value),
        Err(Refusal::Refused) => Ok(refused()),
        Err(Refusal::Unbuildable) => Err(Trouble::Unbuildable),
    }
}

/// Refuses a case whose fields the contract does not name, where the case's
/// own map is the argument.
fn only(from: &Inputs, names: &[&str]) -> Built<()> {
    for key in from.keys() {
        if !names.contains(&key.as_str()) {
            return Err(Refusal::Refused);
        }
    }
    Ok(())
}

/// A verdict as conformance sees it: without `message`, which is for people.
/// The other ports check here that a verdict has exactly the five fields and
/// that its message is a string; the struct is that check.
fn normative(from: &Verdict) -> Value {
    let mut held = Map::new();
    held.insert("spec".to_string(), Value::Text(from.spec.to_string()));
    held.insert(
        "verdict".to_string(),
        Value::Text(from.verdict.as_str().to_string()),
    );
    held.insert("reason".to_string(), Value::Text(from.reason.to_string()));
    held.insert(
        "resume".to_string(),
        match &from.resume {
            None => Value::Null,
            Some(held) => Value::Text(held.clone()),
        },
    );
    Value::Map(held)
}

/// The same for a verdict carrying the fields its reason names beside the
/// five: those may be there, the five must be, and `message` still goes.
fn normative_open(from: Map) -> Result<Value, Trouble> {
    for field in ["spec", "verdict", "reason", "message", "resume"] {
        if !from.contains_key(field) {
            return Err(Trouble::Raised(format!("a verdict has no {field}")));
        }
    }
    if !matches!(from.get("message"), Some(Value::Text(_))) {
        return Err(Trouble::Raised(
            "a verdict's message is not text".to_string(),
        ));
    }
    let mut held = from;
    held.remove("message");
    Ok(Value::Map(held))
}

fn map_of(pairs: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    let mut held = Map::new();
    for (key, value) in pairs {
        held.insert(key.to_string(), value);
    }
    Value::Map(held)
}

// ------------------------------------------------------------------ breaker

fn classify(from: &Inputs) -> Answer {
    // Absent - not given here, null there - is no failure text to read. A
    // message of any other type is outside the contract, which this port's
    // type says by not holding it.
    let message = match from.get("message") {
        None | Some(Json::Null) => return Ok(Value::Null),
        Some(Json::String(held)) => held.clone(),
        Some(_) => return Ok(refused()),
    };
    Ok(match classify_systemic_dispatch_failure(&message) {
        None => Value::Null,
        Some(class) => Value::Text(class.as_str().to_string()),
    })
}

fn backoff(from: &Inputs) -> Answer {
    refusing((|| {
        // The case's own map is the argument: a field the contract does not
        // name reaches the part, as it would from a caller.
        only(from, &["attempt", "initial_ms", "cap_ms"])?;
        let attempt = decode_i64(from.get("attempt"))?;
        let initial_ms = decode_i64(from.get("initial_ms"))?;
        let cap_ms = decode_i64(from.get("cap_ms"))?;
        dispatch_backoff_delay_ms(attempt, initial_ms, cap_ms)
            .map(Value::Int)
            .map_err(|_| Refusal::Refused)
    })())
}

const POLICY_FIELDS: [&str; 6] = [
    "enabled",
    "systemic_threshold",
    "concurrent",
    "per_call_max_attempts",
    "backoff_initial_ms",
    "backoff_cap_ms",
];
const ENTRY_FIELDS: [&str; 5] = [
    "kind",
    "item_id",
    "failure_class",
    "failure_message",
    "attempt_count",
];

fn flag(raw: Option<&Json>) -> Built<bool> {
    match raw {
        Some(Json::Bool(held)) => Ok(*held),
        _ => Err(Refusal::Refused),
    }
}

fn batch_of(raw: Option<&Json>) -> Built<DispatchBreakerState> {
    let given = strictly(raw, &POLICY_FIELDS)?;
    let policy = DispatchBreakerPolicy {
        enabled: flag(given.get("enabled"))?,
        systemic_threshold: decode_i64(given.get("systemic_threshold"))?,
        // Absent - not given here, null there - is off.
        concurrent: match given.get("concurrent") {
            None | Some(Json::Null) => false,
            held => flag(held)?,
        },
        per_call_max_attempts: decode_i64(given.get("per_call_max_attempts"))?,
        backoff_initial_ms: decode_i64(given.get("backoff_initial_ms"))?,
        backoff_cap_ms: decode_i64(given.get("backoff_cap_ms"))?,
    };
    DispatchBreakerState::new(policy).map_err(|_| Refusal::Refused)
}

fn trip_value(trip: Option<&DispatchBreakerTripState>) -> Value {
    let held = match trip {
        None => return Value::Null,
        Some(held) => held,
    };
    let mut shown = match normative(&held.verdict) {
        Value::Map(fields) => fields,
        other => return other,
    };
    shown.insert(
        "failure_class".to_string(),
        Value::Text(held.failure_class.as_str().to_string()),
    );
    shown.insert(
        "consecutive_item_count".to_string(),
        Value::Int(held.consecutive_item_count),
    );
    shown.insert("threshold".to_string(), Value::Int(held.threshold));
    Value::Map(shown)
}

fn entry_value(entry: &DispatchDeadLetterEntry) -> Value {
    map_of([
        ("item_id", Value::Text(entry.item_id.clone())),
        (
            "failure_class",
            match &entry.failure_class {
                None => Value::Null,
                Some(class) => Value::Text(class.as_str().to_string()),
            },
        ),
        (
            "failure_message",
            Value::Text(entry.failure_message.clone()),
        ),
        ("attempt_count", Value::Int(entry.attempt_count)),
    ])
}

/// What one event asks of the batch.
enum Report {
    Success(String),
    Skipped(String),
    Failure(DispatchDeadLetterEntry),
}

fn read_report(raw: &Json) -> Built<Report> {
    let event = strictly(Some(raw), &ENTRY_FIELDS)?;
    let item_id = text(event.get("item_id"))?;
    match event.get("kind").and_then(Json::as_str) {
        Some("success") => return Ok(Report::Success(item_id)),
        Some("skipped") => return Ok(Report::Skipped(item_id)),
        _ => {}
    }
    // A class that is not given is not a class that is null: one is absent
    // from the contract, the other is the item's own failure.
    let failure_class = match event.get("failure_class") {
        None => return Err(Refusal::Refused),
        Some(Json::Null) => None,
        held => Some(FailureClass::new(text(held)?)),
    };
    Ok(Report::Failure(DispatchDeadLetterEntry {
        item_id,
        failure_class,
        failure_message: text(event.get("failure_message"))?,
        attempt_count: decode_i64(event.get("attempt_count"))?,
    }))
}

/// One item's outcome, or this port's refusal of it.
fn one_report(machine: &mut DispatchBreakerState, raw: &Json) -> Value {
    match read_report(raw) {
        Err(_) => refused(),
        Ok(Report::Success(item_id)) => {
            machine.record_item_success(&item_id);
            Value::Null
        }
        Ok(Report::Skipped(item_id)) => {
            machine.record_item_skipped(&item_id);
            Value::Null
        }
        Ok(Report::Failure(entry)) => match machine.record_item_failure(entry) {
            Err(_) => refused(),
            Ok(trip) => trip_value(trip.as_ref()),
        },
    }
}

fn state(from: &Inputs) -> Answer {
    let mut machine = match batch_of(from.get("policy")) {
        Ok(machine) => machine,
        Err(_) => return Ok(refused()),
    };
    let events = match from.get("events").and_then(Json::as_array) {
        Some(events) => events,
        None => return Err(Trouble::Raised("a state case without events".to_string())),
    };
    let mut returns = Vec::with_capacity(events.len());
    for raw in events {
        // A refused report leaves the batch as it was, as a refused charge
        // leaves the ledger: the answer is the refusal and the next goes on.
        returns.push(one_report(&mut machine, raw));
    }
    let completed = machine
        .completed_item_ids()
        .iter()
        .map(|id| Value::Text(id.clone()))
        .collect();
    let dead_letter = machine
        .dead_letter_entries()
        .iter()
        .map(entry_value)
        .collect();
    Ok(map_of([
        ("returns", Value::List(returns)),
        ("completed", Value::List(completed)),
        ("dead_letter", Value::List(dead_letter)),
        ("tripped", trip_value(machine.tripped())),
    ]))
}

// --------------------------------------------------------------- checkpoint

fn canonicalize_case(from: &Inputs) -> Answer {
    let value = match decode_value(from.get("input")) {
        Ok(value) => value,
        Err(Refusal::Unbuildable) => return Err(Trouble::Unbuildable),
        Err(Refusal::Refused) => {
            return Err(Trouble::Raised(
                "a digest input outside the grammar".to_string(),
            ))
        }
    };
    match (canonicalize(&value), checkpoint_digest(&value)) {
        (Ok(canonical), Ok(digest)) => Ok(map_of([
            ("canonical", Value::Text(canonical)),
            ("digest", Value::Text(digest)),
        ])),
        // They must agree in the whole verdict, not only the reason: only one
        // of the two is printed, so a field this comparison leaves out is a
        // field no case can see.
        (Err(halt), Err(other)) if normative(&halt) == normative(&other) => Ok(normative(&halt)),
        // The two entry points must agree about the same value; if they do
        // not, that is a bug in the port, not a result to compare.
        _ => Err(Trouble::Raised(
            "canonicalize and the digest disagree about halting".to_string(),
        )),
    }
}

const CHECKPOINT_FIELDS: [&str; 9] = [
    "stage_id",
    "subject_ref",
    "artifact",
    "expected_contract_revision",
    "expected_stage_config_digest",
    "expected_dependency_digests",
    "required_resume_from_stage",
    "validation_issues",
    "status_map",
];

fn decode_map(raw: Option<&Json>) -> Built<Option<Map>> {
    match raw {
        None | Some(Json::Null) => Ok(None),
        given => match decode_value(given)? {
            Value::Map(held) => Ok(Some(held)),
            _ => Err(Refusal::Refused),
        },
    }
}

fn checkpoint(from: &Inputs) -> Answer {
    let built = (|| -> Built<Value> {
        let given = strictly(from.get("args"), &CHECKPOINT_FIELDS)?;
        let mut args = CheckpointArgs {
            stage_id: text(given.get("stage_id"))?,
            subject_ref: optional_text(given.get("subject_ref"))?,
            artifact: decode_map(given.get("artifact"))?,
            expected_contract_revision: optional_text(given.get("expected_contract_revision"))?,
            expected_stage_config_digest: optional_text(given.get("expected_stage_config_digest"))?,
            expected_dependency_digests: None,
            required_resume_from_stage: optional_text(given.get("required_resume_from_stage"))?,
            validation_issues: None,
            status_map: None,
        };
        if let Some(raw) = present(given.get("expected_dependency_digests")) {
            let held = raw.as_object().ok_or(Refusal::Refused)?;
            let mut digests = BTreeMap::new();
            for (id, digest) in held {
                digests.insert(id.clone(), text(Some(digest))?);
            }
            args.expected_dependency_digests = Some(digests);
        }
        if let Some(raw) = present(given.get("status_map")) {
            let held = raw.as_object().ok_or(Refusal::Refused)?;
            let mut named = BTreeMap::new();
            for (name, status) in held {
                let spelt = status.as_str().ok_or(Refusal::Refused)?;
                // A status outside the vocabulary has no spelling here, which
                // is the refusal the other ports write out.
                let known = ArtifactStatus::from_name(spelt).ok_or(Refusal::Refused)?;
                named.insert(name.clone(), known);
            }
            args.status_map = Some(named);
        }
        if let Some(raw) = present(given.get("validation_issues")) {
            let held = raw.as_array().ok_or(Refusal::Refused)?;
            let mut issues = Vec::with_capacity(held.len());
            for one in held {
                // null is no issue this port can hold.
                issues.push(decode_map(Some(one))?.ok_or(Refusal::Refused)?);
            }
            args.validation_issues = Some(issues);
        }
        let issues = evaluate_checkpoint_artifact(&args).map_err(|_| Refusal::Refused)?;
        Ok(Value::List(issues.into_iter().map(Value::Map).collect()))
    })();
    // A verdict's shape is the adapter's to check, and a Refusal cannot carry
    // that complaint, so the issues come back as maps and are checked here.
    match or_refused(built)? {
        Value::List(issues) => {
            let mut shown = Vec::with_capacity(issues.len());
            for issue in issues {
                match issue {
                    Value::Map(held) => shown.push(normative_open(held)?),
                    _ => return Err(Trouble::Raised("an issue is not a map".to_string())),
                }
            }
            Ok(Value::List(shown))
        }
        refusal => Ok(refusal),
    }
}

/// A field that is there and is not null.
fn present(raw: Option<&Json>) -> Option<&Json> {
    match raw {
        None | Some(Json::Null) => None,
        given => given,
    }
}

// ------------------------------------------------------------------- budget

const CAP_FIELDS: [&str; 3] = ["max_turns", "time_budget_ms", "token_budget"];
const CHARGE_FIELDS: [&str; 3] = ["turns", "ms", "tokens"];

fn one_charge(budget: &mut Budget, raw: &Json) -> Value {
    let read = || -> Built<Charge> {
        let given = strictly(Some(raw), &CHARGE_FIELDS)?;
        Ok(Charge {
            turns: decode_optional_i64(given.get("turns"))?,
            ms: decode_optional_i64(given.get("ms"))?,
            tokens: decode_optional_i64(given.get("tokens"))?,
        })
    };
    match read().and_then(|charge| budget.charge(charge).map_err(|_| Refusal::Refused)) {
        Err(_) => refused(),
        Ok(answer) => normative(&answer),
    }
}

fn charge(from: &Inputs) -> Answer {
    let caps = (|| -> Built<Budget> {
        let given = strictly(from.get("budget"), &CAP_FIELDS)?;
        Budget::new(BudgetCaps {
            max_turns: decode_optional_i64(given.get("max_turns"))?,
            time_budget_ms: decode_optional_i64(given.get("time_budget_ms"))?,
            token_budget: decode_optional_i64(given.get("token_budget"))?,
        })
        .map_err(|_| Refusal::Refused)
    })();
    let mut budget = match caps {
        Ok(budget) => budget,
        Err(_) => return Ok(refused()),
    };
    let charges = match from.get("charges").and_then(Json::as_array) {
        Some(charges) => charges,
        None => return Err(Trouble::Raised("a charge case without charges".to_string())),
    };
    let verdicts = charges
        .iter()
        .map(|raw| one_charge(&mut budget, raw))
        .collect();
    Ok(map_of([
        ("verdicts", Value::List(verdicts)),
        // The ledger in decimal, so 2^63 - 1 survives JSON in every language.
        (
            "used",
            map_of([
                ("turns", Value::Text(budget.turns_used.to_string())),
                ("ms", Value::Text(budget.ms_used.to_string())),
                ("tokens", Value::Text(budget.tokens_used.to_string())),
            ]),
        ),
    ]))
}

// --------------------------------------------------------------------- slot

const SPEC_FIELDS: [&str; 7] = [
    "name",
    "kind",
    "candidates",
    "min_length",
    "max_length",
    "min",
    "max",
];

fn validate(from: &Inputs) -> Answer {
    let value = match decode_value(from.get("value")) {
        Ok(value) => value,
        Err(Refusal::Unbuildable) => return Err(Trouble::Unbuildable),
        Err(Refusal::Refused) => {
            return Err(Trouble::Raised(
                "a slot value outside the grammar".to_string(),
            ))
        }
    };
    refusing((|| -> Built<Value> {
        let given = strictly(from.get("spec"), &SPEC_FIELDS)?;
        let kind = match given.get("kind").and_then(Json::as_str) {
            Some("choice") => SlotKind::Choice,
            Some("text") => SlotKind::Text,
            Some("score") => SlotKind::Score,
            // A kind outside the vocabulary has no spelling here, which is the
            // refusal the other ports write out.
            _ => return Err(Refusal::Refused),
        };
        let candidates = match present(given.get("candidates")) {
            None => None,
            Some(raw) => {
                let held = raw.as_array().ok_or(Refusal::Refused)?;
                let mut named = Vec::with_capacity(held.len());
                for one in held {
                    named.push(text(Some(one))?);
                }
                Some(named)
            }
        };
        let spec = SlotSpec {
            name: text(given.get("name"))?,
            kind,
            candidates,
            min_length: decode_optional_i64(given.get("min_length"))?,
            max_length: decode_optional_i64(given.get("max_length"))?,
            min: decode_optional_f64(given.get("min"))?,
            max: decode_optional_f64(given.get("max"))?,
        };
        validate_slot(&spec, &value)
            .map(|answer| normative(&answer))
            .map_err(|_| Refusal::Refused)
    })())
}

// ---------------------------------------------------------------- the line

fn result_line(from: &Inputs) -> Answer {
    let value = match decode_value(from.get("value")) {
        Ok(value) => value,
        // The line format's own model has no such value either, which is the
        // refusal the case asks for, not a case to sit out.
        Err(Refusal::Unbuildable) => return Ok(refused()),
        Err(Refusal::Refused) => {
            return Err(Trouble::Raised(
                "a line value outside the grammar".to_string(),
            ))
        }
    };
    Ok(match crate::line::write_value(&value) {
        Err(_) => refused(),
        Ok(line) => map_of([("line", Value::Text(line))]),
    })
}
