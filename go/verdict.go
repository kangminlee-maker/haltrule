package haltrule

// Spec is stamped on every verdict: draft 0 until the spec freezes.
const Spec = "haltrule/0"

// Level is what a verdict says to do: carry on, carry on and note it, or stop.
type Level string

const (
	OK      Level = "ok"
	Warning Level = "warning"
	Halt    Level = "halt"
)

// Verdict is the one shape every part returns. It is a value, never an error:
// a part decides, the caller acts. Message is for a person and is not part of
// conformance; the other four fields are.
type Verdict struct {
	Spec    string
	Verdict Level
	Reason  string
	Message string
	// Resume is where the next run should pick up, when that is knowable, and
	// nil when it is not.
	Resume *string
}

func verdict(level Level, reason, message string) Verdict {
	return Verdict{Spec: Spec, Verdict: level, Reason: reason, Message: message}
}
