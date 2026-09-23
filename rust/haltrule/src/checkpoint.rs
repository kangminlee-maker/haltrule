//! Checkpoint - canonical form and digest of a value, and a verdict on whether
//! an artifact an earlier run recorded may be reused.
//!
//! The arguments are typed, and an argument of another type cannot be built:
//! `None` is absent, and nothing else is. What the artifact records is data,
//! written by whoever wrote it, and is read leniently instead.

use alloc::borrow::ToOwned;
use alloc::collections::BTreeMap;
use alloc::format;
use alloc::string::String;
use alloc::vec;
use alloc::vec::Vec;
use core::cmp::Ordering;

use sha2::{Digest, Sha256};

use crate::value::{Map, Refused, Value};
use crate::verdict::{verdict, Level, Verdict, SPEC};

/// The largest integer every implementation represents exactly: 2^53 - 1.
const MAX_SAFE_INTEGER: i64 = (1 << 53) - 1;

/// The same bound as a double. It is exact: 2^53 - 1 is representable.
const MAX_SAFE_INTEGER_AS_FLOAT: f64 = MAX_SAFE_INTEGER as f64;

/// The deepest nesting of lists and maps a digest input may have. A bound
/// every language reaches without exhausting its stack, so all of them halt at
/// the same depth instead of each crashing at its own.
const MAX_DEPTH: usize = 100;

const DIGEST_INPUT_FLOAT: &str = "digest_input_float";
const DIGEST_INPUT_INT_RANGE: &str = "digest_input_int_range";
const DIGEST_INPUT_UNSUPPORTED: &str = "digest_input_unsupported";

/// A value outside the digest value model: a halt verdict whose message names
/// where the value sits, a path being no thing two languages spell alike.
fn halted(reason: &'static str, message: String) -> Verdict {
    verdict(Level::Halt, reason, message)
}

/// The canonical form of a value: an RFC 8785 subset. Map keys are sorted by
/// UTF-16 code unit, there is no insignificant whitespace, integers are
/// written in decimal, and strings carry the spec's escapes. Strings are not
/// Unicode-normalized.
pub fn canonicalize(value: &Value) -> Result<String, Verdict> {
    encode_value(value, "$", 0)
}

/// "sha256:" and the lowercase hex SHA-256 of the canonical form's UTF-8
/// bytes, or the halt [`canonicalize`] returned.
pub fn checkpoint_digest(value: &Value) -> Result<String, Verdict> {
    let canonical = canonicalize(value)?;
    let sum = Sha256::digest(canonical.as_bytes());
    let mut out = String::from("sha256:");
    for byte in sum {
        out.push(hex_digit(byte >> 4));
        out.push(hex_digit(byte & 0x0f));
    }
    Ok(out)
}

fn hex_digit(nibble: u8) -> char {
    char::from(b"0123456789abcdef"[nibble as usize])
}

fn encode_value(value: &Value, at: &str, depth: usize) -> Result<String, Verdict> {
    // The match is exhaustive because `Value` is closed, so there is no other
    // kind to meet: what the reference answers for below its last case cannot
    // be handed to this port at all.
    match value {
        Value::Null => Ok("null".to_owned()),
        Value::Bool(held) => Ok(if *held { "true" } else { "false" }.to_owned()),
        Value::Int(held) => {
            if *held > MAX_SAFE_INTEGER || *held < -MAX_SAFE_INTEGER {
                return Err(halted(
                    DIGEST_INPUT_INT_RANGE,
                    format!("{at}: {held} is outside +/-(2^53 - 1); render it as a string if it belongs in a digest"),
                ));
            }
            Ok(format!("{held}"))
        }
        Value::Float(held) => encode_float(*held, at),
        Value::Text(held) => Ok(encode_string(held)),
        Value::List(held) => {
            if depth >= MAX_DEPTH {
                return Err(too_deep(at));
            }
            let mut out = String::from("[");
            for (index, item) in held.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push_str(&encode_value(item, &format!("{at}[{index}]"), depth + 1)?);
            }
            out.push(']');
            Ok(out)
        }
        Value::Map(held) => {
            if depth >= MAX_DEPTH {
                return Err(too_deep(at));
            }
            let mut out = String::from("{");
            for (index, (key, member)) in sorted_entries(held).into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push_str(&encode_string(key));
                out.push(':');
                out.push_str(&encode_value(member, &format!("{at}.{key}"), depth + 1)?);
            }
            out.push('}');
            Ok(out)
        }
    }
}

fn too_deep(at: &str) -> Verdict {
    halted(
        DIGEST_INPUT_UNSUPPORTED,
        format!("{at}: nested deeper than {MAX_DEPTH} lists and maps"),
    )
}

/// A number the caller held as a double. The reference asks whether it is an
/// integer before asking whether it is in range; this asks the range first,
/// and meets the same halt on every input. Above 2^52 the doubles are spaced a
/// whole apart, so every double whose magnitude passes 2^53 - 1 is an integer
/// already and could only ever have failed the range test. Ruling the range
/// out first is also what makes the integrality test below exact without
/// `std`: `trunc` and `floor` are the standard library's, but within the range
/// a cast to `i64` truncates and casts back losing nothing, so a number that
/// survives the round trip unchanged is the integer it looks like. A negative
/// zero survives it as zero, which is the answer wanted.
fn encode_float(number: f64, at: &str) -> Result<String, Verdict> {
    if number.is_nan() || number.is_infinite() {
        return Err(not_an_integer(number, at));
    }
    if number > MAX_SAFE_INTEGER_AS_FLOAT || number < -MAX_SAFE_INTEGER_AS_FLOAT {
        return Err(halted(
            DIGEST_INPUT_INT_RANGE,
            format!("{at}: {number} is outside +/-(2^53 - 1); render it as a string if it belongs in a digest"),
        ));
    }
    let whole = number as i64;
    if number != whole as f64 {
        return Err(not_an_integer(number, at));
    }
    // A negative zero is written "0", as every zero is.
    Ok(format!("{whole}"))
}

fn not_an_integer(number: f64, at: &str) -> Verdict {
    halted(
        DIGEST_INPUT_FLOAT,
        format!(
            "{at}: {number} is not an integer; render it as a string if it belongs in a digest"
        ),
    )
}

/// A map's members in the spec's order, which is by UTF-16 code unit and not
/// Rust's own, because they disagree above U+FFFF.
fn sorted_entries(held: &Map) -> Vec<(&String, &Value)> {
    let mut entries: Vec<(&String, &Value)> = held.iter().collect();
    // The entries arrive in one order already - a `BTreeMap`'s, by UTF-8 byte -
    // and the sort below is stable, so they are put in one order before being
    // put in the spec's. The answer is the same either way; what this settles
    // is the work done to reach it.
    entries.sort_by(|left, right| cmp_utf16(left.0, right.0));
    entries
}

fn cmp_utf16(left: &str, right: &str) -> Ordering {
    let mut left_units = left.encode_utf16();
    let mut right_units = right.encode_utf16();
    loop {
        match (left_units.next(), right_units.next()) {
            (Some(left_unit), Some(right_unit)) => {
                if left_unit != right_unit {
                    return left_unit.cmp(&right_unit);
                }
            }
            (None, None) => return Ordering::Equal,
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
        }
    }
}

/// Writes a string of Unicode scalar values, quoted, with exactly seven
/// two-character escapes, every other code point below U+0020 as `\u00xx` in
/// lowercase hex, and everything else as itself.
///
/// The reference halts on a string that is not made of scalar values, because
/// an unpaired surrogate has no UTF-8 spelling. A Rust `str` is made of scalar
/// values by construction, so that halt has nothing to catch here and this
/// cannot fail: the case is one the port's types shut out before it ran.
fn encode_string(value: &str) -> String {
    let mut out = String::from("\"");
    for character in value.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            _ => {
                if character < '\u{20}' {
                    out.push_str(&format!("\\u{:04x}", character as u32));
                } else {
                    out.push(character);
                }
            }
        }
    }
    out.push('"');
    out
}

// ------------------------------------------------------------ reuse verdict

/// The vocabulary every artifact is read through. Only [`ArtifactStatus::Complete`]
/// is reusable.
///
/// The reference holds a status as a string and refuses a `status_map` that
/// answers outside the vocabulary. This enum is closed, so an answer outside
/// it cannot be built: the same contract, kept by the type instead of by code.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArtifactStatus {
    Complete,
    Partial,
    Failed,
    Blocked,
}

impl ArtifactStatus {
    /// The default map, which sends each word of the vocabulary to itself, and
    /// the only way to name a status from text: a word outside the vocabulary
    /// is `None`, which is where a caller's bad `status_map` value is refused.
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "complete" => Some(Self::Complete),
            "partial" => Some(Self::Partial),
            "failed" => Some(Self::Failed),
            "blocked" => Some(Self::Blocked),
            _ => None,
        }
    }
}

/// One verdict about an artifact: the five fields of a [`Verdict`], with
/// `stage_id` and `subject_ref` beside them, and what its reason adds. A
/// caller's own issue may lay any of them over the defaults, so it is a map
/// and not the struct, and its `reason` is text and not a `&'static str`.
pub type Issue = Map;

/// What the caller expects of an artifact now. `None` is absent; an empty map
/// is present.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CheckpointArgs {
    pub stage_id: String,
    pub subject_ref: Option<String>,
    /// What was recorded; `None` when nothing was.
    pub artifact: Option<Map>,
    /// An absent expectation is not checked; any string is compared, the empty
    /// one included.
    pub expected_contract_revision: Option<String>,
    pub expected_stage_config_digest: Option<String>,
    pub expected_dependency_digests: Option<BTreeMap<String, String>>,
    /// Defaults to `stage_id`.
    pub required_resume_from_stage: Option<String>,
    pub validation_issues: Option<Vec<Map>>,
    /// Replaces the default map, which sends each word of the vocabulary to
    /// itself; a status it does not name is not reusable.
    pub status_map: Option<BTreeMap<String, ArtifactStatus>>,
}

fn text(value: Option<&str>) -> Value {
    match value {
        None => Value::Null,
        Some(held) => Value::Text(held.to_owned()),
    }
}

/// Decides whether a recorded artifact may be reused. It answers every issue
/// found, in this order: status, contract revision, stage-config digest,
/// dependency digests by UTF-16 key order, the caller's validation issues.
/// With none it answers one valid issue, so the answer is never empty.
///
/// The reference also refuses a status map that answers outside the
/// vocabulary. Here [`ArtifactStatus`] is closed and such a map cannot be
/// built, so that refusal has nothing to catch; what is left to refuse is a
/// caller's own validation issue that signs the spec or gives a verdict
/// outside the three.
pub fn evaluate_checkpoint_artifact(args: &CheckpointArgs) -> Result<Vec<Issue>, Refused> {
    let resume = args
        .required_resume_from_stage
        .as_deref()
        .unwrap_or(&args.stage_id);
    let base = |level: Level, reason: &str, message: String| -> Issue {
        let mut issue = Issue::new();
        issue.insert("spec".to_owned(), Value::Text(SPEC.to_owned()));
        issue.insert("verdict".to_owned(), Value::Text(level.as_str().to_owned()));
        issue.insert("reason".to_owned(), Value::Text(reason.to_owned()));
        issue.insert(
            "message".to_owned(),
            Value::Text(format!("{}: {}", args.stage_id, message)),
        );
        issue.insert("resume".to_owned(), Value::Text(resume.to_owned()));
        issue.insert("stage_id".to_owned(), Value::Text(args.stage_id.clone()));
        issue.insert("subject_ref".to_owned(), text(args.subject_ref.as_deref()));
        issue
    };

    // A caller's issue is an argument like the rest: what is outside the contract is refused before
    // anything is judged, an absent artifact included. A null field is an absent one.
    for given in args.validation_issues.iter().flatten() {
        for (key, field) in given {
            if *field != Value::Null {
                holds_what_a_verdict_promises(key, field)?;
            }
        }
    }

    let artifact = match args.artifact.as_ref() {
        None => {
            return Ok(vec![base(
                Level::Halt,
                "artifact_missing",
                "nothing was recorded".to_owned(),
            )])
        }
        Some(held) => held,
    };

    let mut issues: Vec<Issue> = Vec::new();

    let status = artifact.get("status");
    if js_falsy(status) {
        issues.push(base(
            Level::Halt,
            "artifact_status_missing",
            "what was recorded has no status".to_owned(),
        ));
    } else if resolve_status(status, args.status_map.as_ref()) != Some(ArtifactStatus::Complete) {
        let mut issue = base(
            Level::Halt,
            "artifact_status_not_reusable",
            "the recorded status is not one that may be reused".to_owned(),
        );
        issue.insert("actual_status".to_owned(), recorded_value(status));
        issues.push(issue);
    }

    let revision = artifact.get("contract_revision");
    if let Some(expected) = args.expected_contract_revision.as_deref() {
        if js_falsy(revision) {
            let mut issue = base(
                Level::Halt,
                "contract_revision_missing",
                format!("a contract revision of {expected:?} is expected and none is recorded"),
            );
            issue.insert(
                "expected_contract_revision".to_owned(),
                Value::Text(expected.to_owned()),
            );
            issues.push(issue);
        } else if !same_text(revision, expected) {
            let mut issue = base(
                Level::Halt,
                "contract_revision_mismatch",
                format!("the recorded contract revision is not the expected {expected:?}"),
            );
            issue.insert(
                "expected_contract_revision".to_owned(),
                Value::Text(expected.to_owned()),
            );
            issue.insert(
                "actual_contract_revision".to_owned(),
                recorded_value(revision),
            );
            issues.push(issue);
        }
    }

    let config = artifact.get("stage_config_digest");
    if let Some(expected) = args.expected_stage_config_digest.as_deref() {
        if !same_text(config, expected) {
            let mut issue = base(
                Level::Halt,
                "stage_config_digest_mismatch",
                format!("the recorded stage config digest is not the expected {expected:?}"),
            );
            issue.insert(
                "expected_stage_config_digest".to_owned(),
                Value::Text(expected.to_owned()),
            );
            issue.insert(
                "actual_stage_config_digest".to_owned(),
                recorded_value(config),
            );
            issues.push(issue);
        }
    }

    // A recorded `dependency_digests` that is not a map records nothing.
    let recorded = match artifact.get("dependency_digests") {
        Some(Value::Map(held)) => Some(held),
        _ => None,
    };
    if let Some(expected_digests) = args.expected_dependency_digests.as_ref() {
        let mut expected_ids: Vec<(&String, &String)> = expected_digests.iter().collect();
        expected_ids.sort_by(|left, right| cmp_utf16(left.0, right.0));
        for (id, expected) in expected_ids {
            let actual = recorded.and_then(|held| held.get(id.as_str()));
            if !same_text(actual, expected) {
                let mut issue = base(
                    Level::Halt,
                    "dependency_digest_mismatch",
                    format!("the dependency {id:?} moved"),
                );
                issue.insert("dependency_id".to_owned(), Value::Text(id.clone()));
                issue.insert("expected_digest".to_owned(), Value::Text(expected.clone()));
                issue.insert("actual_digest".to_owned(), recorded_value(actual));
                issues.push(issue);
            }
        }
    }

    for given in args.validation_issues.iter().flatten() {
        let mut issue = base(
            Level::Halt,
            "validation_issue",
            "the caller's own validation found something".to_owned(),
        );
        for (key, field) in given {
            if *field == Value::Null {
                continue;
            }
            issue.insert(key.clone(), field.clone());
        }
        issues.push(issue);
    }

    if issues.is_empty() {
        issues.push(base(
            Level::Ok,
            "checkpoint_valid",
            "the artifact may be reused".to_owned(),
        ));
    }
    // An ok verdict has no resume, whoever set it.
    for issue in issues.iter_mut() {
        if issue.get("verdict") == Some(&Value::Text(Level::Ok.as_str().to_owned())) {
            issue.insert("resume".to_owned(), Value::Null);
        }
    }
    Ok(issues)
}

/// Refuses a caller's field that would leave the issue outside the shape it is
/// laid over. Everything the verdict does not name is the caller's own and is
/// not judged.
fn holds_what_a_verdict_promises(key: &str, field: &Value) -> Result<(), Refused> {
    match key {
        // A caller may disagree with a verdict, not sign one.
        "spec" => Err(Refused(
            "a validation issue carries a spec, which only the library says",
        )),
        "verdict" if !matches!(field, Value::Text(held) if known_level(held)) => Err(Refused(
            "a validation issue's verdict is not one of the three",
        )),
        "reason" | "message" | "resume" | "stage_id" | "subject_ref"
            if !matches!(field, Value::Text(_)) =>
        {
            Err(Refused("a validation issue's field is not text"))
        }
        _ => Ok(()),
    }
}

/// Whether a level is one of the three. The library never asks it of itself;
/// a caller's own verdict, laid over a checkpoint issue, is the one place a
/// level arrives from outside, and it arrives as text.
fn known_level(named: &str) -> bool {
    named == Level::Ok.as_str() || named == Level::Warning.as_str() || named == Level::Halt.as_str()
}

fn resolve_status(
    status: Option<&Value>,
    status_map: Option<&BTreeMap<String, ArtifactStatus>>,
) -> Option<ArtifactStatus> {
    let named = match status {
        Some(Value::Text(held)) => held,
        _ => return None,
    };
    match status_map {
        None => ArtifactStatus::from_name(named),
        Some(held) => held.get(named.as_str()).copied(),
    }
}

/// What a recorded value is, for an `actual_` member: what the artifact holds,
/// null when it holds nothing.
fn recorded_value(value: Option<&Value>) -> Value {
    value.cloned().unwrap_or(Value::Null)
}

/// What "absent" means of a recorded value: null, false, 0, NaN and the empty
/// string. An empty list or map is present. A field the artifact does not
/// record is null, which is absent as everywhere else.
fn js_falsy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::Bool(held)) => !*held,
        Some(Value::Int(held)) => *held == 0,
        Some(Value::Float(held)) => *held == 0.0 || held.is_nan(),
        Some(Value::Text(held)) => held.is_empty(),
        _ => false,
    }
}

/// What equality means between a recorded value and an expectation: the same
/// string, so a recorded 1 is not "1" and a recorded ["v2"] is not "v2".
fn same_text(recorded: Option<&Value>, expected: &str) -> bool {
    matches!(recorded, Some(Value::Text(held)) if held == expected)
}
