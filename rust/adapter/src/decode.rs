//! The Rust adapter's half that turns a fixture case into a call.
//!
//! Two ways a value can fail to become an argument, and they are different
//! answers. A value whose KIND this language has no spelling for - the
//! $unsupported tags, an integer wider than i64 - cannot be handed over at
//! all: that is [`Unbuildable`], and the case is sat out. A value that exists
//! here but is outside the part's contract - a negative cap, a spec field the
//! struct does not have - is refused, which is what every port answers.

use haltrule::{Map, Value};
use serde_json::Value as Json;

/// What a decoder answers: the value, or the reason there is none.
pub type Built<T> = Result<T, Refusal>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// Outside the part's contract: every port answers `refused`.
    Refused,
    /// Outside this language: the case is sat out.
    Unbuildable,
}

/// Builds one value of the spec's model. The fixtures never hold a raw JSON
/// number, so every number arrives as a $number or $bigint tag.
pub fn decode_value(raw: Option<&Json>) -> Built<Value> {
    match raw {
        None | Some(Json::Null) => Ok(Value::Null),
        Some(Json::Bool(held)) => Ok(Value::Bool(*held)),
        Some(Json::String(held)) => Ok(Value::Text(held.clone())),
        // Unreachable: an input never holds a raw JSON number.
        Some(Json::Number(held)) => Ok(Value::Float(held.as_f64().ok_or(Refusal::Refused)?)),
        Some(Json::Array(items)) => {
            let mut list = Vec::with_capacity(items.len());
            for item in items {
                list.push(decode_value(Some(item))?);
            }
            Ok(Value::List(list))
        }
        Some(Json::Object(members)) => {
            if members.len() == 1 {
                let (tag, literal) = members.iter().next().expect("one member");
                if let Json::String(text) = literal {
                    if tag == "$number" || tag == "$bigint" || tag == "$unsupported" {
                        return from_tag(tag, text);
                    }
                }
            }
            let mut held = Map::new();
            for (key, item) in members {
                held.insert(key.clone(), decode_value(Some(item))?);
            }
            Ok(Value::Map(held))
        }
    }
}

fn from_tag(tag: &str, literal: &str) -> Built<Value> {
    match tag {
        "$number" => Ok(Value::Float(decode_double(literal)?)),
        "$bigint" => match literal.parse::<i64>() {
            Ok(integer) => Ok(Value::Int(integer)),
            // Wider than i64, which this language has no integer for.
            Err(_) => Err(Refusal::Unbuildable),
        },
        // undefined, a class instance, a map with a key that is not a string,
        // a hole in a list: none of them has a Rust spelling.
        _ => Err(Refusal::Unbuildable),
    }
}

/// Reads a $number literal. Rust's own parser reads the three values JSON
/// cannot spell - `NaN`, `Infinity`, `-Infinity` - by those names, and the
/// driver has already held every literal to the fixture grammar.
fn decode_double(literal: &str) -> Built<f64> {
    literal.parse::<f64>().map_err(|_| Refusal::Refused)
}

/// Reads a number that must be an integer this port can hold: a cap, an
/// amount, a bound, a count. Anything else is outside the contract, so the
/// error is a refusal and not a reason to sit the case out.
pub fn decode_i64(raw: Option<&Json>) -> Built<i64> {
    match decode_value(raw)? {
        Value::Int(held) => Ok(held),
        Value::Float(number) => {
            // The range is decided in decimal, and never by a cast: Rust's
            // own cast would saturate, and a saturated cap is a wrong answer
            // rather than a refusal. A value that is not an integer does not
            // come back from the round trip - NaN included, which equals
            // nothing, itself included - and an infinity has no digits.
            let text = format!("{number:.0}");
            if text.parse::<f64>() != Ok(number) {
                return Err(Refusal::Refused);
            }
            text.parse::<i64>().map_err(|_| Refusal::Refused)
        }
        _ => Err(Refusal::Refused),
    }
}

/// An integer that may be absent - null or not given - and is then None.
pub fn decode_optional_i64(raw: Option<&Json>) -> Built<Option<i64>> {
    match raw {
        None | Some(Json::Null) => Ok(None),
        given => decode_i64(given).map(Some),
    }
}

/// Refuses a map argument that is not a map, or that holds a field the
/// contract does not name. Go's struct and Python's keyword signature do this
/// for their ports; here the fields arrive as JSON and the names are checked.
pub fn strictly<'a>(
    raw: Option<&'a Json>,
    names: &[&str],
) -> Built<&'a serde_json::Map<String, Json>> {
    let held = raw.and_then(Json::as_object).ok_or(Refusal::Refused)?;
    for key in held.keys() {
        if !names.contains(&key.as_str()) {
            return Err(Refusal::Refused);
        }
    }
    Ok(held)
}

/// A field that must be there and be a string.
pub fn text(raw: Option<&Json>) -> Built<String> {
    match raw {
        Some(Json::String(held)) => Ok(held.clone()),
        _ => Err(Refusal::Refused),
    }
}

/// A string that may be absent - null or not given - and is then None.
pub fn optional_text(raw: Option<&Json>) -> Built<Option<String>> {
    match raw {
        None | Some(Json::Null) => Ok(None),
        given => text(given).map(Some),
    }
}
