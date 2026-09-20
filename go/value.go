// Package haltrule is the Go port of the haltrule spec: the policy of stopping
// well. Pure policy - no I/O, no timers, no clock, no randomness - and the
// standard library only. ../fixtures is the contract every port must satisfy;
// ../spec/README.md is the spec in words. Behaviour, not names, must match the
// other ports: Go identifiers follow Go's conventions.
//
// What the other ports check at run time, this one mostly cannot be handed at
// all: Value below is closed, a cap is an int64, and an absent argument is a
// nil pointer. That is the same contract, kept by the type instead of by code.
package haltrule

// Value is the spec's value model: null, booleans, numbers, strings, lists,
// and maps with string keys. It is closed - isValue is unexported, so no type
// outside this package can be a Value - which makes the model a type rather
// than a check. A nil Value is null.
//
// Float is here although the model holds only integers: a caller must be able
// to hand over the fractional number that a digest halts on. A number is an
// integer when its value is integral, however it is held, so Float(2) and
// Int(2) canonicalize alike.
type Value interface{ isValue() }

type (
	Bool   bool
	Int    int64
	Float  float64
	String string
	List   []Value
	Map    map[string]Value
)

func (Bool) isValue()   {}
func (Int) isValue()    {}
func (Float) isValue()  {}
func (String) isValue() {}
func (List) isValue()   {}
func (Map) isValue()    {}
