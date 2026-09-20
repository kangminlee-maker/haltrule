//! The Rust port's adapter: it reads the fixtures and prints one line per
//! case, and holds no expectation of its own. scripts/conform.py compares the
//! lines with what the fixtures expect.
//!
//! Run from the repository root, with the fixture files as arguments or with
//! none, which reads every file under fixtures/.

mod decode;
mod line;
mod run;
mod sections;

use std::io::{BufWriter, Write};

fn main() {
    let given: Vec<String> = std::env::args().skip(1).collect();
    let paths = if given.is_empty() {
        match run::fixture_paths() {
            Ok(found) => found,
            Err(why) => fail(&why),
        }
    } else {
        given
    };
    let stdout = std::io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    for path in &paths {
        if let Err(why) = run::read_file(path, &mut out) {
            let _ = out.flush();
            fail(&why);
        }
    }
    if let Err(why) = out.flush() {
        fail(&why.to_string());
    }
}

fn fail(why: &str) -> ! {
    eprintln!("adapter: {why}");
    std::process::exit(1);
}

/// The bridge a Rust mutation tool needs. cargo-mutants runs `cargo test` and
/// decides what a mutant is worth from what that test reaches, where the tools
/// for TypeScript and Python take any command. So the run needs a test, and
/// the test has to reach the library in this process rather than in a child.
///
/// It holds no expectation of its own: it writes the lines the adapter would
/// write and hands the judging to scripts/conform.py, which judges every port
/// alike. Nothing but a mutation run needs it.
///
/// HALTRULE_ROOT says where the fixtures and the driver are, for a run that
/// works in a copy of the workspace.
#[cfg(test)]
mod bridge {
    use std::io::{BufWriter, Write};

    #[test]
    fn the_fixtures_pass() {
        let root = std::env::var("HALTRULE_ROOT").unwrap_or_else(|_| "../..".to_string());
        let root = std::fs::canonicalize(&root).expect("the repository root");
        std::env::set_current_dir(&root).expect("working in the repository root");

        let written = std::env::temp_dir().join(format!("haltrule-{}.lines", std::process::id()));
        let mut out =
            BufWriter::new(std::fs::File::create(&written).expect("a file for the lines"));
        for path in crate::run::fixture_paths().expect("the fixture files") {
            crate::run::read_file(&path, &mut out).expect("reading a fixture file");
        }
        out.flush().expect("the lines");
        drop(out);

        let said = std::process::Command::new("python3")
            .arg(root.join("scripts").join("conform.py"))
            .arg("check")
            .arg("cat")
            .arg(&written)
            .current_dir(&root)
            .output()
            .expect("the driver runs");
        let _ = std::fs::remove_file(&written);
        assert!(
            said.status.success(),
            "the fixtures do not pass:\n{}{}",
            String::from_utf8_lossy(&said.stdout),
            String::from_utf8_lossy(&said.stderr),
        );
    }
}
