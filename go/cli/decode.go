// The Go shell program's half that turns a JSON object into a call.
//
// Two ways a value can fail to become an argument, and they are different
// answers. A value this language cannot hold at all - an integer wider than
// int64, a string with an unpaired surrogate - is an argument that could not
// be read: errUnholdable, and no call is made (exit 4). A value that exists
// here but is outside the part's contract - a negative cap, a field the
// contract does not name - is refused (exit 3).
//
// It reads plain JSON, as a caller writes it, and not the fixture grammar the
// adapter reads; the two are held to the same answers by the same fixtures.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode/utf16"

	"haltrule"
)

// errUnholdable is a value no Go value can be: the call cannot be made at all.
var errUnholdable = errors.New("a value this language cannot hold")

// object reads one JSON object into a struct, refusing a field the struct does
// not name - which is how a map outside the contract is refused without a line
// of its own - and leaving every number as its digits.
func object(raw json.RawMessage, into any) error {
	if !isObject(raw) {
		return errors.New("not a map")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	decoder.UseNumber()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if decoder.More() {
		return errors.New("more than one value")
	}
	return nil
}

func isObject(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && trimmed[0] == '{'
}

func isNull(raw json.RawMessage) bool {
	return len(raw) == 0 || string(bytes.TrimSpace(raw)) == "null"
}

// hasUnpairedSurrogate reports whether the text holds a \uXXXX escape that no
// Go string can spell. Go's decoder puts U+FFFD in its place without a word,
// so the bytes are read before they are decoded.
func hasUnpairedSurrogate(raw []byte) bool {
	text := string(raw)
	for at := 0; at+6 <= len(text); at++ {
		if text[at] != '\\' || text[at+1] != 'u' {
			continue
		}
		first, err := strconv.ParseUint(text[at+2:at+6], 16, 32)
		if err != nil {
			continue
		}
		if !utf16.IsSurrogate(rune(first)) {
			continue
		}
		if at+12 <= len(text) && text[at+6] == '\\' && text[at+7] == 'u' {
			second, err := strconv.ParseUint(text[at+8:at+12], 16, 32)
			if err == nil && utf16.DecodeRune(rune(first), rune(second)) != 0xFFFD {
				at += 11
				continue
			}
		}
		return true
	}
	return false
}

// text reads a string. Absent - not given here, null there - is nil.
func optionalText(raw json.RawMessage) (*string, error) {
	if isNull(raw) {
		return nil, nil
	}
	var held string
	if err := json.Unmarshal(raw, &held); err != nil {
		return nil, err
	}
	return &held, nil
}

// whole reads a number that must be an integer this language holds: a cap, a
// bound, a count. A number with a fraction, or one outside int64, is outside
// the contract and not a value the language cannot hold: a caller who writes
// 2^64 has written a number, and the part refuses it.
func whole(raw json.RawMessage) (int64, error) {
	literal, err := number(raw)
	if err != nil {
		return 0, err
	}
	if held, err := literal.Int64(); err == nil {
		return held, nil
	}
	held, err := literal.Float64()
	if err != nil || held != math.Trunc(held) {
		return 0, fmt.Errorf("%v is not an integer this language holds", literal)
	}
	// Go does not say what converting a float outside the range gives, so the
	// range is decided in decimal and the conversion never happens outside it.
	return strconv.ParseInt(strconv.FormatFloat(held, 'f', 0, 64), 10, 64)
}

func optionalWhole(raw json.RawMessage) (*int64, error) {
	if isNull(raw) {
		return nil, nil
	}
	held, err := whole(raw)
	if err != nil {
		return nil, err
	}
	return &held, nil
}

// optionalDouble reads a number that may be any double: a score's bar.
func optionalDouble(raw json.RawMessage) (*float64, error) {
	if isNull(raw) {
		return nil, nil
	}
	literal, err := number(raw)
	if err != nil {
		return nil, err
	}
	held, err := literal.Float64()
	if err != nil {
		return nil, err
	}
	return &held, nil
}

func number(raw json.RawMessage) (json.Number, error) {
	// Go reads a JSON string into a json.Number without a word, and a string
	// is not a number: the token is looked at before it is decoded.
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || (trimmed[0] != '-' && (trimmed[0] < '0' || trimmed[0] > '9')) {
		return "", fmt.Errorf("not a number: %s", trimmed)
	}
	var literal json.Number
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&literal); err != nil {
		return "", err
	}
	return literal, nil
}

// strings reads a list of strings, refusing an element that is not one: Go's
// decoder reads null into a string without complaining and leaves it as it was.
// The list itself absent is nil, which the part tells from the empty list.
func stringList(raw json.RawMessage) ([]string, error) {
	if isNull(raw) {
		return nil, nil
	}
	var elements []json.RawMessage
	if err := json.Unmarshal(raw, &elements); err != nil {
		return nil, err
	}
	held := []string{}
	for _, element := range elements {
		if isNull(element) {
			return nil, errors.New("a list of strings holds null")
		}
		var one string
		if err := json.Unmarshal(element, &one); err != nil {
			return nil, err
		}
		held = append(held, one)
	}
	return held, nil
}

// value builds one value of the spec's model from plain JSON. A number is an
// integer when its digits say so and this language holds it; one it does not
// hold is a value the call cannot carry at all.
func value(raw json.RawMessage) (haltrule.Value, error) {
	if isNull(raw) {
		return nil, nil
	}
	trimmed := bytes.TrimSpace(raw)
	switch trimmed[0] {
	case 't', 'f':
		var held bool
		if err := json.Unmarshal(trimmed, &held); err != nil {
			return nil, err
		}
		return haltrule.Bool(held), nil
	case '"':
		var held string
		if err := json.Unmarshal(trimmed, &held); err != nil {
			return nil, err
		}
		return haltrule.String(held), nil
	case '[':
		var elements []json.RawMessage
		if err := json.Unmarshal(trimmed, &elements); err != nil {
			return nil, err
		}
		list := make(haltrule.List, 0, len(elements))
		for _, element := range elements {
			held, err := value(element)
			if err != nil {
				return nil, err
			}
			list = append(list, held)
		}
		return list, nil
	case '{':
		members, err := members(trimmed)
		if err != nil {
			return nil, err
		}
		held := haltrule.Map{}
		for key, member := range members {
			built, err := value(member)
			if err != nil {
				return nil, err
			}
			held[key] = built
		}
		return held, nil
	}
	literal, err := number(trimmed)
	if err != nil {
		return nil, err
	}
	if !strings.ContainsAny(literal.String(), ".eE") {
		if integer, err := literal.Int64(); err == nil {
			return haltrule.Int(integer), nil
		}
		// An integer no int64 holds is the double this language does hold,
		// which is what every port is handed for such a number anyway; the
		// part decides what it is outside the range.
	}
	held, err := literal.Float64()
	if err != nil {
		// A number too large for a double either: nothing here can hold it.
		return nil, errUnholdable
	}
	return haltrule.Float(held), nil
}

func members(raw json.RawMessage) (map[string]json.RawMessage, error) {
	var held map[string]json.RawMessage
	if err := json.Unmarshal(raw, &held); err != nil {
		return nil, err
	}
	return held, nil
}

// plain turns an answer into what encoding/json writes: the spec says the
// answer is read as JSON and not compared byte for byte, so this is all the
// writing there is.
func plain(held haltrule.Value) any {
	switch value := held.(type) {
	case nil:
		return nil
	case haltrule.Bool:
		return bool(value)
	case haltrule.Int:
		return int64(value)
	case haltrule.Float:
		return float64(value)
	case haltrule.String:
		return string(value)
	case haltrule.List:
		list := make([]any, 0, len(value))
		for _, item := range value {
			list = append(list, plain(item))
		}
		return list
	case haltrule.Map:
		members := make(map[string]any, len(value))
		for key, item := range value {
			members[key] = plain(item)
		}
		return members
	}
	return nil
}

// verdictOf is a verdict as a person reads it: the five fields, message kept.
func verdictOf(from haltrule.Verdict) map[string]any {
	var resume any
	if from.Resume != nil {
		resume = *from.Resume
	}
	return map[string]any{
		"spec":    from.Spec,
		"verdict": string(from.Verdict),
		"reason":  from.Reason,
		"message": from.Message,
		"resume":  resume,
	}
}

var refused = map[string]any{"refused": true}
