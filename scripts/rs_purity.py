#!/usr/bin/env python3
"""The Rust library is written in a language that has no host in it.

`#![no_std]` is not a lint and not a search through the text: it takes the
standard library out of the crate, so `std::fs`, `std::time`, `std::env`,
`std::process` and randomness are not names this code can write. What is left
is `core`, `alloc` and the crates it declares, and the only one it declares is
SHA-256, which is the dependency the spec allows where a language has none of
its own. The adapter is a separate crate and is not held to any of this: it
reads files and parses JSON, which is what an adapter is for.

Three findings are possible: the crate is not no_std after all, it names `std`
through a back door, or it has grown a dependency.

    rs_purity.py [workspace directory]     default: rust
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# The one dependency the spec allows. What sha2 brings with it is sha2's own,
# and is not written here: this is the list the library itself declares.
ALLOWED = {"sha2"}

LIBRARY = "haltrule"


def main(argv: list[str]) -> int:
    directory = Path(argv[0] if argv else "rust")
    findings: list[str] = []

    sources = sorted((directory / LIBRARY / "src").glob("*.rs"))
    if not sources:
        print("no library source was read, so nothing was checked")
        return 1
    root = directory / LIBRARY / "src" / "lib.rs"
    if "#![no_std]" not in root.read_text(encoding="utf-8"):
        findings.append(
            f"{root} does not say #![no_std], so the host is in the language again"
        )
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for named in ("extern crate std", "use std::", "::std::"):
            if named in text:
                findings.append(
                    f"{source} names {named}, which no_std is there to prevent"
                )

    listed = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=directory,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        print(f"cargo metadata failed: {listed.stderr.strip()[:500]}")
        return 1
    described = json.loads(listed.stdout)
    packages = [one for one in described["packages"] if one["name"] == LIBRARY]
    if not packages:
        findings.append(f"the workspace holds no package named {LIBRARY}")
    for package in packages:
        for dependency in package["dependencies"]:
            if dependency["name"] not in ALLOWED:
                findings.append(
                    f"{LIBRARY} depends on {dependency['name']}, which is not on the allowlist"
                )

    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
