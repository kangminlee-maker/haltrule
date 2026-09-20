//! Words for people. Nothing here decides anything: a verdict's `message` and
//! a refusal's text are not part of conformance, so no fixture can tell one
//! wording from another. Code that only words a message lives here; code that
//! decides lives in the part.

use alloc::format;
use alloc::string::String;

use crate::value::Value;

pub(crate) fn show_amount(used: i64, limit: Option<i64>) -> String {
    match limit {
        None => format!("{} of no cap", used),
        Some(limit) => format!("{} of {}", used, limit),
    }
}

pub(crate) fn describe_value(value: &Value) -> &'static str {
    // The other ports end this with a catch-all "a value". There is no arm
    // for it here: the enum is closed, so these are all the values there are.
    match value {
        Value::Null => "nothing",
        Value::Bool(_) => "a boolean",
        Value::Int(_) | Value::Float(_) => "a number",
        Value::Text(_) => "a string",
        Value::List(_) => "a list",
        Value::Map(_) => "a map",
    }
}
