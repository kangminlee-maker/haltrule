package main

// The bridge a Go mutation tool needs for the shell program. Go's mutation
// testers run `go test` and decide what to mutate from what that test covers,
// so the run needs a test, and the test has to reach the program in this
// process rather than in a child.
//
// It holds no expectation of its own: scripts/conform.py says which calls to
// make (cli-calls) and judges what came back (cli-judge), as it judges the
// calls made through processes in scripts/check.sh. Nothing but a mutation run
// needs it.
//
// HALTRULE_ROOT says where the driver and the fixtures are, for a run that
// works in a copy of the tree.

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
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
	driver := filepath.Join(root, "scripts", "conform.py")
	held := t.TempDir()
	calls := filepath.Join(held, "calls.jsonl")
	ask := exec.Command("python3", driver, "cli-calls", calls)
	ask.Dir = root
	if said, err := ask.CombinedOutput(); err != nil {
		t.Fatalf("asking for the calls: %v\n%s", err, said)
	}
	source, err := os.ReadFile(calls)
	if err != nil {
		t.Fatal(err)
	}
	written, err := os.Create(filepath.Join(held, "answers.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	answers := bufio.NewWriter(written)
	for _, line := range strings.Split(strings.TrimSpace(string(source)), "\n") {
		var call struct {
			ID    string `json:"id"`
			Entry string `json:"entry"`
			Input string `json:"input"`
		}
		if err := json.Unmarshal([]byte(line), &call); err != nil {
			t.Fatalf("reading a call: %v", err)
		}
		var said bytes.Buffer
		// The arguments reach the program as a caller's would, through the
		// standard input; what it says to a person is not judged.
		code := run([]string{call.Entry, "-"}, false, strings.NewReader(call.Input), &said, io.Discard)
		answered, err := json.Marshal(map[string]any{
			"id": call.ID, "out": said.String(), "exit": code,
		})
		if err != nil {
			t.Fatalf("writing an answer: %v", err)
		}
		if _, err := answers.Write(append(answered, '\n')); err != nil {
			t.Fatal(err)
		}
	}
	if err := answers.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := written.Close(); err != nil {
		t.Fatal(err)
	}
	judge := exec.Command("python3", driver, "cli-judge", written.Name())
	judge.Dir = root
	if said, err := judge.CombinedOutput(); err != nil {
		t.Fatalf("the fixtures do not pass:\n%s", said)
	}
	// What the program promises beside answering a case: asked through itself as
	// a process, because that is where its arguments and its exits are. The
	// binary is built from this test's own directory and not from the driver's:
	// a mutation tool runs the test in a copy of the module, and the source it
	// mutated is the one beside this file. Built from the tree the driver lives
	// in, every mutant a probe alone would catch was judged on code that had
	// none - measured: two of them.
	binary := filepath.Join(held, "go-cli")
	build := exec.Command("go", "build", "-o", binary, ".")
	if said, err := build.CombinedOutput(); err != nil {
		t.Fatalf("building the program: %v\n%s", err, said)
	}
	probes := exec.Command("python3", driver, "cli-probes", binary)
	probes.Dir = root
	if said, err := probes.CombinedOutput(); err != nil {
		t.Fatalf("what it promises beside answering a case:\n%s", said)
	}
}
