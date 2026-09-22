// The Go adapter: reads the fixture files, feeds each case to the package
// under go/, and prints one result line per case. It judges nothing -
// scripts/conform.py compares the lines with what the fixtures expect, for
// every language alike - and it is the only place that knows how a fixture's
// field maps to this port's functions.
//
// With no argument it reads every .json file under fixtures/, by path, taken
// relative to the working directory, which is the repository root when the
// driver runs it. A line is {"actual": <result>, "id", "section"}, with
// "raised" in place of "actual" when the case failed in a way the port did not
// answer for, or {"id", "section", "unbuildable": true} for an input this
// port's types cannot hold at all.
//
// Standard library only.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"

	"haltrule"
)

func newReader(raw []byte) *bytes.Reader { return bytes.NewReader(raw) }

// ------------------------------------------------------------------ the line

// lineMaxInteger is the largest integer a result line carries: 2^53 - 1.
const lineMaxInteger = 1<<53 - 1

var refused = haltrule.Map{"refused": haltrule.Bool(true)}

// writeValue is a result line, as fixtures/README.md defines it;
// fixtures/protocol/v0.json holds its vectors. Written out member by member,
// because Go's own JSON writer escapes <, > and & and orders map keys by code
// point, and the protocol does neither.
func writeValue(value haltrule.Value) (string, error) {
	switch held := value.(type) {
	case nil:
		return "null", nil
	case haltrule.Bool:
		if held {
			return "true", nil
		}
		return "false", nil
	case haltrule.Int:
		if held > lineMaxInteger || held < -lineMaxInteger {
			return "", fmt.Errorf("a result holds %d; a result number is an integer within +/-(2^53 - 1)", int64(held))
		}
		return strconv.FormatInt(int64(held), 10), nil
	case haltrule.Float:
		number := float64(held)
		if math.IsNaN(number) || math.IsInf(number, 0) || number != math.Trunc(number) ||
			number > lineMaxInteger || number < -lineMaxInteger {
			return "", fmt.Errorf("a result holds %v; a result number is an integer within +/-(2^53 - 1)", number)
		}
		// An integer is an integer however the language holds it, and a zero
		// is written without its sign.
		return strconv.FormatInt(int64(number), 10), nil
	case haltrule.String:
		return writeString(string(held))
	case haltrule.List:
		parts := make([]string, 0, len(held))
		for _, item := range held {
			part, err := writeValue(item)
			if err != nil {
				return "", err
			}
			parts = append(parts, part)
		}
		return "[" + strings.Join(parts, ",") + "]", nil
	case haltrule.Map:
		keys := make([]string, 0, len(held))
		for key := range held {
			keys = append(keys, key)
		}
		// One order before the spec's, because Go hands out a map's keys in a
		// different one every run. The line is the same either way.
		sort.Strings(keys)
		sort.SliceStable(keys, func(left, right int) bool { return lessUTF16(keys[left], keys[right]) })
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			written, err := writeString(key)
			if err != nil {
				return "", err
			}
			part, err := writeValue(held[key])
			if err != nil {
				return "", err
			}
			parts = append(parts, written+":"+part)
		}
		return "{" + strings.Join(parts, ",") + "}", nil
	}
	return "", fmt.Errorf("a result holds a value a result line cannot carry")
}

func writeString(value string) (string, error) {
	if !utf8.ValidString(value) {
		return "", fmt.Errorf("a result holds a string that is not made of Unicode scalar values")
	}
	var out strings.Builder
	out.WriteByte('"')
	for _, character := range value {
		switch character {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		default:
			if character < 0x20 {
				fmt.Fprintf(&out, `\u%04x`, character)
			} else {
				out.WriteRune(character)
			}
		}
	}
	out.WriteByte('"')
	return out.String(), nil
}

// lessUTF16 orders keys by UTF-16 code unit, which is the protocol's order and
// not Go's own: the two disagree above U+FFFF.
func lessUTF16(left, right string) bool {
	return slices.Compare(utf16.Encode([]rune(left)), utf16.Encode([]rune(right))) < 0
}

// ------------------------------------------------- one function per section

type inputs map[string]json.RawMessage

type section func(inputs) (haltrule.Value, error)

// normative is a verdict as conformance sees it: without Message, which is for
// people. The other ports check here that a verdict has exactly the five
// fields and that its message is a string; the struct is that check.
func normative(from haltrule.Verdict) haltrule.Value {
	return haltrule.Map{
		"spec":    haltrule.String(from.Spec),
		"verdict": haltrule.String(string(from.Verdict)),
		"reason":  haltrule.String(from.Reason),
		"resume":  optionalText(from.Resume),
	}
}

// normativeMap is the same for a verdict the part hands back as a map, which
// a checkpoint issue is because a caller's own fields go in it. The message
// has to be there and be text, the struct doing that job elsewhere.
func normativeMap(from haltrule.Map) (haltrule.Value, error) {
	if _, isText := from["message"].(haltrule.String); !isText {
		return nil, fmt.Errorf("a verdict's message is missing or is not text")
	}
	out := haltrule.Map{}
	for key, value := range from {
		if key != "message" {
			out[key] = value
		}
	}
	return out, nil
}

func optionalText(value *string) haltrule.Value {
	if value == nil {
		return nil
	}
	return haltrule.String(*value)
}

func classify(from inputs) (haltrule.Value, error) {
	// Absent - not given here, null there - is no failure text to read. A
	// message of any other type is outside the contract, which this port's
	// type says by not holding it.
	raw, given := from["message"]
	if !given || isNull(raw) {
		return nil, nil
	}
	var message string
	if err := json.Unmarshal(raw, &message); err != nil {
		return refused, nil
	}
	class := haltrule.ClassifySystemicDispatchFailure(message)
	if class == nil {
		return nil, nil
	}
	return haltrule.String(string(*class)), nil
}

// only refuses a map argument holding a field the contract does not name. The
// other ports get this from a map type and a keyword signature; here the
// fields arrive as a map of raw text and the names are checked by hand.
func only(from inputs, names ...string) error {
	for key := range from {
		if !slices.Contains(names, key) {
			return fmt.Errorf("no field %q", key)
		}
	}
	return nil
}

func backoff(from inputs) (haltrule.Value, error) {
	// The case's own map is the argument: a field the contract does not name
	// reaches the part, as it would from a caller.
	if err := only(from, "attempt", "initial_ms", "cap_ms"); err != nil {
		return refused, nil
	}
	attempt, attemptErr := decodeInt64(from["attempt"])
	initialMs, initialErr := decodeInt64(from["initial_ms"])
	capMs, capErr := decodeInt64(from["cap_ms"])
	if err := errors.Join(attemptErr, initialErr, capErr); err != nil {
		return refused, nil
	}
	delay, err := haltrule.DispatchBackoffDelayMs(attempt, initialMs, capMs)
	if err != nil {
		return refused, nil
	}
	return haltrule.Int(delay), nil
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

// policyOf reads a policy, or refuses it.
func policyOf(raw json.RawMessage) (haltrule.DispatchBreakerPolicy, error) {
	var given policyJSON
	if !isObject(raw) {
		return haltrule.DispatchBreakerPolicy{}, errors.New("a policy must be a map")
	}
	if err := strictly(raw, &given); err != nil {
		return haltrule.DispatchBreakerPolicy{}, err
	}
	if given.Enabled == nil {
		return haltrule.DispatchBreakerPolicy{}, errors.New("a policy has no enabled")
	}
	threshold, thresholdErr := decodeInt64(given.SystemicThreshold)
	attempts, attemptsErr := decodeInt64(given.PerCallMaxAttempts)
	initialMs, initialErr := decodeInt64(given.BackoffInitialMs)
	capMs, capErr := decodeInt64(given.BackoffCapMs)
	if err := errors.Join(thresholdErr, attemptsErr, initialErr, capErr); err != nil {
		return haltrule.DispatchBreakerPolicy{}, err
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

// batchOf reads a policy and starts a batch, or refuses the policy.
func batchOf(raw json.RawMessage) (*haltrule.DispatchBreakerState, error) {
	policy, err := policyOf(raw)
	if err != nil {
		return nil, err
	}
	return haltrule.NewDispatchBreakerState(policy)
}

// oneReport is one item's outcome, or this port's refusal of it.
func oneReport(machine *haltrule.DispatchBreakerState, raw json.RawMessage) haltrule.Value {
	var event eventJSON
	if !isObject(raw) {
		return refused
	}
	if err := strictly(raw, &event); err != nil {
		return refused
	}
	if event.ItemID == nil {
		return refused
	}
	switch event.Kind {
	case "success":
		machine.RecordItemSuccess(*event.ItemID)
		return nil
	case "skipped":
		machine.RecordItemSkipped(*event.ItemID)
		return nil
	}
	count, err := decodeInt64(event.AttemptCount)
	if err != nil || event.FailureMessage == nil {
		return refused
	}
	// A class that is not given is not a class that is null: one is absent
	// from the contract, the other is the item's own failure.
	if len(event.FailureClass) == 0 {
		return refused
	}
	var class *haltrule.FailureClass
	if !isNull(event.FailureClass) {
		var text string
		if err := json.Unmarshal(event.FailureClass, &text); err != nil {
			return refused
		}
		held := haltrule.FailureClass(text)
		class = &held
	}
	trip, err := machine.RecordItemFailure(haltrule.DispatchDeadLetterEntry{
		ItemID:         *event.ItemID,
		FailureClass:   class,
		FailureMessage: *event.FailureMessage,
		AttemptCount:   count,
	})
	if err != nil {
		return refused
	}
	return tripValue(trip)
}

func tripValue(trip *haltrule.DispatchBreakerTripState) haltrule.Value {
	if trip == nil {
		return nil
	}
	shown := normative(trip.Verdict).(haltrule.Map)
	shown["failure_class"] = haltrule.String(string(trip.FailureClass))
	shown["consecutive_item_count"] = haltrule.Int(trip.ConsecutiveItemCount)
	shown["threshold"] = haltrule.Int(trip.Threshold)
	return shown
}

func entryValue(entry haltrule.DispatchDeadLetterEntry) haltrule.Value {
	var class haltrule.Value
	if entry.FailureClass != nil {
		class = haltrule.String(string(*entry.FailureClass))
	}
	return haltrule.Map{
		"item_id":         haltrule.String(entry.ItemID),
		"failure_class":   class,
		"failure_message": haltrule.String(entry.FailureMessage),
		"attempt_count":   haltrule.Int(entry.AttemptCount),
	}
}

func state(from inputs) (haltrule.Value, error) {
	machine, err := batchOf(from["policy"])
	if err != nil {
		return refused, nil
	}
	var events []json.RawMessage
	if err := json.Unmarshal(from["events"], &events); err != nil {
		return nil, err
	}
	returns := haltrule.List{}
	for _, raw := range events {
		// A refused report leaves the batch as it was, as a refused charge
		// leaves the ledger: the answer is the refusal and the next goes on.
		returns = append(returns, oneReport(machine, raw))
	}
	completed := haltrule.List{}
	for _, id := range machine.CompletedItemIDs() {
		completed = append(completed, haltrule.String(id))
	}
	deadLetter := haltrule.List{}
	for _, entry := range machine.DeadLetterEntries() {
		deadLetter = append(deadLetter, entryValue(entry))
	}
	return haltrule.Map{
		"returns":     returns,
		"completed":   completed,
		"dead_letter": deadLetter,
		"tripped":     tripValue(machine.Tripped()),
	}, nil
}

type outcomeJSON struct {
	Kind           string          `json:"kind"`
	FailureMessage *string         `json:"failure_message"`
	FailureClass   json.RawMessage `json:"failure_class"`
}

// script is `call` as a case scripts it: answers[i] is what item i's calls
// answer, in order, and a case without answers is every call succeeding. A
// call the script has no answer for, or an answer no call asks for, is the
// case's own mistake and is reported under its id; call cannot answer an
// error, so the first mistake is kept and reported after the loop.
// A `raises` answer is the caller's own bug and no outcome of the loop, and
// what it panics with is the fixtures' word and no part's
// (../../fixtures/README.md, "Expectations").
const (
	raisesKind        = "raises"
	raisedByTheScript = "the caller's own bug"
)

type script struct {
	items    []string
	answers  [][]haltrule.DispatchOutcome
	scripted bool
	at       int
	trouble  error
}

func (s *script) remaining(at int) []haltrule.DispatchOutcome {
	if at < len(s.answers) {
		return s.answers[at]
	}
	return nil
}

func (s *script) call(itemID string) haltrule.DispatchOutcome {
	if !s.scripted {
		return haltrule.DispatchOutcome{Kind: haltrule.OutcomeSuccess}
	}
	if len(s.remaining(s.at)) == 0 {
		s.at++
	}
	if s.at >= len(s.items) || s.items[s.at] != itemID || len(s.remaining(s.at)) == 0 {
		if s.trouble == nil {
			s.trouble = fmt.Errorf("the loop called %q where the script has no answer", itemID)
		}
		return haltrule.DispatchOutcome{Kind: haltrule.OutcomeSuccess}
	}
	outcome := s.answers[s.at][0]
	s.answers[s.at] = s.answers[s.at][1:]
	// The caller's own function failing instead of answering; the loop is to
	// let it out, and the case runner catches it where the case was run.
	if string(outcome.Kind) == raisesKind {
		panic(raisedByTheScript)
	}
	return outcome
}

func (s *script) unasked() bool {
	for _, rest := range s.answers {
		if len(rest) > 0 {
			return true
		}
	}
	return false
}

// scriptOf reads a case's answers. The driver has held them to the fixture
// protocol already, so a script it cannot read is this adapter's own failure.
func scriptOf(raw json.RawMessage) ([][]haltrule.DispatchOutcome, bool, error) {
	if isNull(raw) {
		return nil, false, nil
	}
	var scripts [][]outcomeJSON
	if err := strictly(raw, &scripts); err != nil {
		return nil, false, err
	}
	answers := make([][]haltrule.DispatchOutcome, len(scripts))
	for index, given := range scripts {
		for _, outcome := range given {
			built := haltrule.DispatchOutcome{Kind: haltrule.DispatchOutcomeKind(outcome.Kind)}
			if outcome.FailureMessage != nil {
				built.FailureMessage = *outcome.FailureMessage
			}
			if !isNull(outcome.FailureClass) {
				var text string
				if err := json.Unmarshal(outcome.FailureClass, &text); err != nil {
					return nil, false, err
				}
				held := haltrule.FailureClass(text)
				built.FailureClass = &held
			}
			answers[index] = append(answers[index], built)
		}
	}
	return answers, true, nil
}

func run(from inputs) (haltrule.Value, error) {
	policy, err := policyOf(from["policy"])
	if err != nil {
		return refused, nil
	}
	// A list of ids, each a string; absent - null - or anything else is refused.
	items, err := decodeStrings(from["items"])
	if err != nil || items == nil {
		return refused, nil
	}
	answers, scripted, err := scriptOf(from["answers"])
	if err != nil {
		return nil, err
	}
	s := &script{items: items, answers: answers, scripted: scripted}
	slept := haltrule.List{}
	result, err := haltrule.RunBatch(policy, items, s.call, func(ms int64) {
		slept = append(slept, haltrule.Int(ms))
	})
	if err != nil {
		return refused, nil
	}
	if s.trouble != nil {
		return nil, s.trouble
	}
	if s.unasked() {
		return nil, errors.New("the script holds answers no call asked for")
	}
	ids := func(held []string) haltrule.List {
		list := haltrule.List{}
		for _, id := range held {
			list = append(list, haltrule.String(id))
		}
		return list
	}
	deadLetter := haltrule.List{}
	for _, entry := range result.DeadLetter {
		deadLetter = append(deadLetter, entryValue(entry))
	}
	return haltrule.Map{
		"completed":   ids(result.Completed),
		"dead_letter": deadLetter,
		"tripped":     tripValue(result.Tripped),
		"incomplete":  ids(result.Incomplete),
		"slept":       slept,
	}, nil
}

func canonicalize(from inputs) (haltrule.Value, error) {
	value, err := decodeValue(from["input"])
	if err != nil {
		return nil, err
	}
	canonical, halt := haltrule.Canonicalize(value)
	digest, digestHalt := haltrule.CheckpointDigest(value)
	if halt != nil || digestHalt != nil {
		// The two entry points must agree about the same value, and agree in
		// the whole verdict: only one of the two is printed, so a field this
		// comparison leaves out is a field no case can see. A disagreement is
		// a bug in the port, not a result to compare.
		if halt == nil || digestHalt == nil {
			return nil, fmt.Errorf("canonicalize and the digest disagree about halting")
		}
		shown, err := writeValue(normative(*halt))
		if err != nil {
			return nil, err
		}
		other, err := writeValue(normative(*digestHalt))
		if err != nil {
			return nil, err
		}
		if shown != other {
			return nil, fmt.Errorf("canonicalize halts with %s and the digest with %s", shown, other)
		}
		return normative(*halt), nil
	}
	return haltrule.Map{
		"canonical": haltrule.String(canonical),
		"digest":    haltrule.String(digest),
	}, nil
}

type checkpointJSON struct {
	StageID                   *string         `json:"stage_id"`
	SubjectRef                *string         `json:"subject_ref"`
	Artifact                  json.RawMessage `json:"artifact"`
	ExpectedContractRevision  *string         `json:"expected_contract_revision"`
	ExpectedStageConfigDigest *string         `json:"expected_stage_config_digest"`
	ExpectedDependencyDigests json.RawMessage `json:"expected_dependency_digests"`
	RequiredResumeFromStage   *string         `json:"required_resume_from_stage"`
	ValidationIssues          json.RawMessage `json:"validation_issues"`
	StatusMap                 json.RawMessage `json:"status_map"`
}

func decodeMap(raw json.RawMessage) (haltrule.Map, error) {
	if isNull(raw) {
		return nil, nil
	}
	value, err := decodeValue(raw)
	if err != nil {
		return nil, err
	}
	asMap, isMap := value.(haltrule.Map)
	if !isMap {
		return nil, fmt.Errorf("not a map")
	}
	return asMap, nil
}

func checkpoint(from inputs) (haltrule.Value, error) {
	var given checkpointJSON
	if !isObject(from["args"]) {
		return refused, nil
	}
	if err := strictly(from["args"], &given); err != nil {
		return refused, nil
	}
	if given.StageID == nil {
		return refused, nil
	}
	args := haltrule.CheckpointArgs{
		StageID:                   *given.StageID,
		SubjectRef:                given.SubjectRef,
		ExpectedContractRevision:  given.ExpectedContractRevision,
		ExpectedStageConfigDigest: given.ExpectedStageConfigDigest,
		RequiredResumeFromStage:   given.RequiredResumeFromStage,
	}
	artifact, err := decodeMap(given.Artifact)
	if err != nil {
		if errors.Is(err, errUnbuildable) {
			return nil, err
		}
		return refused, nil
	}
	args.Artifact = artifact
	// A map argument is a value of the model like any other, so a $ tag inside it is read as the
	// value it names and not as a key: {"$number": "1"} where a map belongs is a number, refused.
	expected, err := decodeMap(given.ExpectedDependencyDigests)
	if err != nil {
		if errors.Is(err, errUnbuildable) {
			return nil, err
		}
		return refused, nil
	}
	if expected != nil {
		args.ExpectedDependencyDigests = map[string]string{}
		for id, digest := range expected {
			held, isText := digest.(haltrule.String)
			if !isText {
				return refused, nil
			}
			args.ExpectedDependencyDigests[id] = string(held)
		}
	}
	statuses, err := decodeMap(given.StatusMap)
	if err != nil {
		if errors.Is(err, errUnbuildable) {
			return nil, err
		}
		return refused, nil
	}
	if statuses != nil {
		args.StatusMap = map[string]haltrule.ArtifactStatus{}
		for name, status := range statuses {
			held, isText := status.(haltrule.String)
			if !isText {
				return refused, nil
			}
			args.StatusMap[name] = haltrule.ArtifactStatus(held)
		}
	}
	if !isNull(given.ValidationIssues) {
		var raws []json.RawMessage
		if err := json.Unmarshal(given.ValidationIssues, &raws); err != nil {
			return refused, nil
		}
		args.ValidationIssues = []haltrule.Map{}
		for _, raw := range raws {
			issue, err := decodeMap(raw)
			if err != nil {
				if errors.Is(err, errUnbuildable) {
					return nil, err
				}
				return refused, nil
			}
			if issue == nil {
				return refused, nil // null is no issue this port can hold
			}
			args.ValidationIssues = append(args.ValidationIssues, issue)
		}
	}
	issues, err := haltrule.EvaluateCheckpointArtifact(args)
	if err != nil {
		return refused, nil
	}
	answered := haltrule.List{}
	for _, issue := range issues {
		shown, err := normativeMap(haltrule.Map(issue))
		if err != nil {
			return nil, err
		}
		answered = append(answered, shown)
	}
	return answered, nil
}

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

func charge(from inputs) (haltrule.Value, error) {
	var given capsJSON
	if !isObject(from["budget"]) {
		return refused, nil
	}
	if err := strictly(from["budget"], &given); err != nil {
		return refused, nil
	}
	maxTurns, turnsErr := decodeOptionalInt64(given.MaxTurns)
	timeBudget, timeErr := decodeOptionalInt64(given.TimeBudgetMs)
	tokenBudget, tokenErr := decodeOptionalInt64(given.TokenBudget)
	if err := errors.Join(turnsErr, timeErr, tokenErr); err != nil {
		return refused, nil
	}
	budget, err := haltrule.NewBudget(haltrule.BudgetCaps{
		MaxTurns: maxTurns, TimeBudgetMs: timeBudget, TokenBudget: tokenBudget,
	})
	if err != nil {
		return refused, nil
	}
	var raws []json.RawMessage
	if err := json.Unmarshal(from["charges"], &raws); err != nil {
		return nil, err
	}
	verdicts := haltrule.List{}
	for _, raw := range raws {
		verdicts = append(verdicts, oneCharge(budget, raw))
	}
	return haltrule.Map{
		"verdicts": verdicts,
		// The ledger in decimal, so 2^63 - 1 survives JSON in every language.
		"used": haltrule.Map{
			"turns":  haltrule.String(strconv.FormatInt(budget.TurnsUsed, 10)),
			"ms":     haltrule.String(strconv.FormatInt(budget.MsUsed, 10)),
			"tokens": haltrule.String(strconv.FormatInt(budget.TokensUsed, 10)),
		},
	}, nil
}

func oneCharge(budget *haltrule.Budget, raw json.RawMessage) haltrule.Value {
	var given chargeJSON
	if !isObject(raw) {
		return refused
	}
	if err := strictly(raw, &given); err != nil {
		return refused
	}
	turns, turnsErr := decodeOptionalInt64(given.Turns)
	ms, msErr := decodeOptionalInt64(given.Ms)
	tokens, tokensErr := decodeOptionalInt64(given.Tokens)
	if err := errors.Join(turnsErr, msErr, tokensErr); err != nil {
		return refused
	}
	answer, err := budget.Charge(haltrule.Charge{Turns: turns, Ms: ms, Tokens: tokens})
	if err != nil {
		return refused
	}
	return normative(answer)
}

type specJSON struct {
	Name       *string         `json:"name"`
	Kind       *string         `json:"kind"`
	Candidates json.RawMessage `json:"candidates"`
	MinLength  json.RawMessage `json:"min_length"`
	MaxLength  json.RawMessage `json:"max_length"`
	Min        json.RawMessage `json:"min"`
	Max        json.RawMessage `json:"max"`
}

func validate(from inputs) (haltrule.Value, error) {
	value, err := decodeValue(from["value"])
	if err != nil {
		return nil, err
	}
	var given specJSON
	if !isObject(from["spec"]) {
		return refused, nil
	}
	if err := strictly(from["spec"], &given); err != nil {
		return refused, nil
	}
	if given.Name == nil {
		return refused, nil
	}
	minimum, minErr := decodeOptionalInt64(given.MinLength)
	maximum, maxErr := decodeOptionalInt64(given.MaxLength)
	floor, floorErr := decodeOptionalFloat64(given.Min)
	ceiling, ceilingErr := decodeOptionalFloat64(given.Max)
	candidates, candidatesErr := decodeStrings(given.Candidates)
	if err := errors.Join(minErr, maxErr, floorErr, ceilingErr, candidatesErr); err != nil {
		return refused, nil
	}
	spec := haltrule.SlotSpec{Name: *given.Name, MinLength: minimum, MaxLength: maximum, Min: floor, Max: ceiling}
	if given.Kind != nil {
		spec.Kind = haltrule.SlotKind(*given.Kind)
	}
	// Absent is nil, which the part answers for; given and empty is not.
	spec.Candidates = candidates
	answer, err := haltrule.ValidateSlot(spec, value)
	if err != nil {
		return refused, nil
	}
	return normative(answer), nil
}

func resultLine(from inputs) (haltrule.Value, error) {
	value, err := decodeValue(from["value"])
	if errors.Is(err, errUnbuildable) {
		// The line format's own model has no such value either, which is the
		// refusal the case asks for, not a case to sit out.
		return refused, nil
	}
	if err != nil {
		return nil, err
	}
	line, err := writeValue(value)
	if err != nil {
		return refused, nil
	}
	return haltrule.Map{"line": haltrule.String(line)}, nil
}

var sections = map[string]section{
	"classify":     classify,
	"backoff":      backoff,
	"state":        state,
	"run":          run,
	"canonicalize": canonicalize,
	"checkpoint":   checkpoint,
	"charge":       charge,
	"validate":     validate,
	"result_line":  resultLine,
}

// ------------------------------------------------------------------ the run

func fixturePaths() ([]string, error) {
	var paths []string
	err := filepath.WalkDir("fixtures", func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() && strings.HasSuffix(path, ".json") {
			paths = append(paths, filepath.ToSlash(path))
		}
		return nil
	})
	sort.Strings(paths)
	return paths, err
}

func lineFor(parts haltrule.Map) string {
	line, err := writeValue(parts)
	if err != nil {
		// A line that cannot be written is a defect in this adapter.
		fmt.Fprintln(os.Stderr, "adapter:", err)
		os.Exit(1)
	}
	return line
}

func runCase(out *bufio.Writer, sectionName string, raw json.RawMessage) error {
	var fields inputs
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	var id string
	if err := json.Unmarshal(fields["id"], &id); err != nil {
		return err
	}
	parts := haltrule.Map{"id": haltrule.String(id), "section": haltrule.String(sectionName)}
	compute, known := sections[sectionName]
	if !known {
		return fmt.Errorf("no function for section %s", sectionName)
	}
	if hasUnpairedSurrogateEscape(raw) {
		parts["unbuildable"] = haltrule.Bool(true)
		fmt.Fprintln(out, lineFor(parts))
		return nil
	}
	delete(fields, "id")
	delete(fields, "expect")
	actual, err := computed(compute, fields)
	switch {
	case errors.Is(err, errUnbuildable):
		parts["unbuildable"] = haltrule.Bool(true)
	case err != nil:
		// Reported under the case's id, never hidden.
		parts["raised"] = haltrule.String(err.Error())
	default:
		// A result the line cannot carry is this port's failure, under its id.
		if _, writeErr := writeValue(actual); writeErr != nil {
			parts["raised"] = haltrule.String(writeErr.Error())
		} else {
			parts["actual"] = actual
		}
	}
	fmt.Fprintln(out, lineFor(parts))
	return nil
}

// computed runs one case's section function and turns a panic into that case's
// own trouble: a case that raises is reported under its id, as it is in the
// languages whose calls throw, rather than ending the run and losing the rest.
func computed(compute func(inputs) (haltrule.Value, error), fields inputs) (value haltrule.Value, err error) {
	defer func() {
		if held := recover(); held != nil {
			value, err = nil, fmt.Errorf("%v", held)
		}
	}()
	return compute(fields)
}

func readFile(path string, out *bufio.Writer) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	decoder := json.NewDecoder(newReader(data))
	if _, err := decoder.Token(); err != nil { // the opening brace
		return err
	}
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return err
		}
		sectionName, isText := key.(string)
		if !isText {
			return fmt.Errorf("%s: a section name that is not a string", path)
		}
		if sectionName == "fixture_version" {
			var version string
			if err := decoder.Decode(&version); err != nil {
				return err
			}
			continue
		}
		var cases []json.RawMessage
		if err := decoder.Decode(&cases); err != nil {
			return err
		}
		for _, raw := range cases {
			if err := runCase(out, sectionName, raw); err != nil {
				return err
			}
		}
	}
	return nil
}

func main() {
	paths := os.Args[1:]
	if len(paths) == 0 {
		found, err := fixturePaths()
		if err != nil {
			fmt.Fprintln(os.Stderr, "adapter:", err)
			os.Exit(1)
		}
		paths = found
	}
	out := bufio.NewWriter(os.Stdout)
	for _, path := range paths {
		if err := readFile(path, out); err != nil {
			out.Flush()
			fmt.Fprintln(os.Stderr, "adapter:", err)
			os.Exit(1)
		}
	}
	out.Flush()
}
