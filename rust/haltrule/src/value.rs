use alloc::collections::BTreeMap;
use alloc::string::String;
use alloc::vec::Vec;

/// The spec's value model: null, booleans, numbers, strings, lists, and maps
/// with string keys. An enum is closed, so a value outside the model cannot
/// be built - the same contract the other ports keep with a check.
///
/// Float is here although the model holds only integers: a caller must be
/// able to hand over the fractional number that a digest halts on. A number
/// is an integer when its value is integral, however it is held, so
/// `Float(2.0)` and `Int(2)` canonicalize alike.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    List(Vec<Value>),
    Map(Map),
}

/// A map of the value model. Its order is the keys' own; the canonical form
/// and a result line both sort by UTF-16 code unit, which is not this order.
pub type Map = BTreeMap<String, Value>;

/// An argument outside a part's contract. The call fails and changes nothing:
/// a refusal is not a verdict and carries no reason. The text is for a person
/// reading a stack trace and is not part of conformance.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refused(pub &'static str);
