use alloc::string::String;

/// Stamped on every verdict: draft 0 until the spec freezes.
pub const SPEC: &str = "haltrule/0";

/// What a verdict says to do: carry on, carry on and note it, or stop.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Ok,
    Warning,
    Halt,
}

impl Level {
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Warning => "warning",
            Level::Halt => "halt",
        }
    }
}

/// The one shape every part returns. It is a value, never an error: a part
/// decides, the caller acts. `message` is for a person and is not part of
/// conformance; the other four fields are.
#[derive(Clone, Debug, PartialEq)]
pub struct Verdict {
    pub spec: &'static str,
    pub verdict: Level,
    pub reason: &'static str,
    pub message: String,
    /// Where the next run should pick up, when that is knowable.
    pub resume: Option<String>,
}

pub(crate) fn verdict(level: Level, reason: &'static str, message: String) -> Verdict {
    Verdict {
        spec: SPEC,
        verdict: level,
        reason,
        message,
        resume: None,
    }
}
