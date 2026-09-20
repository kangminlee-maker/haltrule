//! Reading the fixtures and printing one line per case. The adapter holds no
//! expectation and compares nothing: scripts/conform.py does that.

use crate::line::{write_value, Unwritable};
use crate::sections::{section_for, Trouble};
use haltrule::{Map, Value};
use indexmap::IndexMap;
use serde_json::value::RawValue;
use serde_json::Value as Json;
use std::io::Write;

/// The fields of one case, its id and expectation taken out.
pub type Inputs = serde_json::Map<String, Json>;

fn line_for(parts: &Map) -> String {
    match write_value(&Value::Map(parts.clone())) {
        Ok(line) => line,
        // A line that cannot be written is a defect in this adapter.
        Err(Unwritable(why)) => {
            eprintln!("adapter: {why}");
            std::process::exit(1);
        }
    }
}

fn text_value(held: &str) -> Value {
    Value::Text(held.to_string())
}

fn run_case(out: &mut impl Write, section_name: &str, raw: &RawValue) -> Result<(), String> {
    // The case is read twice: once without decoding its strings, to learn its
    // id even when a string in it has no Rust spelling, and once fully.
    let held: IndexMap<String, Box<RawValue>> =
        serde_json::from_str(raw.get()).map_err(|err| err.to_string())?;
    let id_raw = held.get("id").ok_or("a case without an id")?;
    let id: String = serde_json::from_str(id_raw.get()).map_err(|err| err.to_string())?;

    let mut parts = Map::new();
    parts.insert("id".to_string(), text_value(&id));
    parts.insert("section".to_string(), text_value(section_name));
    let compute =
        section_for(section_name).ok_or(format!("no function for section {section_name}"))?;

    // A case this parser will not read holds a string Rust has no spelling
    // for: an unpaired surrogate escape, which UTF-8 cannot carry. The parser
    // is the check, and the case is sat out.
    let mut fields: Inputs = match serde_json::from_str(raw.get()) {
        Ok(fields) => fields,
        Err(_) => {
            parts.insert("unbuildable".to_string(), Value::Bool(true));
            writeln!(out, "{}", line_for(&parts)).map_err(|err| err.to_string())?;
            return Ok(());
        }
    };
    fields.remove("id");
    fields.remove("expect");

    match compute(&fields) {
        Err(Trouble::Unbuildable) => {
            parts.insert("unbuildable".to_string(), Value::Bool(true));
        }
        // Reported under the case's id, never hidden.
        Err(Trouble::Raised(why)) => {
            parts.insert("raised".to_string(), text_value(&why));
        }
        Ok(actual) => match write_value(&actual) {
            // A result the line cannot carry is this port's failure, under its id.
            Err(Unwritable(why)) => {
                parts.insert("raised".to_string(), text_value(&why));
            }
            Ok(_) => {
                parts.insert("actual".to_string(), actual);
            }
        },
    }
    writeln!(out, "{}", line_for(&parts)).map_err(|err| err.to_string())
}

pub fn read_file(path: &str, out: &mut impl Write) -> Result<(), String> {
    let text = std::fs::read_to_string(path).map_err(|err| format!("{path}: {err}"))?;
    // Every value stays raw until its own case is read: a string with no Rust
    // spelling must not fail the file it sits in.
    let file: IndexMap<String, Box<RawValue>> =
        serde_json::from_str(&text).map_err(|err| format!("{path}: {err}"))?;
    for (section_name, held) in &file {
        if section_name == "fixture_version" {
            continue;
        }
        let cases: Vec<Box<RawValue>> =
            serde_json::from_str(held.get()).map_err(|err| format!("{path}: {err}"))?;
        for raw in &cases {
            run_case(out, section_name, raw).map_err(|why| format!("{path}: {why}"))?;
        }
    }
    Ok(())
}

/// Every fixture file, in one order on every machine.
pub fn fixture_paths() -> Result<Vec<String>, String> {
    let mut found = Vec::new();
    let mut stack = vec![std::path::PathBuf::from("fixtures")];
    while let Some(here) = stack.pop() {
        let entries =
            std::fs::read_dir(&here).map_err(|err| format!("{}: {err}", here.display()))?;
        for entry in entries {
            let entry = entry.map_err(|err| err.to_string())?;
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|kind| kind == "json") {
                found.push(path.to_string_lossy().replace('\\', "/"));
            }
        }
    }
    found.sort();
    Ok(found)
}
