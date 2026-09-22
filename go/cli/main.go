// haltrule from a shell, in Go: one entry point of the contract per call.
//
//	go-cli <entry point> [<arguments file> | -]
//
// What a shell program takes and answers is ../../spec/README.md, "From a
// shell": an entry point of the contract by its own name, the arguments as one
// JSON object under the contract's names, the answer as one line of JSON with
// each verdict's message kept, and the worst verdict in that answer as the
// exit code - 0 ok, 1 warning, 2 halt; 3 for arguments the part refuses; 4 for
// a call that was never made. This program is the Go port's, and holds no rule
// of its own beyond reading the contract and calling the port: the fixtures
// hold it to the same answers the Python one gives
// (scripts/conform.py cli .bin/go-cli).
//
// It is a single file with nothing to install, which is what it is for: the
// contract it lists is compiled into it, and scripts/check.sh holds that copy
// to spec/contract.json byte for byte.
package main

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"

	"haltrule"
)

const (
	exitRefused = 3
	exitNoCall  = 4
)

//go:embed contract.json
var contract []byte

// inputs are the arguments as they were written, by name.
type inputs map[string]json.RawMessage

// answer is what an entry point answers: the value to write, or the reason
// there is none. A part's own refusal is errRefused; a value this language
// cannot hold is errUnholdable.
type entryPoint func(inputs) (any, error)

var errRefused = errors.New("outside the contract")

var entryPoints = map[string]entryPoint{
	"slot.validate":           validate,
	"budget.charge":           charge,
	"breaker.classify":        classify,
	"breaker.backoff":         backoff,
	"breaker.state":           state,
	"checkpoint.canonicalize": canonicalize,
	"checkpoint.evaluate":     evaluate,
}

func main() {
	stdin, _ := os.Stdin.Stat()
	terminal := stdin != nil && stdin.Mode()&os.ModeCharDevice != 0
	os.Exit(run(os.Args[1:], terminal, os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, terminal bool, in io.Reader, out, problems io.Writer) int {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" || len(args) > 2 {
		fmt.Fprint(out, listing(""))
		return exitNoCall
	}
	name := args[0]
	entry, known := entryPoints[name]
	if !known {
		fmt.Fprintf(problems, "no entry point is named %q\n", name)
		fmt.Fprint(out, listing(""))
		return exitNoCall
	}
	if len(args) == 1 && terminal {
		fmt.Fprint(out, listing(name))
		return exitNoCall
	}
	raw, err := read(args, in)
	if err != nil {
		fmt.Fprintf(problems, "the arguments could not be read: %v\n", err)
		return exitNoCall
	}
	// A string this language cannot spell is an argument that could not be read.
	if hasUnpairedSurrogate(raw) {
		fmt.Fprintln(problems, "the arguments hold a string this language cannot spell")
		return exitNoCall
	}
	var from inputs
	if err := json.Unmarshal(raw, &from); err != nil {
		fmt.Fprintf(problems, "the arguments are one JSON object: %v\n", err)
		return exitNoCall
	}
	answer, err := entry(from)
	// Two ifs and not a switch: Go counts a `case` line as part of the block
	// above it, so a mutation tool calls the line uncovered and never runs what
	// it planted there. By hand, negating this one fails 445 cases.
	if errors.Is(err, errUnholdable) {
		fmt.Fprintln(problems, "the arguments hold a value this language cannot hold")
		return exitNoCall
	}
	if err != nil {
		fmt.Fprintf(problems, "refused: %v\n", err)
		return exitRefused
	}
	written, err := json.Marshal(answer)
	if err != nil {
		fmt.Fprintf(problems, "the answer could not be written as JSON: %v\n", err)
		return exitNoCall
	}
	fmt.Fprintln(out, string(written))
	return worstVerdict(answer)
}

func read(args []string, in io.Reader) ([]byte, error) {
	if len(args) == 2 && args[1] != "-" {
		return os.ReadFile(args[1])
	}
	return io.ReadAll(in)
}

// worstVerdict is the worst verdict anywhere in the answer, and 0 where it
// holds none. A walk with its own stack: an answer echoes the caller's own
// values, however deep they are nested.
func worstVerdict(answer any) int {
	levels := map[string]int{"ok": 0, "warning": 1, "halt": 2}
	worst := 0
	pending := []any{answer}
	for len(pending) > 0 {
		node := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		switch held := node.(type) {
		case map[string]any:
			if level, named := held["verdict"].(string); named {
				if found, known := levels[level]; known && found > worst {
					worst = found
				}
			}
			for _, member := range held {
				pending = append(pending, member)
			}
		case []any:
			pending = append(pending, held...)
		}
	}
	return worst
}

// ------------------------------------------------------------------- breaker

func classify(from inputs) (any, error) {
	// Absent - not given here, null there - is no failure text to read.
	raw, given := from["message"]
	if !given || isNull(raw) {
		return nil, nil
	}
	var message string
	if err := json.Unmarshal(raw, &message); err != nil {
		return nil, errRefused
	}
	class := haltrule.ClassifySystemicDispatchFailure(message)
	if class == nil {
		return nil, nil
	}
	return string(*class), nil
}

type backoffJSON struct {
	Attempt   json.RawMessage `json:"attempt"`
	InitialMs json.RawMessage `json:"initial_ms"`
	CapMs     json.RawMessage `json:"cap_ms"`
}

func backoff(from inputs) (any, error) {
	// The object itself is the argument, as the contract says of this call.
	raw, err := json.Marshal(from)
	if err != nil {
		return nil, err
	}
	var given backoffJSON
	if err := object(raw, &given); err != nil {
		return nil, errRefused
	}
	attempt, attemptErr := whole(given.Attempt)
	initialMs, initialErr := whole(given.InitialMs)
	capMs, capErr := whole(given.CapMs)
	if err := errors.Join(attemptErr, initialErr, capErr); err != nil {
		return nil, errRefused
	}
	delay, err := haltrule.DispatchBackoffDelayMs(attempt, initialMs, capMs)
	if err != nil {
		return nil, errRefused
	}
	return delay, nil
}

type policyJSON struct {
	Enabled            *bool           `json:"enabled"`
	SystemicThreshold  json.RawMessage `json:"systemic_threshold"`
	Concurrent         *bool           `json:"concurrent"`
	PerCallMaxAttempts json.RawMessage `json:"per_call_max_attempts"`
	BackoffInitialMs   json.RawMessage `json:"backoff_initial_ms"`
	BackoffCapMs       json.RawMessage `json:"backoff_cap_ms"`
}

type eventJSON struct {
	Kind           string          `json:"kind"`
	ItemID         *string         `json:"item_id"`
	FailureClass   json.RawMessage `json:"failure_class"`
	FailureMessage *string         `json:"failure_message"`
	AttemptCount   json.RawMessage `json:"attempt_count"`
}

func policyOf(raw json.RawMessage) (haltrule.DispatchBreakerPolicy, error) {
	var given policyJSON
	empty := haltrule.DispatchBreakerPolicy{}
	if err := object(raw, &given); err != nil {
		return empty, err
	}
	if given.Enabled == nil {
		return empty, errors.New("a policy has no enabled")
	}
	threshold, thresholdErr := whole(given.SystemicThreshold)
	attempts, attemptsErr := whole(given.PerCallMaxAttempts)
	initialMs, initialErr := whole(given.BackoffInitialMs)
	capMs, capErr := whole(given.BackoffCapMs)
	if err := errors.Join(thresholdErr, attemptsErr, initialErr, capErr); err != nil {
		return empty, err
	}
	policy := haltrule.DispatchBreakerPolicy{
		Enabled:            *given.Enabled,
		SystemicThreshold:  threshold,
		PerCallMaxAttempts: attempts,
		BackoffInitialMs:   initialMs,
		BackoffCapMs:       capMs,
	}
	// Absent - not given here, null there - is off.
	if given.Concurrent != nil {
		policy.Concurrent = *given.Concurrent
	}
	return policy, nil
}

// oneReport is one item's outcome, or this program's refusal of it. A refused
// report leaves the batch as it was, as a refused charge leaves the ledger.
func oneReport(batch *haltrule.DispatchBreakerState, raw json.RawMessage) any {
	var event eventJSON
	if err := object(raw, &event); err != nil || event.ItemID == nil {
		return refused
	}
	switch event.Kind {
	case "success":
		batch.RecordItemSuccess(*event.ItemID)
		return nil
	case "skipped":
		batch.RecordItemSkipped(*event.ItemID)
		return nil
	case "failure":
	default:
		return refused
	}
	count, err := whole(event.AttemptCount)
	if err != nil || event.FailureMessage == nil || len(event.FailureClass) == 0 {
		return refused
	}
	// A class that is not given is not a class that is null: one is absent
	// from the contract, the other is the item's own failure.
	var class *haltrule.FailureClass
	if !isNull(event.FailureClass) {
		var held string
		if err := json.Unmarshal(event.FailureClass, &held); err != nil {
			return refused
		}
		named := haltrule.FailureClass(held)
		class = &named
	}
	trip, err := batch.RecordItemFailure(haltrule.DispatchDeadLetterEntry{
		ItemID:         *event.ItemID,
		FailureClass:   class,
		FailureMessage: *event.FailureMessage,
		AttemptCount:   count,
	})
	if err != nil {
		return refused
	}
	return tripOf(trip)
}

func tripOf(trip *haltrule.DispatchBreakerTripState) any {
	if trip == nil {
		return nil
	}
	shown := verdictOf(trip.Verdict)
	shown["failure_class"] = string(trip.FailureClass)
	shown["consecutive_item_count"] = trip.ConsecutiveItemCount
	shown["threshold"] = trip.Threshold
	return shown
}

func entryOf(entry haltrule.DispatchDeadLetterEntry) map[string]any {
	var class any
	if entry.FailureClass != nil {
		class = string(*entry.FailureClass)
	}
	return map[string]any{
		"item_id":         entry.ItemID,
		"failure_class":   class,
		"failure_message": entry.FailureMessage,
		"attempt_count":   entry.AttemptCount,
	}
}

func state(from inputs) (any, error) {
	policy, err := policyOf(from["policy"])
	if err != nil {
		return nil, errRefused
	}
	batch, err := haltrule.NewDispatchBreakerState(policy)
	if err != nil {
		return nil, errRefused
	}
	events, err := calls(from["events"])
	if err != nil {
		return nil, errRefused
	}
	returns := []any{}
	for _, raw := range events {
		returns = append(returns, oneReport(batch, raw))
	}
	completed := []any{}
	for _, id := range batch.CompletedItemIDs() {
		completed = append(completed, id)
	}
	deadLetter := []any{}
	for _, held := range batch.DeadLetterEntries() {
		deadLetter = append(deadLetter, entryOf(held))
	}
	return map[string]any{
		"returns":     returns,
		"completed":   completed,
		"dead_letter": deadLetter,
		"tripped":     tripOf(batch.Tripped()),
	}, nil
}

// calls reads a list of calls - charges, reports. Absent is none; anything
// that is not a list is outside the contract.
func calls(raw json.RawMessage) ([]json.RawMessage, error) {
	if isNull(raw) {
		return nil, nil
	}
	var held []json.RawMessage
	if err := json.Unmarshal(raw, &held); err != nil {
		return nil, err
	}
	return held, nil
}

// -------------------------------------------------------------------- budget

type capsJSON struct {
	MaxTurns     json.RawMessage `json:"max_turns"`
	TimeBudgetMs json.RawMessage `json:"time_budget_ms"`
	TokenBudget  json.RawMessage `json:"token_budget"`
}

type chargeJSON struct {
	Turns  json.RawMessage `json:"turns"`
	Ms     json.RawMessage `json:"ms"`
	Tokens json.RawMessage `json:"tokens"`
}

func charge(from inputs) (any, error) {
	var given capsJSON
	if err := object(from["budget"], &given); err != nil {
		return nil, errRefused
	}
	maxTurns, turnsErr := optionalWhole(given.MaxTurns)
	timeBudget, timeErr := optionalWhole(given.TimeBudgetMs)
	tokenBudget, tokenErr := optionalWhole(given.TokenBudget)
	if err := errors.Join(turnsErr, timeErr, tokenErr); err != nil {
		return nil, errRefused
	}
	budget, err := haltrule.NewBudget(haltrule.BudgetCaps{
		MaxTurns: maxTurns, TimeBudgetMs: timeBudget, TokenBudget: tokenBudget,
	})
	if err != nil {
		return nil, errRefused
	}
	charges, err := calls(from["charges"])
	if err != nil {
		return nil, errRefused
	}
	verdicts := []any{}
	for _, raw := range charges {
		verdicts = append(verdicts, oneCharge(budget, raw))
	}
	return map[string]any{
		"verdicts": verdicts,
		// The ledger in decimal, so 2^63 - 1 survives JSON in every language.
		"used": map[string]any{
			"turns":  strconv.FormatInt(budget.TurnsUsed, 10),
			"ms":     strconv.FormatInt(budget.MsUsed, 10),
			"tokens": strconv.FormatInt(budget.TokensUsed, 10),
		},
	}, nil
}

func oneCharge(budget *haltrule.Budget, raw json.RawMessage) any {
	var given chargeJSON
	if err := object(raw, &given); err != nil {
		return refused
	}
	turns, turnsErr := optionalWhole(given.Turns)
	ms, msErr := optionalWhole(given.Ms)
	tokens, tokensErr := optionalWhole(given.Tokens)
	if err := errors.Join(turnsErr, msErr, tokensErr); err != nil {
		return refused
	}
	answer, err := budget.Charge(haltrule.Charge{Turns: turns, Ms: ms, Tokens: tokens})
	if err != nil {
		return refused
	}
	return verdictOf(answer)
}

// ---------------------------------------------------------------------- slot

type specJSON struct {
	Name       *string         `json:"name"`
	Kind       *string         `json:"kind"`
	Candidates json.RawMessage `json:"candidates"`
	MinLength  json.RawMessage `json:"min_length"`
	MaxLength  json.RawMessage `json:"max_length"`
	Min        json.RawMessage `json:"min"`
	Max        json.RawMessage `json:"max"`
}

func validate(from inputs) (any, error) {
	held, err := value(from["value"])
	if err != nil {
		return nil, err
	}
	var given specJSON
	if err := object(from["spec"], &given); err != nil {
		return nil, errRefused
	}
	if given.Name == nil {
		return nil, errRefused
	}
	minimum, minErr := optionalWhole(given.MinLength)
	maximum, maxErr := optionalWhole(given.MaxLength)
	floor, floorErr := optionalDouble(given.Min)
	ceiling, ceilingErr := optionalDouble(given.Max)
	candidates, candidatesErr := stringList(given.Candidates)
	if err := errors.Join(minErr, maxErr, floorErr, ceilingErr, candidatesErr); err != nil {
		return nil, errRefused
	}
	spec := haltrule.SlotSpec{
		Name: *given.Name, Candidates: candidates,
		MinLength: minimum, MaxLength: maximum, Min: floor, Max: ceiling,
	}
	if given.Kind != nil {
		spec.Kind = haltrule.SlotKind(*given.Kind)
	}
	answer, err := haltrule.ValidateSlot(spec, held)
	if err != nil {
		return nil, errRefused
	}
	return verdictOf(answer), nil
}

// ---------------------------------------------------------------- checkpoint

func canonicalize(from inputs) (any, error) {
	held, err := value(from["input"])
	if err != nil {
		return nil, err
	}
	canonical, halt := haltrule.Canonicalize(held)
	digest, digestHalt := haltrule.CheckpointDigest(held)
	if halt != nil || digestHalt != nil {
		// The two entry points must agree about the same value; a disagreement
		// is a defect in the port, not an answer.
		if halt == nil || digestHalt == nil {
			return nil, errors.New("canonicalize and the digest disagree about halting")
		}
		return verdictOf(*halt), nil
	}
	return map[string]any{"canonical": canonical, "digest": digest}, nil
}

type argsJSON struct {
	StageID                   *string         `json:"stage_id"`
	SubjectRef                json.RawMessage `json:"subject_ref"`
	Artifact                  json.RawMessage `json:"artifact"`
	ExpectedContractRevision  json.RawMessage `json:"expected_contract_revision"`
	ExpectedStageConfigDigest json.RawMessage `json:"expected_stage_config_digest"`
	ExpectedDependencyDigests json.RawMessage `json:"expected_dependency_digests"`
	RequiredResumeFromStage   json.RawMessage `json:"required_resume_from_stage"`
	ValidationIssues          json.RawMessage `json:"validation_issues"`
	StatusMap                 json.RawMessage `json:"status_map"`
}

func evaluate(from inputs) (any, error) {
	var given argsJSON
	if err := object(from["args"], &given); err != nil {
		return nil, errRefused
	}
	if given.StageID == nil {
		return nil, errRefused
	}
	args := haltrule.CheckpointArgs{StageID: *given.StageID}
	subject, subjectErr := optionalText(given.SubjectRef)
	revision, revisionErr := optionalText(given.ExpectedContractRevision)
	config, configErr := optionalText(given.ExpectedStageConfigDigest)
	resume, resumeErr := optionalText(given.RequiredResumeFromStage)
	if err := errors.Join(subjectErr, revisionErr, configErr, resumeErr); err != nil {
		return nil, errRefused
	}
	args.SubjectRef, args.ExpectedContractRevision = subject, revision
	args.ExpectedStageConfigDigest, args.RequiredResumeFromStage = config, resume
	artifact, err := valueMap(given.Artifact)
	if err != nil {
		return nil, err
	}
	args.Artifact = artifact
	digests, err := textMap(given.ExpectedDependencyDigests)
	if err != nil {
		return nil, errRefused
	}
	args.ExpectedDependencyDigests = digests
	issues, err := valueMaps(given.ValidationIssues)
	if err != nil {
		return nil, err
	}
	args.ValidationIssues = issues
	statuses, err := textMap(given.StatusMap)
	if err != nil {
		return nil, errRefused
	}
	if statuses != nil {
		args.StatusMap = map[string]haltrule.ArtifactStatus{}
		for key, held := range statuses {
			args.StatusMap[key] = haltrule.ArtifactStatus(held)
		}
	}
	answers, err := haltrule.EvaluateCheckpointArtifact(args)
	if err != nil {
		return nil, errRefused
	}
	written := []any{}
	for _, issue := range answers {
		written = append(written, plain(haltrule.Map(issue)))
	}
	return written, nil
}

// valueMap reads a map of the caller's own values: an artifact, an issue.
func valueMap(raw json.RawMessage) (haltrule.Map, error) {
	if isNull(raw) {
		return nil, nil
	}
	held, err := value(raw)
	if err != nil {
		return nil, err
	}
	members, isMap := held.(haltrule.Map)
	if !isMap {
		return nil, errRefused
	}
	return members, nil
}

func valueMaps(raw json.RawMessage) ([]haltrule.Map, error) {
	if isNull(raw) {
		return nil, nil
	}
	var elements []json.RawMessage
	if err := json.Unmarshal(raw, &elements); err != nil {
		return nil, errRefused
	}
	held := []haltrule.Map{}
	for _, element := range elements {
		members, err := valueMap(element)
		if err != nil {
			return nil, err
		}
		if members == nil {
			return nil, errRefused
		}
		held = append(held, members)
	}
	return held, nil
}

// textMap reads a map whose values are strings: digests, a status map.
func textMap(raw json.RawMessage) (map[string]string, error) {
	if isNull(raw) {
		return nil, nil
	}
	var held map[string]json.RawMessage
	if err := json.Unmarshal(raw, &held); err != nil {
		return nil, err
	}
	built := map[string]string{}
	for key, member := range held {
		if isNull(member) {
			return nil, errors.New("a map of strings holds null")
		}
		var one string
		if err := json.Unmarshal(member, &one); err != nil {
			return nil, err
		}
		built[key] = one
	}
	return built, nil
}
