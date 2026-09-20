package haltrule

// Budget - a ledger of turns, time, and tokens against caps. Pure: it never
// reads a clock or counts a token; the caller charges what it measured.
//
// A budget is exhausted when the amount used reaches its cap, for all three
// resources alike. The ledger holds 64-bit integers and its additions saturate
// at 2^63 - 1, which is what int64 already is: the type carries the range the
// other ports check for. What is left to check is that an amount is not
// negative, and a negative one is refused, which is an error here.
//
// A nil cap is no cap and a nil amount is nothing used: null and not given are
// the same thing, as everywhere in this spec.

import (
	"errors"
	"fmt"
	"math"
)

const ledgerMax = math.MaxInt64

// BudgetCaps are the caps a budget is made with; each is nil for no cap.
type BudgetCaps struct {
	MaxTurns     *int64
	TimeBudgetMs *int64
	TokenBudget  *int64
}

// Charge is what was used since the last charge; each field is nil for nothing.
type Charge struct {
	Turns  *int64
	Ms     *int64
	Tokens *int64
}

// Budget is the ledger. Make one with NewBudget.
type Budget struct {
	caps       BudgetCaps
	TurnsUsed  int64
	MsUsed     int64
	TokensUsed int64
}

func amount(value *int64, what string) (int64, error) {
	if value == nil {
		return 0, nil
	}
	if *value < 0 {
		return 0, fmt.Errorf("%s must be within [0, 2^63 - 1], got %d", what, *value)
	}
	return *value, nil
}

// NewBudget refuses a cap that is not a non-negative integer.
func NewBudget(caps BudgetCaps) (*Budget, error) {
	for _, held := range []struct {
		value *int64
		what  string
	}{
		{caps.MaxTurns, "max_turns"},
		{caps.TimeBudgetMs, "time_budget_ms"},
		{caps.TokenBudget, "token_budget"},
	} {
		if _, err := amount(held.value, held.what); err != nil {
			return nil, err
		}
	}
	return &Budget{caps: caps}, nil
}

func saturatingAdd(used, added int64) int64 {
	if used > ledgerMax-added {
		return ledgerMax
	}
	return used + added
}

// Charge adds what was used and says where the budget stands: Warning naming
// the first exhausted resource, in the order turns, time, tokens, or OK while
// every capped resource is below its cap. A charge of nothing reports the
// current state, and an exhausted budget stays exhausted. A charge is refused
// whole: every amount is read before any is added, so a refused charge leaves
// the ledger exactly as it was.
func (b *Budget) Charge(charge Charge) (Verdict, error) {
	turns, turnsErr := amount(charge.Turns, "turns")
	ms, msErr := amount(charge.Ms, "ms")
	tokens, tokensErr := amount(charge.Tokens, "tokens")
	if err := errors.Join(turnsErr, msErr, tokensErr); err != nil {
		return Verdict{}, err
	}
	b.TurnsUsed = saturatingAdd(b.TurnsUsed, turns)
	b.MsUsed = saturatingAdd(b.MsUsed, ms)
	b.TokensUsed = saturatingAdd(b.TokensUsed, tokens)
	if b.caps.MaxTurns != nil && b.TurnsUsed >= *b.caps.MaxTurns {
		return verdict(Warning, "budget_turns",
			fmt.Sprintf("turns exhausted: %s used", showAmount(b.TurnsUsed, b.caps.MaxTurns))), nil
	}
	if b.caps.TimeBudgetMs != nil && b.MsUsed >= *b.caps.TimeBudgetMs {
		return verdict(Warning, "budget_time",
			fmt.Sprintf("time exhausted: %s ms used", showAmount(b.MsUsed, b.caps.TimeBudgetMs))), nil
	}
	if b.caps.TokenBudget != nil && b.TokensUsed >= *b.caps.TokenBudget {
		return verdict(Warning, "budget_tokens",
			fmt.Sprintf("tokens exhausted: %s used", showAmount(b.TokensUsed, b.caps.TokenBudget))), nil
	}
	return verdict(OK, "budget_ok", fmt.Sprintf("within budget: turns %s, ms %s, tokens %s",
		showAmount(b.TurnsUsed, b.caps.MaxTurns),
		showAmount(b.MsUsed, b.caps.TimeBudgetMs),
		showAmount(b.TokensUsed, b.caps.TokenBudget))), nil
}
