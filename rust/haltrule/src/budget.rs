//! Budget - a ledger of turns, time, and tokens against caps. Pure: it never
//! reads a clock or counts a token; the caller charges what it measured.
//!
//! A budget is exhausted when the amount used reaches its cap, for all three
//! resources alike. The ledger holds 64-bit integers and its additions
//! saturate at 2^63 - 1, which is what `i64` already is: the type carries the
//! range the other ports check for. What is left to check is that an amount is
//! not negative, and a negative one is refused, which is a [`Refused`] here.
//!
//! A `None` cap is no cap and a `None` amount is nothing used: null and not
//! given are the same thing, as everywhere in this spec.

use alloc::format;

use crate::messages::show_amount;
use crate::value::Refused;
use crate::verdict::{verdict, Level, Verdict};

const LEDGER_MAX: i64 = i64::MAX;

/// The caps a budget is made with; each is `None` for no cap.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BudgetCaps {
    pub max_turns: Option<i64>,
    pub time_budget_ms: Option<i64>,
    pub token_budget: Option<i64>,
}

/// What was used since the last charge; each field is `None` for nothing.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Charge {
    pub turns: Option<i64>,
    pub ms: Option<i64>,
    pub tokens: Option<i64>,
}

/// The ledger. Make one with [`Budget::new`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Budget {
    caps: BudgetCaps,
    pub turns_used: i64,
    pub ms_used: i64,
    pub tokens_used: i64,
}

fn amount(value: Option<i64>, refusal: &'static str) -> Result<i64, Refused> {
    match value {
        None => Ok(0),
        Some(value) if value < 0 => Err(Refused(refusal)),
        Some(value) => Ok(value),
    }
}

fn saturating_add(used: i64, added: i64) -> i64 {
    if used > LEDGER_MAX - added {
        return LEDGER_MAX;
    }
    used + added
}

impl Budget {
    /// Refuses a cap that is not a non-negative integer.
    pub fn new(caps: BudgetCaps) -> Result<Budget, Refused> {
        amount(caps.max_turns, "max_turns must be within [0, 2^63 - 1]")?;
        amount(
            caps.time_budget_ms,
            "time_budget_ms must be within [0, 2^63 - 1]",
        )?;
        amount(
            caps.token_budget,
            "token_budget must be within [0, 2^63 - 1]",
        )?;
        Ok(Budget {
            caps,
            turns_used: 0,
            ms_used: 0,
            tokens_used: 0,
        })
    }

    /// Adds what was used and says where the budget stands: a warning naming
    /// the first exhausted resource, in the order turns, time, tokens, or ok
    /// while every capped resource is below its cap. A charge of nothing
    /// reports the current state, and an exhausted budget stays exhausted. A
    /// charge is refused whole: every amount is read before any is added, so a
    /// refused charge leaves the ledger exactly as it was.
    pub fn charge(&mut self, charge: Charge) -> Result<Verdict, Refused> {
        let turns = amount(charge.turns, "turns must be within [0, 2^63 - 1]");
        let ms = amount(charge.ms, "ms must be within [0, 2^63 - 1]");
        let tokens = amount(charge.tokens, "tokens must be within [0, 2^63 - 1]");
        let (turns, ms, tokens) = (turns?, ms?, tokens?);
        self.turns_used = saturating_add(self.turns_used, turns);
        self.ms_used = saturating_add(self.ms_used, ms);
        self.tokens_used = saturating_add(self.tokens_used, tokens);
        if let Some(cap) = self.caps.max_turns {
            if self.turns_used >= cap {
                return Ok(verdict(
                    Level::Warning,
                    "budget_turns",
                    format!(
                        "turns exhausted: {} used",
                        show_amount(self.turns_used, self.caps.max_turns)
                    ),
                ));
            }
        }
        if let Some(cap) = self.caps.time_budget_ms {
            if self.ms_used >= cap {
                return Ok(verdict(
                    Level::Warning,
                    "budget_time",
                    format!(
                        "time exhausted: {} ms used",
                        show_amount(self.ms_used, self.caps.time_budget_ms)
                    ),
                ));
            }
        }
        if let Some(cap) = self.caps.token_budget {
            if self.tokens_used >= cap {
                return Ok(verdict(
                    Level::Warning,
                    "budget_tokens",
                    format!(
                        "tokens exhausted: {} used",
                        show_amount(self.tokens_used, self.caps.token_budget)
                    ),
                ));
            }
        }
        Ok(verdict(
            Level::Ok,
            "budget_ok",
            format!(
                "within budget: turns {}, ms {}, tokens {}",
                show_amount(self.turns_used, self.caps.max_turns),
                show_amount(self.ms_used, self.caps.time_budget_ms),
                show_amount(self.tokens_used, self.caps.token_budget)
            ),
        ))
    }
}
