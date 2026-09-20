package haltrule

// Slot - does a value a person or a model filled in satisfy its contract?
//
// Two kinds cover it: Choice, a value that must be one of the candidates
// exactly, and Text, a value of the person's own within length bounds. A
// reference to something that exists is a Choice whose candidates are the
// known identifiers.
//
// A missing value is a warning: the judgment has not been made yet. A present
// value that fails its contract is a halt: it would be written to a ledger as
// if it were valid. Missing is nil, or a string that is empty or holds only
// ASCII whitespace. Comparison is exact - no trimming, no case folding, no
// Unicode normalization; a caller that wants those applies them first. Lengths
// count Unicode scalar values, so every language counts the same, and a string
// that is not made of them fails its contract. In Go that is a string which is
// not valid UTF-8, which is the same thing: an unpaired surrogate has no UTF-8
// spelling. A shape beyond length - a UUID, a URL - is the caller's to check.

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// SlotKind is what a slot accepts.
type SlotKind string

const (
	Choice SlotKind = "choice"
	Text   SlotKind = "text"
)

// boundMax is the largest integer every language holds exactly: JavaScript's
// limit, as for digest inputs.
const boundMax = 1<<53 - 1

const asciiWhitespace = " \t\n\r\f\v"

// SlotSpec is what a value is held to. A field the contract does not name
// cannot be set: the struct has no such field.
type SlotSpec struct {
	Name string
	Kind SlotKind
	// Candidates are the values a Choice accepts, compared exactly. A Choice
	// needs them; nil is absent, and an empty list accepts nothing.
	Candidates []string
	// MinLength and MaxLength bound a Text's length in Unicode scalar values,
	// each in 0..2^53 - 1; nil for no bound.
	MinLength *int64
	MaxLength *int64
}

func bound(value *int64, what string, absent int64) (int64, error) {
	if value == nil {
		return absent, nil
	}
	if *value < 0 || *value > boundMax {
		return 0, fmt.Errorf("%s must be a non-negative integer up to 2^53 - 1, got %d", what, *value)
	}
	return *value, nil
}

func isBlank(text string) bool {
	return strings.Trim(text, asciiWhitespace) == ""
}

// ValidateSlot refuses a spec outside the contract - an unknown kind, a Choice
// without candidates, a bound outside 0..2^53 - 1, MinLength above MaxLength -
// and otherwise answers a verdict.
func ValidateSlot(spec SlotSpec, value Value) (Verdict, error) {
	if spec.Kind != Choice && spec.Kind != Text {
		return Verdict{}, fmt.Errorf("slot %s: unknown kind %q", spec.Name, spec.Kind)
	}
	if spec.Kind == Choice && spec.Candidates == nil {
		return Verdict{}, fmt.Errorf("slot %s: a choice needs candidates", spec.Name)
	}
	// No bound is the bound every length meets: at least 0, at most the largest.
	minimum, minErr := bound(spec.MinLength, fmt.Sprintf("slot %s: min_length", spec.Name), 0)
	if minErr != nil {
		return Verdict{}, minErr
	}
	maximum, maxErr := bound(spec.MaxLength, fmt.Sprintf("slot %s: max_length", spec.Name), boundMax)
	if maxErr != nil {
		return Verdict{}, maxErr
	}
	if minimum > maximum {
		return Verdict{}, fmt.Errorf("slot %s: min_length %d exceeds max_length %d", spec.Name, minimum, maximum)
	}

	if value == nil {
		return verdict(Warning, "slot_missing", fmt.Sprintf("slot %s: no value", spec.Name)), nil
	}
	text, isText := value.(String)
	if !isText {
		return verdict(Halt, "slot_invalid",
			fmt.Sprintf("slot %s: %s is not a string", spec.Name, describeValue(value))), nil
	}
	if !utf8.ValidString(string(text)) {
		return verdict(Halt, "slot_invalid",
			fmt.Sprintf("slot %s: not a string of Unicode scalar values", spec.Name)), nil
	}
	if isBlank(string(text)) {
		return verdict(Warning, "slot_missing", fmt.Sprintf("slot %s: blank", spec.Name)), nil
	}

	if spec.Kind == Choice {
		for _, candidate := range spec.Candidates {
			if candidate == string(text) {
				return verdict(OK, "slot_accepted", fmt.Sprintf("slot %s: %q is a candidate", spec.Name, text)), nil
			}
		}
		return verdict(Halt, "slot_invalid",
			fmt.Sprintf("slot %s: %q is not one of %d candidates", spec.Name, text, len(spec.Candidates))), nil
	}
	length := int64(utf8.RuneCountInString(string(text)))
	if length < minimum {
		return verdict(Halt, "slot_invalid",
			fmt.Sprintf("slot %s: %d characters, fewer than %d", spec.Name, length, minimum)), nil
	}
	if length > maximum {
		return verdict(Halt, "slot_invalid",
			fmt.Sprintf("slot %s: %d characters, more than %d", spec.Name, length, maximum)), nil
	}
	return verdict(OK, "slot_accepted", fmt.Sprintf("slot %s: %d characters", spec.Name, length)), nil
}
