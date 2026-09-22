package main

// The bridge a Go mutation tool needs. Go's mutation testers run `go test` and
// decide what to mutate from what that test covers, where the tools for
// TypeScript and Python take any command. So the run needs a test, and the
// test has to reach the policy modules in this process rather than in a child.
//
// It holds no expectation of its own: it writes the lines the adapter would
// write and hands the judging to scripts/conform.py, which judges every port
// alike. Nothing but a mutation run needs it.
//
// HALTRULE_ROOT says where the fixtures and the driver are, for a run that
// works in a copy of the module.

import (
	"bufio"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestTheFixturesPass(t *testing.T) {
	root := os.Getenv("HALTRULE_ROOT")
	if root == "" {
		root = "../.."
	}
	root, err := filepath.Abs(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(root); err != nil {
		t.Fatal(err)
	}
	paths, err := fixturePaths()
	if err != nil {
		t.Fatal(err)
	}
	// The driver writes the generated cases and says where; they are read like a fixture file.
	generated, err := exec.Command("python3", filepath.Join(root, "scripts", "conform.py"), "generate").Output()
	if err != nil {
		t.Fatalf("generating the contract cases: %v", err)
	}
	paths = append(paths, strings.TrimSpace(string(generated)))
	written, err := os.CreateTemp(t.TempDir(), "lines")
	if err != nil {
		t.Fatal(err)
	}
	lines := bufio.NewWriter(written)
	for _, path := range paths {
		if err := readFile(path, lines); err != nil {
			t.Fatalf("reading %s: %v", path, err)
		}
	}
	if err := lines.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := written.Close(); err != nil {
		t.Fatal(err)
	}
	driver := exec.Command("python3", filepath.Join(root, "scripts", "conform.py"),
		"judge", written.Name())
	driver.Dir = root
	said, err := driver.CombinedOutput()
	if err != nil {
		t.Fatalf("the fixtures do not pass:\n%s", said)
	}
}
