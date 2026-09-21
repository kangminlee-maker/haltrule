// The Go adapter's half that turns a fixture case into a call.
//
// Two ways a value can fail to become an argument, and they are different
// answers. A value whose KIND this language has no spelling for - the
// $unsupported tags, an integer wider than int64 - cannot be handed over at
// all: that is errUnbuildable, and the case is sat out. A value that exists
// here but is outside the part's contract - a negative cap, a spec field the
// struct does not have - is refused, which is what every port answers.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"

	"haltrule"
)

var errUnbuildable = errors.New("this port's types cannot hold that value")

// decodeValue builds one value of the spec's model. The fixtures never hold a
// raw JSON number, so every number arrives as a $number or $bigint tag.
func decodeValue(raw json.RawMessage) (haltrule.Value, error) {
	var decoded any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return nil, err
	}
	return fromDecoded(decoded)
}

func fromDecoded(decoded any) (haltrule.Value, error) {
	switch held := decoded.(type) {
	case nil:
		return nil, nil
	case bool:
		return haltrule.Bool(held), nil
	case string:
		return haltrule.String(held), nil
	case float64:
		// Unreachable: an input never holds a raw JSON number.
		return haltrule.Float(held), nil
	case []any:
		list := make(haltrule.List, 0, len(held))
		for _, item := range held {
			value, err := fromDecoded(item)
			if err != nil {
				return nil, err
			}
			list = append(list, value)
		}
		return list, nil
	case map[string]any:
		if len(held) == 1 {
			for tag, literal := range held {
				text, isText := literal.(string)
				if isText && (tag == "$number" || tag == "$bigint" || tag == "$unsupported") {
					return fromTag(tag, text)
				}
			}
		}
		asMap := make(haltrule.Map, len(held))
		for key, item := range held {
			value, err := fromDecoded(item)
			if err != nil {
				return nil, err
			}
			asMap[key] = value
		}
		return asMap, nil
	}
	return nil, fmt.Errorf("a fixture input holds %T", decoded)
}

func fromTag(tag, literal string) (haltrule.Value, error) {
	switch tag {
	case "$number":
		return decodeDouble(literal)
	case "$bigint":
		integer, err := strconv.ParseInt(literal, 10, 64)
		if err != nil {
			// Wider than int64, which this language has no integer for.
			return nil, errUnbuildable
		}
		return haltrule.Int(integer), nil
	}
	// undefined, a class instance, a map with a key that is not a string, a
	// hole in a list: none of them has a Go spelling.
	return nil, errUnbuildable
}

// decodeDouble reads a $number literal by the fixture grammar and no other:
// Go's own parser takes "0x10" and "1_0", which the grammar does not.
func decodeDouble(literal string) (haltrule.Float, error) {
	switch literal {
	case "NaN":
		return haltrule.Float(math.NaN()), nil
	case "Infinity":
		return haltrule.Float(math.Inf(1)), nil
	case "-Infinity":
		return haltrule.Float(math.Inf(-1)), nil
	}
	number, err := strconv.ParseFloat(literal, 64)
	if err != nil {
		return 0, fmt.Errorf("a $number literal outside the grammar: %q", literal)
	}
	return haltrule.Float(number), nil
}

// decodeInt64 reads a number that must be an integer this port can hold: a
// cap, an amount, a bound, a count. Anything else is outside the contract, so
// the error is a refusal and not a reason to sit the case out.
func decodeInt64(raw json.RawMessage) (int64, error) {
	value, err := decodeValue(raw)
	if err != nil {
		return 0, fmt.Errorf("not an integer this port holds")
	}
	switch held := value.(type) {
	case haltrule.Int:
		return int64(held), nil
	case haltrule.Float:
		number := float64(held)
		if number != math.Trunc(number) {
			// NaN fails this one too: it equals nothing, itself included.
			return 0, fmt.Errorf("%v is not an integer", number)
		}
		// Go does not say what converting a float outside the integer's range
		// gives - one machine saturates, another wraps - so the range is
		// decided in decimal and the conversion never happens outside it.
		// An infinity has no decimal digits and fails here.
		integer, err := strconv.ParseInt(strconv.FormatFloat(number, 'f', 0, 64), 10, 64)
		if err != nil {
			return 0, fmt.Errorf("%v is outside a 64-bit integer", number)
		}
		return integer, nil
	}
	return 0, fmt.Errorf("not a number")
}

func decodeOptionalInt64(raw json.RawMessage) (*int64, error) {
	if isNull(raw) {
		return nil, nil
	}
	value, err := decodeInt64(raw)
	if err != nil {
		return nil, err
	}
	return &value, nil
}

// decodeOptionalFloat64 reads a number that may be any double: a score's bar.
// A value that is not a number - a string, a boolean - is outside the
// contract; whether a double is one the spec holds is the part's to say.
func decodeOptionalFloat64(raw json.RawMessage) (*float64, error) {
	if isNull(raw) {
		return nil, nil
	}
	value, err := decodeValue(raw)
	if err != nil {
		return nil, fmt.Errorf("not a number this port holds")
	}
	var number float64
	switch held := value.(type) {
	case haltrule.Float:
		number = float64(held)
	case haltrule.Int:
		number = float64(held)
	default:
		return nil, fmt.Errorf("not a number")
	}
	return &number, nil
}

func isNull(raw json.RawMessage) bool {
	return len(raw) == 0 || string(raw) == "null"
}

// isObject reports whether raw is a JSON object. Go's decoder reads null into
// a struct without complaining and leaves it as it was, so a map argument is
// asked for by shape before it is decoded.
func isObject(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && trimmed[0] == '{'
}

// strictly decodes one JSON value into a struct, refusing a field the struct
// does not name - which is how this port refuses a map holding a field the
// contract does not name, without a line of its own.
func strictly(raw json.RawMessage, into any) error {
	decoder := json.NewDecoder(newReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if decoder.More() {
		return fmt.Errorf("more than one value")
	}
	return nil
}

// hasUnpairedSurrogateEscape reports whether raw JSON text holds a \uD800-\uDFFF
// escape without its pair. Go's JSON decoder turns one into U+FFFD, so this
// port cannot be handed the string the case means, and sits the case out.
func hasUnpairedSurrogateEscape(raw []byte) bool {
	for at := 0; at < len(raw); {
		if raw[at] != '\\' {
			at++
			continue
		}
		if at+6 > len(raw) || raw[at+1] != 'u' {
			at += 2 // any other escape, "\\" included, so its second byte is not read as an escape
			continue
		}
		unit, ok := hex4(raw[at+2 : at+6])
		if !ok {
			at += 2
			continue
		}
		if unit >= 0xDC00 && unit <= 0xDFFF {
			return true
		}
		if unit >= 0xD800 && unit <= 0xDBFF {
			if at+12 <= len(raw) && raw[at+6] == '\\' && raw[at+7] == 'u' {
				if low, ok := hex4(raw[at+8 : at+12]); ok && low >= 0xDC00 && low <= 0xDFFF {
					at += 12
					continue
				}
			}
			return true
		}
		at += 6
	}
	return false
}

func hex4(digits []byte) (int, bool) {
	unit, err := strconv.ParseUint(string(digits), 16, 32)
	if err != nil {
		return 0, false
	}
	return int(unit), true
}
