//! A result line, as fixtures/README.md defines it; fixtures/protocol/v0.json
//! holds its vectors. Written out member by member, because serde_json's own
//! writer orders map keys by code point and the protocol orders them by
//! UTF-16 code unit, which differs above U+FFFF.

use haltrule::{Map, Value};

/// The largest integer a result line carries: 2^53 - 1.
const LINE_MAX_INTEGER: i64 = (1 << 53) - 1;

/// A value a result line cannot carry. It is this port's failure, and the
/// line says so under the case's id.
#[derive(Debug, Clone)]
pub struct Unwritable(pub String);

pub fn refused() -> Value {
    let mut held = Map::new();
    held.insert("refused".to_string(), Value::Bool(true));
    Value::Map(held)
}

pub fn write_value(value: &Value) -> Result<String, Unwritable> {
    match value {
        Value::Null => Ok("null".to_string()),
        Value::Bool(true) => Ok("true".to_string()),
        Value::Bool(false) => Ok("false".to_string()),
        Value::Int(held) => {
            if *held > LINE_MAX_INTEGER || *held < -LINE_MAX_INTEGER {
                return Err(Unwritable(format!(
                    "a result holds {held}; a result number is an integer within +/-(2^53 - 1)"
                )));
            }
            Ok(held.to_string())
        }
        Value::Float(number) => {
            let rounded = format!("{number:.0}");
            let integral = !number.is_nan()
                && !number.is_infinite()
                && rounded.parse::<f64>().map(|back| back == *number) == Ok(true);
            match rounded.parse::<i64>() {
                // An integer is an integer however the language holds it, and
                // a zero is written without its sign.
                Ok(held) if integral && held.abs() <= LINE_MAX_INTEGER => Ok(held.to_string()),
                _ => Err(Unwritable(format!(
                    "a result holds {number}; a result number is an integer within +/-(2^53 - 1)"
                ))),
            }
        }
        // A Rust string is made of Unicode scalar values by its type, which is
        // the check the other ports write out.
        Value::Text(held) => Ok(write_string(held)),
        Value::List(items) => {
            let mut parts = Vec::with_capacity(items.len());
            for item in items {
                parts.push(write_value(item)?);
            }
            Ok(format!("[{}]", parts.join(",")))
        }
        Value::Map(held) => {
            let mut keys: Vec<&String> = held.keys().collect();
            keys.sort_by(|left, right| left.encode_utf16().cmp(right.encode_utf16()));
            let mut parts = Vec::with_capacity(keys.len());
            for key in keys {
                parts.push(format!(
                    "{}:{}",
                    write_string(key),
                    write_value(&held[key])?
                ));
            }
            Ok(format!("{{{}}}", parts.join(",")))
        }
    }
}

fn write_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for character in value.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            _ if character < '\u{20}' => {
                out.push_str(&format!("\\u{:04x}", character as u32));
            }
            _ => out.push(character),
        }
    }
    out.push('"');
    out
}
