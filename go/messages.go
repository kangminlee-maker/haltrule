package haltrule

import "fmt"

// Words for people. Nothing here decides anything: a verdict's Message and a
// refusal's text are not part of conformance, so no fixture can tell one
// wording from another. Code that only words a message lives here; code that
// decides lives in the part.

func showAmount(used int64, limit *int64) string {
	if limit == nil {
		return fmt.Sprintf("%d of no cap", used)
	}
	return fmt.Sprintf("%d of %d", used, *limit)
}

func describeValue(value Value) string {
	switch value.(type) {
	case nil:
		return "nothing"
	case Bool:
		return "a boolean"
	case Int, Float:
		return "a number"
	case String:
		return "a string"
	case List:
		return "a list"
	case Map:
		return "a map"
	}
	return "a value"
}
