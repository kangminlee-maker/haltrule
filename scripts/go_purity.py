#!/usr/bin/env python3
"""The Go modules import only what the allowlist holds.

Go has no way to compile a package with the host taken away, as TypeScript
does, and no mainstream linter that bans an import by default. It does have
something better than a search through the text: a package can reach nothing it
has not imported - there is no global object, no clock built into the language,
no code built from a string - so its import list is the whole of what it can
reach, and `go list` prints that list.

Every package in the module is held to the allowlist except the adapter, which
is the one package named main: it may read files and write lines, and it is not
policy. A package the allowlist does not name, or one that appears with no
imports read at all, is a finding.

    go_purity.py [module directory]     default: go
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Every import a policy module may have. Each is a pure function of its
# arguments: no clock, no randomness, no files, no network, no locale.
ALLOWED = {
    "crypto/sha256",  # the one dependency the spec allows, where a standard library has it
    "encoding/hex",
    "errors",
    "fmt",
    "math",
    "sort",
    "strconv",
    "strings",
    "unicode",
    "unicode/utf16",
    "unicode/utf8",
}


def main(argv: list[str]) -> int:
    directory = Path(argv[0] if argv else "go")
    listed = subprocess.run(
        ["go", "list", "-json", "./..."],
        cwd=directory,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        print(f"go list failed: {listed.stderr.strip()[:500]}")
        return 1
    findings: list[str] = []
    packages = 0
    decoder = json.JSONDecoder()
    text = listed.stdout.lstrip()
    while text:
        described, at = decoder.raw_decode(text)
        text = text[at:].lstrip()
        packages += 1
        if described.get("Name") == "main":
            continue
        for imported in described.get("Imports", []):
            if imported not in ALLOWED:
                findings.append(
                    f"{described['ImportPath']} imports {imported}, which is not on the allowlist"
                )
    if packages == 0:
        findings.append("no package was read, so nothing was checked")
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
