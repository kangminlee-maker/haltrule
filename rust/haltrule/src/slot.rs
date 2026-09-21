//! Slot - does a value a person or a model filled in satisfy its contract?
//!
//! Three kinds cover it: [`SlotKind::Choice`], a value that must be one of the
//! candidates exactly, [`SlotKind::Text`], a value of the person's own within
//! length bounds, and [`SlotKind::Score`], a number held against a bar. A
//! reference to something that exists is a choice whose candidates are the
//! known identifiers.
//!
//! A missing value is a warning: the judgment has not been made yet. A present
//! value that fails its contract is a halt: it would be written to a ledger as
//! if it were valid. Missing is null, or a string that is empty or holds only
//! ASCII whitespace. Comparison is exact - no trimming, no case folding, no
//! Unicode normalization; a caller that wants those applies them first.
//! Lengths count Unicode scalar values, so every language counts the same, and
//! a string that is not made of them fails its contract. In Rust such a string
//! cannot be built at all: `String` is UTF-8, and an unpaired surrogate has no
//! UTF-8 spelling. A shape beyond length - a UUID, a URL - is the caller's to
//! check. A score is only compared: rounding it, summing it or weighting it is
//! the caller's, done first, where it can be reviewed.

use alloc::format;
use alloc::string::String;
use alloc::vec::Vec;

use crate::messages::describe_value;
use crate::value::{Refused, Value};
use crate::verdict::{verdict, Level, Verdict};

/// What a slot accepts. The other ports read a string and refuse an unknown
/// kind; an enum leaves no unknown kind to be handed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SlotKind {
    Choice,
    Text,
    Score,
}

/// The largest integer every language holds exactly: JavaScript's limit, as
/// for digest inputs.
const BOUND_MAX: i64 = (1 << 53) - 1;

/// The same limit as a double, which holds it exactly.
const BOUND_MAX_AS_FLOAT: f64 = BOUND_MAX as f64;

const ASCII_WHITESPACE: [char; 6] = [' ', '\t', '\n', '\r', '\u{000c}', '\u{000b}'];

/// What a value is held to. A field the contract does not name cannot be set:
/// the struct has no such field.
#[derive(Clone, Debug, PartialEq)]
pub struct SlotSpec {
    pub name: String,
    pub kind: SlotKind,
    /// The values a choice accepts, compared exactly. A choice needs them;
    /// `None` is absent, and an empty list accepts nothing.
    pub candidates: Option<Vec<String>>,
    /// Bounds a text's length in Unicode scalar values, each in
    /// 0..2^53 - 1; `None` for no bound.
    pub min_length: Option<i64>,
    pub max_length: Option<i64>,
    /// A score's bar, both ends included; `None` for none.
    pub min: Option<f64>,
    pub max: Option<f64>,
}

fn bound(value: Option<i64>, refusal: &'static str, absent: i64) -> Result<i64, Refused> {
    match value {
        None => Ok(absent),
        Some(value) if !(0..=BOUND_MAX).contains(&value) => Err(Refused(refusal)),
        Some(value) => Ok(value),
    }
}

/// A number as this spec has it: a finite double, and an integral one an
/// integer within +/-(2^53 - 1), as every other number in the spec is. Every
/// double past 2^53 - 1 is integral, so that is one range test, which NaN and
/// the infinities fail too.
fn is_number(number: f64) -> bool {
    (-BOUND_MAX_AS_FLOAT..=BOUND_MAX_AS_FLOAT).contains(&number)
}

/// A value read as a score, or `None` when it is not one.
fn score_of(value: &Value) -> Option<f64> {
    match value {
        // Within 2^53 - 1 the conversion is exact.
        Value::Int(held) if (-BOUND_MAX..=BOUND_MAX).contains(held) => Some(*held as f64),
        Value::Float(held) if is_number(*held) => Some(*held),
        _ => None,
    }
}

fn bar(value: Option<f64>, refusal: &'static str, absent: f64) -> Result<f64, Refused> {
    match value {
        None => Ok(absent),
        Some(value) if !is_number(value) => Err(Refused(refusal)),
        Some(value) => Ok(value),
    }
}

fn judge_score(name: &str, value: &Value, floor: f64, ceiling: f64) -> Verdict {
    match score_of(value) {
        None => verdict(
            Level::Halt,
            "slot_invalid",
            format!(
                "slot {}: {} is not a number this spec holds",
                name,
                describe_value(value)
            ),
        ),
        Some(score) if score < floor => verdict(
            Level::Halt,
            "slot_invalid",
            format!("slot {name}: {score} is below {floor}"),
        ),
        Some(score) if score > ceiling => verdict(
            Level::Halt,
            "slot_invalid",
            format!("slot {name}: {score} is above {ceiling}"),
        ),
        Some(score) => verdict(Level::Ok, "slot_accepted", format!("slot {name}: {score}")),
    }
}

fn is_blank(text: &str) -> bool {
    text.trim_matches(|c: char| ASCII_WHITESPACE.contains(&c))
        .is_empty()
}

/// Refuses a spec outside the contract - a choice without candidates, a bound
/// outside 0..2^53 - 1, a min_length above max_length, a min or max that is not
/// a number, a min above max - and otherwise answers a verdict. An unknown kind is the fourth refusal the other ports make, and
/// [`SlotKind`] has already made it.
pub fn validate_slot(spec: &SlotSpec, value: &Value) -> Result<Verdict, Refused> {
    if spec.kind == SlotKind::Choice && spec.candidates.is_none() {
        return Err(Refused("a choice needs candidates"));
    }
    // No bound is the bound every length meets: at least 0, at most the largest.
    let minimum = bound(
        spec.min_length,
        "min_length must be a non-negative integer up to 2^53 - 1",
        0,
    )?;
    let maximum = bound(
        spec.max_length,
        "max_length must be a non-negative integer up to 2^53 - 1",
        BOUND_MAX,
    )?;
    if minimum > maximum {
        return Err(Refused("min_length exceeds max_length"));
    }
    let floor = bar(
        spec.min,
        "min must be a finite number, an integral one within 2^53 - 1",
        f64::NEG_INFINITY,
    )?;
    let ceiling = bar(
        spec.max,
        "max must be a finite number, an integral one within 2^53 - 1",
        f64::INFINITY,
    )?;
    if floor > ceiling {
        return Err(Refused("min exceeds max"));
    }

    let text = match value {
        Value::Null => {
            return Ok(verdict(
                Level::Warning,
                "slot_missing",
                format!("slot {}: no value", spec.name),
            ))
        }
        Value::Text(text) => text,
        other if spec.kind == SlotKind::Score => {
            return Ok(judge_score(&spec.name, other, floor, ceiling))
        }
        other => {
            return Ok(verdict(
                Level::Halt,
                "slot_invalid",
                format!(
                    "slot {}: {} is not a string",
                    spec.name,
                    describe_value(other)
                ),
            ))
        }
    };
    // The other ports check here that the string is made of Unicode scalar
    // values, and halt with `slot_invalid` when it is not. A `String` is that
    // by its type, so the check is the type's and the case cannot arise.
    if is_blank(text) {
        return Ok(verdict(
            Level::Warning,
            "slot_missing",
            format!("slot {}: blank", spec.name),
        ));
    }
    if spec.kind == SlotKind::Score {
        return Ok(verdict(
            Level::Halt,
            "slot_invalid",
            format!(
                "slot {}: a string is not a number, however it reads",
                spec.name
            ),
        ));
    }

    if spec.kind == SlotKind::Choice {
        let candidates: &[String] = match &spec.candidates {
            Some(candidates) => candidates,
            None => &[],
        };
        for candidate in candidates {
            if candidate == text {
                return Ok(verdict(
                    Level::Ok,
                    "slot_accepted",
                    format!("slot {}: {:?} is a candidate", spec.name, text),
                ));
            }
        }
        return Ok(verdict(
            Level::Halt,
            "slot_invalid",
            format!(
                "slot {}: {:?} is not one of {} candidates",
                spec.name,
                text,
                candidates.len()
            ),
        ));
    }
    let length = text.chars().count() as i64;
    if length < minimum {
        return Ok(verdict(
            Level::Halt,
            "slot_invalid",
            format!(
                "slot {}: {} characters, fewer than {}",
                spec.name, length, minimum
            ),
        ));
    }
    if length > maximum {
        return Ok(verdict(
            Level::Halt,
            "slot_invalid",
            format!(
                "slot {}: {} characters, more than {}",
                spec.name, length, maximum
            ),
        ));
    }
    Ok(verdict(
        Level::Ok,
        "slot_accepted",
        format!("slot {}: {} characters", spec.name, length),
    ))
}
