package haltrule

// Checkpoint - canonical form and digest of a value, and a verdict on whether
// an artifact an earlier run recorded may be reused.
//
// The arguments are typed, and an argument of another type cannot be built: a
// nil pointer or a nil map is absent, and nothing else is. What the artifact
// records is data, written by whoever wrote it, and is read leniently instead.

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// maxSafeInteger is the largest integer every implementation represents
// exactly: 2^53 - 1.
const maxSafeInteger = 1<<53 - 1

// maxDepth is the deepest nesting of lists and maps a digest input may have. A
// bound every language reaches without exhausting its stack, so all of them
// halt at the same depth instead of each crashing at its own.
const maxDepth = 100

const (
	digestInputFloat       = "digest_input_float"
	digestInputIntRange    = "digest_input_int_range"
	digestInputUnsupported = "digest_input_unsupported"
)

// halted is a value outside the digest value model: a halt verdict whose
// message names where in the value the trouble sits, a path being no thing two
// languages spell alike.
func halted(reason, message string) *Verdict {
	held := verdict(Halt, reason, message)
	return &held
}

// Canonicalize is the canonical form of a value: an RFC 8785 subset. Map keys
// are sorted by UTF-16 code unit, there is no insignificant whitespace,
// integers are written in decimal, and strings carry the spec's escapes.
// Strings are not Unicode-normalized.
func Canonicalize(value Value) (string, *Verdict) {
	return encodeValue(value, "$", 0)
}

// CheckpointDigest is "sha256:" and the lowercase hex SHA-256 of the canonical
// form's UTF-8 bytes, or the halt Canonicalize returned.
func CheckpointDigest(value Value) (string, *Verdict) {
	canonical, halt := Canonicalize(value)
	if halt != nil {
		return "", halt
	}
	sum := sha256.Sum256([]byte(canonical))
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

func encodeValue(value Value, at string, depth int) (string, *Verdict) {
	switch held := value.(type) {
	case nil:
		return "null", nil
	case Bool:
		if held {
			return "true", nil
		}
		return "false", nil
	case Int:
		if held > maxSafeInteger || held < -maxSafeInteger {
			return "", halted(digestInputIntRange, fmt.Sprintf("%s: %d is outside +/-(2^53 - 1)", at, int64(held)))
		}
		return strconv.FormatInt(int64(held), 10), nil
	case Float:
		number := float64(held)
		if math.IsNaN(number) || math.IsInf(number, 0) || number != math.Trunc(number) {
			return "", halted(digestInputFloat, fmt.Sprintf(
				"%s: %v is not an integer; render it as a string if it belongs in a digest", at, number))
		}
		if number > maxSafeInteger || number < -maxSafeInteger {
			return "", halted(digestInputIntRange, fmt.Sprintf("%s: %v is outside +/-(2^53 - 1)", at, number))
		}
		// A negative zero is written "0", as every zero is.
		return strconv.FormatInt(int64(number), 10), nil
	case String:
		return encodeString(string(held), at)
	case List:
		if depth >= maxDepth {
			return "", halted(digestInputUnsupported, fmt.Sprintf("%s: nested deeper than %d lists and maps", at, maxDepth))
		}
		parts := make([]string, 0, len(held))
		for index, item := range held {
			part, halt := encodeValue(item, fmt.Sprintf("%s[%d]", at, index), depth+1)
			if halt != nil {
				return "", halt
			}
			parts = append(parts, part)
		}
		return "[" + strings.Join(parts, ",") + "]", nil
	case Map:
		if depth >= maxDepth {
			return "", halted(digestInputUnsupported, fmt.Sprintf("%s: nested deeper than %d lists and maps", at, maxDepth))
		}
		parts := make([]string, 0, len(held))
		for _, key := range sortedKeys(held) {
			written, halt := encodeString(key, at+" key")
			if halt != nil {
				return "", halt
			}
			part, halt := encodeValue(held[key], fmt.Sprintf("%s.%s", at, key), depth+1)
			if halt != nil {
				return "", halt
			}
			parts = append(parts, written+":"+part)
		}
		return "{" + strings.Join(parts, ",") + "}", nil
	}
	// Value is closed, so there is no other kind to meet.
	return "", halted(digestInputUnsupported, at+": outside the digest value model")
}

// sortedKeys orders map keys by UTF-16 code unit, which is the spec's order
// and not Go's own, because they disagree above U+FFFF.
func sortedKeys(held Map) []string {
	keys := make([]string, 0, len(held))
	for key := range held {
		keys = append(keys, key)
	}
	// Go hands out a map's keys in a different order every run, so they are put
	// in one order before being put in the spec's. The answer is the same
	// either way; what this settles is the work done to reach it.
	sort.Strings(keys)
	sort.SliceStable(keys, func(left, right int) bool { return lessUTF16(keys[left], keys[right]) })
	return keys
}

func lessUTF16(left, right string) bool {
	leftUnits, rightUnits := utf16.Encode([]rune(left)), utf16.Encode([]rune(right))
	for index := 0; index < len(leftUnits) && index < len(rightUnits); index++ {
		if leftUnits[index] != rightUnits[index] {
			return leftUnits[index] < rightUnits[index]
		}
	}
	return len(leftUnits) < len(rightUnits)
}

// encodeString writes a string of Unicode scalar values, quoted, with exactly
// seven two-character escapes, every other code point below U+0020 as \u00xx
// in lowercase hex, and everything else as itself. A Go string that is not
// valid UTF-8 is not made of scalar values - an unpaired surrogate has no
// UTF-8 spelling - so it halts.
func encodeString(value, at string) (string, *Verdict) {
	if !utf8.ValidString(value) {
		return "", halted(digestInputUnsupported, at+": string is not made of Unicode scalar values")
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

// ------------------------------------------------------------ reuse verdict

// ArtifactStatus is the vocabulary every artifact is read through. Only
// Complete is reusable.
type ArtifactStatus string

const (
	Complete ArtifactStatus = "complete"
	Partial  ArtifactStatus = "partial"
	Failed   ArtifactStatus = "failed"
	Blocked  ArtifactStatus = "blocked"
)

var identityStatusMap = map[string]ArtifactStatus{
	"complete": Complete, "partial": Partial, "failed": Failed, "blocked": Blocked,
}

// Issue is one verdict about an artifact: the five fields of a Verdict, with
// stage_id and subject_ref beside them, and what its reason adds. A caller's
// own issue may lay any of them over the defaults, so it is a map and not the
// struct. Message is in it, being part of the shape; the adapters drop it,
// being for people.
type Issue map[string]Value

// CheckpointArgs is what the caller expects of an artifact now. A nil pointer
// or a nil map is absent; an empty map is present.
type CheckpointArgs struct {
	StageID    string
	SubjectRef *string
	// Artifact is what was recorded; nil when nothing was.
	Artifact Map
	// An absent expectation is not checked; any string is compared, the empty
	// one included.
	ExpectedContractRevision  *string
	ExpectedStageConfigDigest *string
	ExpectedDependencyDigests map[string]string
	// RequiredResumeFromStage defaults to StageID.
	RequiredResumeFromStage *string
	ValidationIssues        []Map
	// StatusMap replaces the default map, which sends each word of the
	// vocabulary to itself; a status it does not name is not reusable.
	StatusMap map[string]ArtifactStatus
}

func text(value *string) Value {
	if value == nil {
		return nil
	}
	return String(*value)
}

// EvaluateCheckpointArtifact decides whether a recorded artifact may be
// reused. It answers every issue found, in this order: status, contract
// revision, stage-config digest, dependency digests by UTF-16 key order, the
// caller's validation issues. With none it answers one valid issue, so the
// answer is never empty. It refuses a status map that answers outside the
// vocabulary.
func EvaluateCheckpointArtifact(args CheckpointArgs) ([]Issue, error) {
	statusMap := identityStatusMap
	if args.StatusMap != nil {
		for named, mapped := range args.StatusMap {
			if _, known := identityStatusMap[string(mapped)]; !known {
				return nil, fmt.Errorf("status_map sends %q outside the vocabulary, to %q", named, mapped)
			}
		}
		statusMap = args.StatusMap
	}
	resume := args.StageID
	if args.RequiredResumeFromStage != nil {
		resume = *args.RequiredResumeFromStage
	}
	base := func(level Level, reason, message string) Issue {
		return Issue{
			"spec":        String(Spec),
			"verdict":     String(string(level)),
			"reason":      String(reason),
			"message":     String(fmt.Sprintf("%s: %s", args.StageID, message)),
			"resume":      String(resume),
			"stage_id":    String(args.StageID),
			"subject_ref": text(args.SubjectRef),
		}
	}

	if args.Artifact == nil {
		return []Issue{base(Halt, "artifact_missing", "nothing was recorded")}, nil
	}

	var issues []Issue
	status := args.Artifact["status"]
	if jsFalsy(status) {
		issues = append(issues, base(Halt, "artifact_status_missing", "what was recorded has no status"))
	} else if resolveStatus(status, statusMap) != Complete {
		issue := base(Halt, "artifact_status_not_reusable", "the recorded status is not one that may be reused")
		issue["actual_status"] = status
		issues = append(issues, issue)
	}

	revision := args.Artifact["contract_revision"]
	if args.ExpectedContractRevision != nil && jsFalsy(revision) {
		issue := base(Halt, "contract_revision_missing",
			fmt.Sprintf("a contract revision of %q is expected and none is recorded", *args.ExpectedContractRevision))
		issue["expected_contract_revision"] = String(*args.ExpectedContractRevision)
		issues = append(issues, issue)
	} else if args.ExpectedContractRevision != nil && !sameText(revision, *args.ExpectedContractRevision) {
		issue := base(Halt, "contract_revision_mismatch",
			fmt.Sprintf("the recorded contract revision is not the expected %q", *args.ExpectedContractRevision))
		issue["expected_contract_revision"] = String(*args.ExpectedContractRevision)
		issue["actual_contract_revision"] = revision
		issues = append(issues, issue)
	}

	config := args.Artifact["stage_config_digest"]
	if args.ExpectedStageConfigDigest != nil && !sameText(config, *args.ExpectedStageConfigDigest) {
		issue := base(Halt, "stage_config_digest_mismatch",
			fmt.Sprintf("the recorded stage config digest is not the expected %q", *args.ExpectedStageConfigDigest))
		issue["expected_stage_config_digest"] = String(*args.ExpectedStageConfigDigest)
		issue["actual_stage_config_digest"] = config
		issues = append(issues, issue)
	}

	recorded, recordedIsMap := args.Artifact["dependency_digests"].(Map)
	expectedIDs := make([]string, 0, len(args.ExpectedDependencyDigests))
	for id := range args.ExpectedDependencyDigests {
		expectedIDs = append(expectedIDs, id)
	}
	sort.Slice(expectedIDs, func(left, right int) bool { return lessUTF16(expectedIDs[left], expectedIDs[right]) })
	for _, id := range expectedIDs {
		expected := args.ExpectedDependencyDigests[id]
		var actual Value
		if recordedIsMap {
			actual = recorded[id]
		}
		if !sameText(actual, expected) {
			issue := base(Halt, "dependency_digest_mismatch",
				fmt.Sprintf("the dependency %q moved", id))
			issue["dependency_id"] = String(id)
			issue["expected_digest"] = String(expected)
			issue["actual_digest"] = actual
			issues = append(issues, issue)
		}
	}

	for _, given := range args.ValidationIssues {
		issue := base(Halt, "validation_issue", "the caller's own validation found something")
		// A null field is an absent one: the default stands where there is one.
		for key, field := range given {
			if field == nil {
				continue
			}
			// A caller may disagree with a verdict, not sign one, and the three
			// are as closed a set here as they are anywhere else.
			if key == "spec" {
				return nil, fmt.Errorf("a validation issue carries a spec, which only the library says")
			}
			if key == "verdict" {
				level, isText := field.(String)
				if !isText || !knownLevel(Level(level)) {
					return nil, fmt.Errorf("a validation issue's verdict is not one of the three: %v", field)
				}
			}
			issue[key] = field
		}
		issues = append(issues, issue)
	}

	if len(issues) == 0 {
		valid := base(OK, "checkpoint_valid", "the artifact may be reused")
		valid["resume"] = nil
		return []Issue{valid}, nil
	}
	return issues, nil
}

func resolveStatus(status Value, statusMap map[string]ArtifactStatus) ArtifactStatus {
	named, isText := status.(String)
	if !isText {
		return ""
	}
	return statusMap[string(named)]
}

// jsFalsy is what "absent" means of a recorded value: null, false, 0, NaN and
// the empty string. An empty list or map is present.
func jsFalsy(value Value) bool {
	switch held := value.(type) {
	case nil:
		return true
	case Bool:
		return !bool(held)
	case Int:
		return held == 0
	case Float:
		return held == 0 || math.IsNaN(float64(held))
	case String:
		return held == ""
	}
	return false
}

// sameText is what equality means between a recorded value and an expectation:
// the same string, so a recorded 1 is not "1" and a recorded ["v2"] is not "v2".
func sameText(recorded Value, expected string) bool {
	held, isText := recorded.(String)
	return isText && string(held) == expected
}
